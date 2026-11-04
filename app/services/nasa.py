"""
NASA Image and Video Library（images.nasa.gov）素材源。

目前只使用图片：搜索后按文字信息过滤第三方版权、可识别人物以及标志、文字卡和
图表，再按居中裁剪后的目标分辨率选择最小的图片规格。下载后先裁剪到目标画面
比例并缩小，再复用 ``video.render_image_zoom_video`` 渲染成短片段。接口不需要
API Key。使用条款见 https://www.nasa.gov/nasa-brand-center/images-and-media/ 。
"""

from __future__ import annotations

import html
import math
import os
import re
import tempfile
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from typing import Any, List
from urllib.parse import quote, urlsplit, urlunsplit

import requests
from loguru import logger
from PIL import Image, ImageFile

from app.config import config
from app.models.schema import ImageMotion, MaterialInfo, VideoAspect
from app.services import generation_progress, video
from app.utils import utils
from app.utils.request_logging import trace_http_call

API_ROOT = "https://images-api.nasa.gov"
DETAILS_PAGE = "https://images.nasa.gov/details/{nasa_id}"
DEFAULT_CREDIT = "NASA"
# 将来可加入 "video"；目前只实现图片。
SUPPORTED_MEDIA_TYPES = ("image",)
# 每次搜索的结果数和逐条查询资产清单/元数据的候选上限：搜索响应已经带有标题、
# 关键词和描述，文字过滤在本地完成，只有通过过滤的前若干条才需要额外请求。
_SEARCH_PAGE_SIZE = 50
_MAX_DETAILED_CANDIDATES = 15
_DETAIL_WORKERS = 4
# 与 Pixabay 等库存素材源一致：搜索 (30, 60)，下载 (60, 240)，不自动重试。
_SEARCH_TIMEOUT = (30, 60)
_DOWNLOAD_TIMEOUT = (60, 240)
_MAX_IMAGE_DOWNLOAD_BYTES = 150 * 1024 * 1024
# 元数据缺少尺寸时，只读取原图开头这部分来解析图片头。
_SIZE_PROBE_BYTES = 1024 * 1024
MIN_IMAGE_DIMENSION = 480
# 与 video.render_image_zoom_video 的放大速度一致：每秒放大 3%。
ZOOM_RATE_PER_SECOND = 0.03
# 资产清单中的派生图按长边缩放（实测 large 1920、medium 1280、small 640）。
# 缩略图（thumb）尺寸不固定且画质低，不作为素材。
VARIANT_LONG_EDGES = {"large": 1920, "medium": 1280, "small": 640}
_IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png")

# ---------------------------------------------------------------- 过滤词表
# 全部按整词、忽略大小写匹配。

# a) 版权：只有指名 NASA 以外权利人的声明才视为第三方内容。NASA 的标准文字
#    （指向 nasa.gov 使用条款）和 "NASA"、"NASA/摄影师" 不算；ICC 色彩配置文件里的
#    "Copyright (c) 1998 Hewlett-Packard Company" 属于文件格式信息，不检查。
COPYRIGHT_FIELDS = ("XMP:Rights", "IPTC:CopyrightNotice", "EXIF:Copyright", "XMP-dc:Rights")
NASA_RIGHTS_PATTERNS = (
    r"nasa\.gov",
    r"public domain",
    r"^\W*nasa(?:\s*/\s*[\w .'’-]+)?\W*$",
)
COPYRIGHT_BOILERPLATE_STARTS = ("and restrictions", "restrictions", "information", "notice", "status")

# b) 可识别人物：标题或关键词里的这些词，以及 KSC 的照片（-PH-）和视频（-VP-）编号。
PERSON_RISK_TERMS = (
    "astronaut", "astronauts", "cosmonaut", "cosmonauts", "crew", "crews", "crew member",
    "crewmember", "briefing", "interview", "interviews", "expert", "experts", "press",
    "news conference", "media day", "portrait", "portraits", "headshot", "selfie",
    "employee", "employees", "ceremony", "spokesperson",
)
PERSON_RISK_ID_PATTERNS = (r"^KSC-\d{8}-(?:PH|VP)-",)

# c) 标志、文字卡和图表：标题或关键词中的词，以及描述开头的 "This spectrum shows…" 一类句子。
TEXT_GRAPHIC_TERMS = (
    "insignia", "logo", "logos", "emblem", "patch", "infographic", "infographics",
    "diagram", "diagrams", "chart", "charts", "graph", "graphs", "spectrum", "spectra",
    "poster", "fact sheet", "timeline", "title card", "nasa direct", "coming up",
)
TEXT_GRAPHIC_DESCRIPTION_OPENINGS = (
    "spectrum", "spectra", "chart", "graph", "plot", "diagram", "infographic", "timeline",
)

# ---------------------------------------------------------------- 相关度
# NASA 的关键词很不统一：有时整条标题就是一个关键词，还常带机构和地点名称。
# 4 个词以上的关键词按标题处理；机构名称（"Johns Hopkins Applied Physics
# Laboratory"、"Target Field"）从标签里去掉，避免 "physics laboratory"、
# "star field" 一类搜索词靠机构名得分。连字符复合词合成一个词（"All-Star" 不再
# 拆出 "star"）。
LONG_KEYWORD_WORDS = 4
INSTITUTION_WORDS = (
    "Laboratory", "Laboratories", "Lab", "Center", "Centre", "Institute", "University",
    "College", "Observatory", "Field", "Facility", "Agency", "Academy", "Museum",
    "Foundation", "Corporation", "Company",
)
# 以上述词结尾、但本身是天文或物理概念的说法。
INSTITUTION_EXCEPTIONS = ("deep field", "ultra deep field", "magnetic field", "star field", "radiation field")

_INSTITUTION_SPAN = re.compile(
    r"\b(?:[A-Z][\w'’.&]*\s+){1,5}(?:" + "|".join(INSTITUTION_WORDS) + r")\b"
)
_UNIVERSITY_OF = re.compile(r"\bUniversity\s+of(?:\s+[A-Z][\w'’.-]*)+")
_HYPHEN_COMPOUND = re.compile(r"(?<=[A-Za-z]{2})[-‐‑](?=[A-Za-z]{2})")
_COPYRIGHT_MARK = re.compile(
    r"(?:©|\(c\)|\bcopyright\b)\s*(?:©\s*)?(?:\d{4}(?:\s*[-–]\s*\d{4})?\s*)?(?:by\s+)?([^\n<.;]{2,120})",
    re.I,
)
_CREDIT_LINE = re.compile(r"\b(?:image\s+|photo\s+)?credits?\s*:\s*([^\n<]+)", re.I)
_DESCRIPTION_GRAPHIC = re.compile(
    r"^\W*(?:this|the|a|an)\s+(?:[\w'’-]+\s+){0,2}(?:" + "|".join(TEXT_GRAPHIC_DESCRIPTION_OPENINGS) + r")\b",
    re.I,
)


def _term_pattern(terms) -> re.Pattern:
    return re.compile(r"\b(?:" + "|".join(re.escape(term) for term in terms) + r")\b", re.I)


_PERSON_RISK = _term_pattern(PERSON_RISK_TERMS)
_TEXT_GRAPHIC = _term_pattern(TEXT_GRAPHIC_TERMS)


def _tls_verify() -> bool:
    value = config.app.get("tls_verify", True)
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "no", "off", ""}
    return bool(value)


def _public_asset_url(url: Any) -> str:
    """资产清单中的地址可能是 http 且包含空格；统一成 https 并编码路径。"""
    parsed = urlsplit(str(url or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return ""
    return urlunsplit(("https", parsed.netloc, quote(parsed.path, safe="/%~"), "", ""))


def _get_json(url: str, params: dict | None = None) -> Any:
    """请求一个 JSON 资源；失败时只记录状态和内容类型，返回 None。"""
    try:
        response = trace_http_call(
            "GET", requests.get, url, params=params, proxies=config.proxy,
            verify=_tls_verify(), timeout=_SEARCH_TIMEOUT,
        )
    except Exception as exc:
        logger.error(f"nasa request failed: error={type(exc).__name__}, detail={exc}")
        return None
    status = int(getattr(response, "status_code", 200))
    content_type = str((getattr(response, "headers", {}) or {}).get("content-type", "")) or "unknown"
    if status == 429:
        logger.error("nasa API rate limit exceeded: status=429")
        return None
    if status >= 400:
        logger.error(f"nasa request failed: status={status}, content_type={content_type}")
        return None
    try:
        return response.json()
    except ValueError:
        logger.error(f"nasa returned a non-JSON response: status={status}, content_type={content_type}")
        return None


def configured_media_types() -> tuple[str, ...]:
    """读取 nasa_media_types；未实现的类型给出警告并忽略。"""
    value = config.app.get("nasa_media_types", list(SUPPORTED_MEDIA_TYPES))
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        value = list(SUPPORTED_MEDIA_TYPES)
    requested = [str(media_type).strip().lower() for media_type in value if str(media_type).strip()]
    unsupported = [media_type for media_type in requested if media_type not in SUPPORTED_MEDIA_TYPES]
    if unsupported:
        logger.warning(f"nasa_media_types {unsupported} are not supported yet; only images are used")
    return tuple(media_type for media_type in requested if media_type in SUPPORTED_MEDIA_TYPES)


def _text(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        value = " ".join(str(part) for part in value)
    return " ".join(html.unescape(str(value or "")).split())


def _names_only_nasa(text: str) -> bool:
    lowered = text.strip().lower()
    return any(re.search(pattern, lowered) for pattern in NASA_RIGHTS_PATTERNS)


def third_party_copyright(data: dict, metadata: dict | None = None) -> str | None:
    """
    返回指名 NASA 以外权利人的版权声明；没有时返回 None。

    检查描述中的 "©"、"(c)"、"Copyright" 声明，以及元数据的权利字段。
    """
    description = html.unescape(str(data.get("description") or ""))
    for match in _COPYRIGHT_MARK.finditer(description):
        holder = " ".join(match.group(1).split()).strip(" ,:-")
        if not holder or holder.lower().startswith(COPYRIGHT_BOILERPLATE_STARTS):
            continue
        if not _names_only_nasa(holder):
            return holder
    for field in COPYRIGHT_FIELDS:
        value = _text((metadata or {}).get(field))
        if value and not _names_only_nasa(value):
            return value[:120]
    return None


def identifiable_person_risk(data: dict) -> str | None:
    """标题、关键词或 KSC 编号显示可能有可识别人物时，返回命中的词。"""
    nasa_id = str(data.get("nasa_id") or "")
    for pattern in PERSON_RISK_ID_PATTERNS:
        if re.search(pattern, nasa_id, re.I):
            return nasa_id
    match = _PERSON_RISK.search(f"{_text(data.get('title'))} {_text(data.get('keywords'))}")
    return match.group(0).lower() if match else None


def text_graphic_risk(data: dict) -> str | None:
    """标题、关键词或描述开头显示是标志、文字卡或图表时，返回命中的词。"""
    match = _TEXT_GRAPHIC.search(f"{_text(data.get('title'))} {_text(data.get('keywords'))}")
    if match:
        return match.group(0).lower()
    match = _DESCRIPTION_GRAPHIC.search(_text(data.get("description")))
    return match.group(0).strip().lower() if match else None


def extract_credit(data: dict, metadata: dict | None = None) -> str:
    """描述中的 "Credit: …" 行，其次元数据 Credit 字段、次要作者和摄影师，最后是 NASA。"""
    match = _CREDIT_LINE.search(str(data.get("description") or ""))
    if match:
        credit = _text(match.group(1)).strip(" .;,")
        if credit:
            return credit[:200]
    for field in ("XMP:Credit", "IPTC:Credit", "Photoshop:Credit"):
        credit = _text((metadata or {}).get(field))
        if credit:
            return credit[:200]
    for field in ("secondary_creator", "photographer"):
        credit = _text(data.get(field))
        if credit:
            return credit[:200]
    return DEFAULT_CREDIT


def _strip_institutions(text: str) -> str:
    def drop(match: re.Match) -> str:
        span = match.group(0)
        lowered = span.lower()
        return span if any(lowered.endswith(exception) for exception in INSTITUTION_EXCEPTIONS) else " "

    text = _UNIVERSITY_OF.sub(" ", text)
    return _INSTITUTION_SPAN.sub(drop, text)


def relevance_tags(data: dict) -> List[str]:
    """
    生成供 material_tag_relevance 使用的标签：短关键词加去掉机构名的标题。

    只用关键词和标题，不用描述：描述通常很长，几乎任何词都能命中。
    """
    tags = []
    title_like = [_text(data.get("title"))]
    for keyword in data.get("keywords") or []:
        keyword = _HYPHEN_COMPOUND.sub("", _text(keyword))
        if not keyword:
            continue
        words = keyword.split()
        if len(words) >= LONG_KEYWORD_WORDS:
            title_like.append(keyword)
            continue
        if words[-1].lower() in {word.lower() for word in INSTITUTION_WORDS} and not any(
            keyword.lower().endswith(exception) for exception in INSTITUTION_EXCEPTIONS
        ):
            continue
        tags.append(keyword[:100])
    for text in title_like:
        cleaned = " ".join(_strip_institutions(_HYPHEN_COMPOUND.sub("", text)).split())
        if re.search(r"\w", cleaned):
            tags.append(cleaned[:100])
    return list(dict.fromkeys(tags))[:50]


def image_size(metadata: dict | None) -> tuple[int, int] | None:
    """从元数据读取原图尺寸；字段因来源而异，缺失时返回 None。"""
    for width_key, height_key in (
        ("File:ImageWidth", "File:ImageHeight"),
        ("EXIF:ExifImageWidth", "EXIF:ExifImageHeight"),
        ("EXIF:ImageWidth", "EXIF:ImageHeight"),
    ):
        try:
            width, height = int((metadata or {})[width_key]), int((metadata or {})[height_key])
        except (KeyError, TypeError, ValueError):
            continue
        if width > 0 and height > 0:
            return width, height
    return None


def _probe_image_size(url: str) -> tuple[int, int] | None:
    """只下载图片开头，用 PIL 解析图片头得到尺寸。"""
    try:
        with trace_http_call(
            "GET", requests.get, url, headers={"Range": f"bytes=0-{_SIZE_PROBE_BYTES - 1}"},
            proxies=config.proxy, verify=_tls_verify(), timeout=_SEARCH_TIMEOUT, stream=True,
        ) as response:
            if int(getattr(response, "status_code", 200)) >= 400:
                return None
            parser = ImageFile.Parser()
            received = 0
            for chunk in response.iter_content(65536):
                parser.feed(chunk)
                received += len(chunk)
                if parser.image is not None:
                    return parser.image.size
                if received >= _SIZE_PROBE_BYTES:
                    break
    except Exception as exc:
        logger.warning(f"nasa image size probe failed: error={type(exc).__name__}, detail={exc}")
    return None


def variant_files(files: List[str]) -> dict[str, str]:
    """把资产清单整理成 {规格名: 地址}，只保留 orig/large/medium/small 图片。"""
    variants = {}
    for href in files:
        name = str(href or "").rsplit("/", 1)[-1]
        stem, _, suffix = name.rpartition("~")
        variant, _, extension = suffix.partition(".")
        if not stem or f".{extension.lower()}" not in _IMAGE_SUFFIXES:
            continue
        if variant in ("orig", *VARIANT_LONG_EDGES) and variant not in variants:
            url = _public_asset_url(href)
            if url:
                variants[variant] = url
    return variants


def _center_crop_size(width: int, height: int, video_aspect: VideoAspect) -> tuple[int, int]:
    """与 material._center_crop_size 相同的居中裁剪尺寸（整数向下取整）。"""
    target_width, target_height = VideoAspect(video_aspect).to_resolution()
    if width * target_height > height * target_width:
        return height * target_width // target_height, height
    return width, width * target_height // target_width


def _orientation_matches(width: int, height: int, video_aspect: VideoAspect) -> bool:
    aspect = VideoAspect(video_aspect)
    if aspect == VideoAspect.portrait:
        return height > width
    if aspect == VideoAspect.landscape:
        return width > height
    return True


def zoom_margin(clip_duration: float, image_motion: ImageMotion = ImageMotion.zoom) -> float:
    """放大模式下画面最后放大到 1 + 3%·时长，图片需要留出这部分余量才不会被放大失真。"""
    if ImageMotion(image_motion) != ImageMotion.zoom:
        return 1.0
    return 1 + ZOOM_RATE_PER_SECOND * max(float(clip_duration), 0.0)


def choose_variant(
    variants: dict[str, str],
    width: int,
    height: int,
    video_aspect: VideoAspect,
    margin: float = 1.0,
    *,
    crop_to_aspect: bool = True,
) -> tuple[str, str, int, int] | None:
    """
    选出居中裁剪到目标比例后仍满足目标分辨率的最小规格，返回 (规格, 地址, 宽, 高)。

    同时满足放大余量的规格优先；都不满足余量时选满足目标分辨率的最小规格；
    任何规格都不满足或短边小于 480 px 时返回 None。``crop_to_aspect`` 关闭时只接受
    与目标同方向的图片，与 crop_materials_to_aspect 对库存视频的含义一致。
    """
    aspect = VideoAspect(video_aspect)
    target_width, target_height = aspect.to_resolution()
    if min(width, height) < MIN_IMAGE_DIMENSION:
        return None
    if not crop_to_aspect and not _orientation_matches(width, height, aspect):
        return None
    options = []
    for name, url in variants.items():
        if name == "orig":
            variant_width, variant_height = width, height
        else:
            long_edge = VARIANT_LONG_EDGES[name]
            scale = min(1.0, long_edge / max(width, height))
            variant_width, variant_height = round(width * scale), round(height * scale)
        if min(variant_width, variant_height) < MIN_IMAGE_DIMENSION:
            continue
        crop_width, crop_height = _center_crop_size(variant_width, variant_height, aspect)
        if crop_width < target_width or crop_height < target_height:
            continue
        lacks_margin = crop_width < target_width * margin or crop_height < target_height * margin
        options.append((lacks_margin, variant_width * variant_height, name, url, variant_width, variant_height))
    if not options:
        return None
    return min(options)[2:]


def _item_data(raw_item: Any) -> dict | None:
    if not isinstance(raw_item, dict):
        return None
    data = raw_item.get("data")
    if not isinstance(data, list) or not data or not isinstance(data[0], dict):
        return None
    return data[0]


def _detailed_candidate(
    data: dict,
    search_term: str,
    minimum_duration: int,
    video_aspect: VideoAspect,
    crop_to_aspect: bool,
    margin: float,
) -> tuple[MaterialInfo | None, str]:
    """查询资产清单和元数据，返回 (候选, 淘汰原因)。"""
    nasa_id = str(data["nasa_id"])
    asset = _get_json(f"{API_ROOT}/asset/{quote(nasa_id)}")
    files = [item.get("href") for item in ((asset or {}).get("collection") or {}).get("items") or []
             if isinstance(item, dict)]
    variants = variant_files(files)
    if not variants:
        return None, "no_image"
    metadata_url = next((_public_asset_url(f) for f in files if str(f).endswith("metadata.json")), "")
    metadata = _get_json(metadata_url) if metadata_url else None
    metadata = metadata if isinstance(metadata, dict) else {}
    if third_party_copyright(data, metadata):
        return None, "copyright"
    size = image_size(metadata)
    if size is None and "orig" in variants:
        size = _probe_image_size(variants["orig"])
    if size is None:
        return None, "no_size"
    chosen = choose_variant(variants, *size, video_aspect, margin, crop_to_aspect=crop_to_aspect)
    if chosen is None:
        return None, "resolution"
    variant, url, width, height = chosen
    item = MaterialInfo()
    item.provider = "nasa"
    item.url = url
    item.duration = max(int(minimum_duration), 1)
    item.source_info = {
        "provider": "nasa",
        "search_term": search_term,
        "asset_id": nasa_id,
        "source_page": DETAILS_PAGE.format(nasa_id=quote(nasa_id)),
        "credit": extract_credit(data, metadata),
        "rendition": {"id": variant, "width": width, "height": height},
        "tags": relevance_tags(data),
        # NASA 不提供 AI 生成标记；保留字段以便 exclude_ai_generated_materials 一致处理。
        "ai_generated": False,
    }
    return item, ""


def search_images(
    search_term: str,
    minimum_duration: int,
    video_aspect: VideoAspect = VideoAspect.portrait,
    *,
    crop_to_aspect: bool = False,
) -> List[MaterialInfo]:
    """
    搜索 NASA 图片并返回可用候选，顺序与 NASA 的相关度排序一致。

    ``minimum_duration`` 是要渲染的片段时长；图片片段时长由渲染决定，因此每个
    候选的 duration 就是它。每次搜索记录各过滤器淘汰的数量。
    """
    if "image" not in configured_media_types():
        logger.warning("nasa search skipped: nasa_media_types does not include \"image\"")
        return []
    aspect = VideoAspect(video_aspect)
    logger.info(f"searching images on nasa: term={search_term!r}, proxy_enabled={bool(config.proxy)}")
    body = _get_json(f"{API_ROOT}/search", {"q": search_term, "media_type": "image", "page_size": _SEARCH_PAGE_SIZE})
    collection = body.get("collection") if isinstance(body, dict) else None
    raw_items = collection.get("items") if isinstance(collection, dict) else None
    if not isinstance(raw_items, list):
        if body is not None:
            logger.error("nasa image search returned an unsupported response")
        return []

    skipped: Counter = Counter()
    survivors = []
    for raw_item in raw_items:
        data = _item_data(raw_item)
        if not data or not data.get("nasa_id") or str(data.get("media_type") or "image") != "image":
            skipped["invalid"] += 1
        elif identifiable_person_risk(data):
            skipped["person"] += 1
        elif text_graphic_risk(data):
            skipped["text_graphic"] += 1
        elif third_party_copyright(data):
            skipped["copyright"] += 1
        else:
            survivors.append(data)

    margin = zoom_margin(minimum_duration)
    detailed = survivors[:_MAX_DETAILED_CANDIDATES]
    with ThreadPoolExecutor(max_workers=_DETAIL_WORKERS, thread_name_prefix="nasa-detail") as executor:
        results = list(executor.map(
            lambda data: _detailed_candidate(data, search_term, minimum_duration, aspect, crop_to_aspect, margin),
            detailed,
        ))
    items = []
    for item, reason in results:
        if item is None:
            skipped[reason] += 1
        else:
            items.append(item)
    total_hits = (collection.get("metadata") or {}).get("total_hits")
    logger.info(
        f"nasa image search: term={search_term!r}, total_hits={total_hits}, results={len(raw_items)}, "
        f"checked={len(detailed)}, kept={len(items)}, skipped={dict(skipped)}, "
        f"unchecked={len(survivors) - len(detailed)}"
    )
    return items


# ---------------------------------------------------------------- 下载与渲染


def _download_image(url: str, save_dir: str) -> str:
    """下载图片到缓存目录；完整下载并能被 PIL 识别后才发布到最终路径。"""
    extension = os.path.splitext(urlsplit(url).path)[1].lower() or ".jpg"
    image_path = os.path.join(save_dir, f"nasa-{utils.md5(url)}{extension}")
    if os.path.isfile(image_path) and os.path.getsize(image_path) > 0:
        try:
            with Image.open(image_path) as cached:
                cached.verify()
            return image_path
        except Exception:
            logger.warning(f"discarding invalid cached nasa image: {image_path}")
    temp_path = ""
    try:
        with tempfile.NamedTemporaryFile("wb", prefix=".nasa-", suffix=extension, dir=save_dir, delete=False) as temp:
            temp_path = temp.name
            with trace_http_call(
                "GET", requests.get, url, proxies=config.proxy, verify=_tls_verify(),
                timeout=_DOWNLOAD_TIMEOUT, stream=True,
            ) as response:
                response.raise_for_status()
                downloaded = 0
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    downloaded += len(chunk)
                    if downloaded > _MAX_IMAGE_DOWNLOAD_BYTES:
                        raise ValueError("nasa image download exceeds 150 MB limit")
                    temp.write(chunk)
        with Image.open(temp_path) as downloaded_image:
            downloaded_image.verify()
        os.replace(temp_path, image_path)
        temp_path = ""
        return image_path
    except Exception as exc:
        logger.error(f"failed to download nasa image: error={type(exc).__name__}, detail={exc}")
        return ""
    finally:
        if temp_path and os.path.exists(temp_path):
            os.remove(temp_path)


def prepare_image(
    image_path: str,
    video_aspect: VideoAspect,
    clip_duration: float,
    image_motion: ImageMotion = ImageMotion.zoom,
) -> str:
    """
    把图片居中裁剪到目标比例，并缩小到 目标尺寸 × 放大余量，返回处理后的路径。

    只缩小不放大：裁剪区域比所需尺寸小（但不小于目标分辨率）时保持原样；比目标
    分辨率还小时返回空字符串。JPEG 先用 draft 按 2 的幂降采样解码，避免把上亿像素的
    原图完整读入内存。
    """
    aspect = VideoAspect(video_aspect)
    target_width, target_height = aspect.to_resolution()
    margin = zoom_margin(clip_duration, image_motion)
    desired_width = math.ceil(target_width * margin)
    try:
        with Image.open(image_path) as image:
            width, height = image.size
            crop_width, crop_height = _center_crop_size(width, height, aspect)
            if crop_width < target_width or crop_height < target_height:
                logger.warning(
                    f"nasa image is below the output resolution after cropping: {width}x{height}"
                )
                return ""
            out_width = min(crop_width, desired_width)
            out_height = out_width * target_height // target_width
            scale = out_width / crop_width
            if scale < 1 and image.format == "JPEG":
                image.draft("RGB", (math.ceil(width * scale), math.ceil(height * scale)))
            image = image.convert("RGB")
            width, height = image.size
            crop_width, crop_height = _center_crop_size(width, height, aspect)
            left, top = (width - crop_width) // 2, (height - crop_height) // 2
            image = image.crop((left, top, left + crop_width, top + crop_height))
            if image.size != (out_width, out_height):
                image = image.resize((out_width, out_height), Image.LANCZOS)
            root, _ = os.path.splitext(image_path)
            prepared_path = f"{root}-{aspect.name}-{out_width}x{out_height}.jpg"
            image.save(prepared_path, "JPEG", quality=92)
            return prepared_path
    except Exception as exc:
        logger.error(f"failed to prepare nasa image: error={type(exc).__name__}, detail={exc}")
        return ""


def save_image_clip(
    item: MaterialInfo,
    save_dir: str = "",
    *,
    video_aspect: VideoAspect,
    clip_duration: float,
    image_motion: ImageMotion = ImageMotion.zoom,
) -> str:
    """下载 NASA 图片，裁剪缩小后渲染成片段，返回片段路径；失败返回空字符串。"""
    generation_progress.downloading()
    if not save_dir:
        save_dir = utils.storage_dir("cache_videos")
    os.makedirs(save_dir, exist_ok=True)
    image_path = _download_image(item.url, save_dir)
    if not image_path:
        return ""
    prepared_path = prepare_image(image_path, video_aspect, clip_duration, image_motion)
    if not prepared_path:
        return ""
    try:
        return video.render_image_zoom_video(prepared_path, int(math.ceil(clip_duration)), image_motion=image_motion)
    except Exception as exc:
        logger.error(f"failed to render nasa image clip: error={type(exc).__name__}, detail={exc}")
        return ""
