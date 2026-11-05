"""
NASA Image and Video Library（images.nasa.gov）素材源。

目前只使用图片：搜索后按文字信息过滤第三方版权、可识别人物以及标志、文字卡和
图表，再按居中裁剪后的目标分辨率选择最小的图片规格。下载后先裁剪到目标画面
比例并缩小；安装了可选依赖 opencv-python-headless 时再检查画面里有没有人脸，
最后复用 ``video.render_image_zoom_video`` 渲染成短片段。接口不需要 API Key。
使用条款见 https://www.nasa.gov/nasa-brand-center/images-and-media/ 。
"""

from __future__ import annotations

import hashlib
import html
import json
import math
import os
import re
import tempfile
import threading
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
USAGE_GUIDELINES_URL = "https://www.nasa.gov/nasa-brand-center/images-and-media/"
# 过滤规则（人物、标志、版权、规格选择）的版本，写入 24 小时搜索缓存的键：规则改变后
# 必须加 1，否则旧规则选出的候选还会被使用一整天。1 = 首个版本，2 = 活动用语、NHQ 编号、
# 人名关键词和人脸检查，3 = 署名去掉话题标签，署名里没有 NASA 或核实过的合作方时淘汰，
# 4 = 候选带预览图地址（文字检查用），nasa_text_filter 写入缓存键。
FILTER_VERSION = 4
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
#    署名里既没有 NASA 也没有下面这些合作方时（例如只写 "SpaceX" 或某公司、某机构的
#    摄影师），图片可能属于第三方，淘汰。名单只收官方条款里核实过可免费使用、包括商用的
#    合作方（2026-11 查阅）：
#    - ESA/Hubble、ESA/Webb：CC BY 4.0，可商用，署名须原样、醒目（YouTube 视频要叠加在
#      画面上）。https://esahubble.org/copyright/ https://esawebb.org/copyright/
#    - JPL（JPL-Caltech）："may be used for any purpose without prior permission"，署名
#      "Courtesy NASA/JPL-Caltech"。https://www.jpl.nasa.gov/jpl-image-use-policy
#    - STScI：按 NASA 合同可如公有领域使用，请求注明 NASA 和 STScI。https://www.stsci.edu/copyright
#    - Chandra（CXC、SAO）：不主张版权，按 NASA 指南使用。https://chandra.harvard.edu/photo/image_use.html
#    不在名单内：ESA 官网的一般图片（"ESA"、"European Space Agency (ESA) - D. Ducros"
#    这类署名），商用需要 ESA 的书面授权。https://www.esa.int/ESA_Multimedia/Copyright_Notice_Images
#    与 NASA 合署的署名（"ESA/Herschel/...; NASA, ESA and ..."）因为包含 NASA 而保留。
FREE_USE_PARTNERS = ("ESA/Hubble", "ESA/Webb", "JPL", "STScI", "CXC", "SAO", "Chandra")
#    NASA 自己的中心名称同样算作 NASA（实测 "ASA/GSFC/Solar Dynamics Observatory" 这类
#    署名漏写了 NASA）。
NASA_CENTERS = (
    "GSFC", "Goddard", "JSC", "Johnson Space Center", "KSC", "Kennedy Space Center", "MSFC",
    "Marshall Space Flight Center", "ARC", "Ames", "AFRC", "Armstrong Flight Research Center", "Dryden",
    "GRC", "Glenn Research Center", "LaRC", "Langley",
)
#    摄影师字段只写人名时（宇航员、NASA 摄影师：实测 "Alex Gerst"、"EMMETT GIVEN"），
#    视为 NASA 的照片并写成 "NASA/姓名"；写的是机构名（"SpaceX"）时按上面的名单检查。

# b) 可识别人物。词表和阈值按 32 个搜索词首页结果（1055 条，其中 481 条人工看过预览图）
#    的实测选定。
#    - 标题或关键词里的人物词。国际空间站照片标题常写 "photographed by the Expedition 6
#      crew"，只说明拍摄者：实测这类照片 10 张全是地球或星空，因此先去掉这些说法再匹配。
PERSON_RISK_TERMS = (
    "astronaut", "astronauts", "cosmonaut", "cosmonauts", "crew", "crews", "crew member",
    "crewmember", "briefing", "interview", "interviews", "expert", "experts", "press",
    "news conference", "media day", "portrait", "portraits", "headshot", "selfie",
    "employee", "employees", "ceremony", "spokesperson",
)
CREW_PHOTO_PATTERNS = (
    r"\bcrew earth observations?\b",
    r"\b(?:taken|photographed|captured|downlinked|shot) by (?:an? |the )?(?:[\w'’-]+\s+){0,4}?"
    r"crew(?:\s*members?)?\b",
)
#    - 标题、关键词或描述中的活动用语：实测 59 条命中全部有人。单独的 event、panel、
#      presents、visits、signs、audience 在科学图片说明里误报（"eruption event"、"solar
#      panel"、"this image presents"、"signs of"），只用下面带上下文的说法；"from left
#      to right" 多指画面里的运动方向，也不用。人体模型（mannequin）在画面里像人，一并排除。
EVENT_TERMS = (
    "speaks", "speaking", "spoke", "panelist", "panelists", "on a panel", "panel discussion",
    "attendees", "audience members", "ceremony", "ceremonies", "remarks", "guests", "reporters",
    "journalists", "news conference", "press conference", "poses for", "poses with",
    "posing for", "posing with", "posed for", "signing", "award ceremony", "awards ceremony",
    "honoree", "honorees", "mannequin", "mannequins",
)
EVENT_PATTERNS = (
    r"\bduring an? (?:[\w'’-]+\s+){0,3}event\b",
    r"\bevent (?:titled|called|named|hosted|held|honoring|celebrating)\b",
)
#    - 编号。NASA 总部摄影（NHQ）直接排除：实测 70 张中 69 张是活动照片，唯一例外是一张
#      发射照片，而且其他信号也分不开它。各中心的摄影编号（KSC、JSC、AFRC…）单看编号时
#      只有约一半有人（181 张中 87 张），多为飞机、火箭和设施，因此只在描述里同时出现
#      人物角色词时排除（实测 35 张中 25 张有人）；"Dr. 某某"、principal investigator、
#      physicist 一类指向具体人物的写法也算（又多 6 张，其中 5 张有人）；technician、engineer 一类泛称不算
#      （61 张中只有 11 张能看清人脸）。
PERSON_RISK_ID_PATTERNS = (r"^NHQ\d",)
PHOTO_OFFICE_ID_PATTERNS = (
    r"^KSC-", r"^\d{2}p[dp]\d{4}", r"^JSC", r"^jsc\d{4}e", r"^AFRC\d{4}-", r"^E[CD]\d{2}-",
    r"^GRC-", r"^LRC-", r"^MSFC",
)
PHOTO_OFFICE_ROLE_TERMS = (
    "astronaut", "astronauts", "cosmonaut", "cosmonauts", "crew member", "crew members",
    "crewmember", "crewmembers", "commander", "pilot", "mission specialist", "flight engineer",
    "payload specialist", "suits up", "suit up", "principal investigator", "physicist",
    "physicists", "researcher", "researchers", "chemist", "chemists",
)
# 区分大小写：只认 "Dr. Mark Nurge"、"scientist Jane Doe" 这类后面跟着姓名的写法。
PHOTO_OFFICE_NAMED_PERSON_PATTERNS = (
    r"\bDr\.\s+[A-Z][a-z]+",
    r"\b[Ss]cientists?\s+[A-Z][a-z]+\s+[A-Z][a-z]+",
)
#    - 关键词大多是人名：2～3 个首字母大写的词（或姓名缩写），不含下面的常见非人名词，也
#      不是摄影师或作者本人。至少 2 个且占全部关键词 60% 以上时排除；实测这样 2 条命中都
#      是活动照片。阈值降到 50% 会把列出项目成员姓名的模型飞机照片也排除。
PERSON_NAME_MIN_COUNT = 2
PERSON_NAME_MIN_SHARE = 0.6
NON_NAME_WORDS = (
    # 天体和天文概念
    "Galaxy", "Galaxies", "Nebula", "Cluster", "Way", "Milky", "Hole", "Holes", "Black", "Spiral",
    "Remnant", "Nova", "Universe", "Field", "Deep", "Cosmic", "Stellar", "Bubble", "Shock", "Jets",
    "Loop", "Borealis", "Australis", "Earth", "Moon", "Sun", "Mars", "Magnetic", "Lensing",
    "Formation", "Activity",
    # 任务、设施和机构
    "Space", "Telescope", "Observatory", "Station", "Center", "Centre", "Program", "Mission",
    "Horizons", "Shuttle", "Launch", "Building", "Campus", "Laboratory", "Lab", "Research",
    "Flight", "Facility", "International", "Rocket", "Aircraft", "Module", "Stage", "Probe",
    "Clipper", "Services", "Technology", "Transportation", "Observation", "Imaging",
    # 公司和地名
    "Lockheed", "Martin", "Northrop", "Grumman", "Marietta", "Works", "Zealand", "Guiana",
)

# c) 标志、文字卡和图表：标题或关键词中的词，以及描述开头的 "This spectrum shows…" 一类句子。
#    描述里只认明确的徽标说法（实测 2 条命中都是徽标或任务徽章）；单独的 patch 在描述里
#    多是 "a patch of sky"。
TEXT_GRAPHIC_TERMS = (
    "insignia", "logo", "logos", "emblem", "patch", "infographic", "infographics",
    "diagram", "diagrams", "chart", "charts", "graph", "graphs", "spectrum", "spectra",
    "poster", "fact sheet", "timeline", "title card", "nasa direct", "coming up",
)
TEXT_GRAPHIC_DESCRIPTION_TERMS = (
    "insignia", "meatball", "mission patch", "crew patch", "emblem", "emblems", "logo", "logos",
)
TEXT_GRAPHIC_DESCRIPTION_OPENINGS = (
    "spectrum", "spectra", "chart", "graph", "plot", "diagram", "infographic", "timeline",
)

# d) 人脸（可选，需要 opencv-python-headless，`uv sync --extra face`）：在裁剪、缩小后的
#    图片上用随附的 YuNet 模型（cv2.FaceDetectorYN，MIT 许可）检测，先缩到 FACE_DETECT_WIDTH
#    宽。实测（129 张有人照片的 9:16 裁剪、319 张天文图片）：置信度 0.75、人脸至少 24 px 时
#    有人照片 93 张检出，天文图片 0 张误报；0.7 时星云开始误报。OpenCV 自带的 Haar 分类器
#    在同一批天文图片上误报 6%～17%，不用。
FACE_MODEL_FILE = os.path.join(os.path.dirname(__file__), "face_detection", "face_detection_yunet_2023mar.onnx")
FACE_DETECT_WIDTH = 720
FACE_SCORE_THRESHOLD = 0.75
FACE_NMS_THRESHOLD = 0.3
FACE_MIN_SIZE = 24
# 被裁剪边缘切掉一部分的脸在裁剪图上常常检测不到。裁剪图没有检出人脸时，再在原图上
# 以同样的比例检测裁剪区域及其两侧（各 FACE_EDGE_MARGIN 个裁剪宽度），至少 15% 落在
# 裁剪区域内、置信度不低于 0.85 的脸也算。实测（138 张可辨认人物照片、379 张天文图片）：
# 裁剪图单独检测 100 张检出；加上这一步 107 张，天文误报仍为 0。置信度 0.75 时一张黑洞
# 示意图的辉光误报；落入比例 20% 时会漏掉 Rocket Lab 照片里只露出约 20% 的半张脸。
FACE_EDGE_SCORE_THRESHOLD = 0.85
FACE_EDGE_MIN_INSIDE = 0.15
FACE_EDGE_MARGIN = 0.5

# e) 图片上的文字（可选，同样需要 face 依赖里的 opencv）：在下载原图之前，用随附的 PP-OCRv3
#    文字检测模型（OpenCV Zoo，Apache-2.0，cv2.dnn_TextDetectionModel_DB）检查 NASA 的 ~small
#    预览图（长边约 640 px），并按目标比例算出预览图上的居中裁剪区域：有文字行与裁剪区域
#    相交就淘汰，完全在裁剪区域外的文字不会出现在视频里，不淘汰。文字行要求置信度不低于
#    0.7、宽高比不低于 2.5。可行性测量（86 个候选，其中 12 张带标题、标注或图表，40 张天文
#    图片）：只看置信度时木星云带、星云结和红外暗斑被当成文字（天文误报 2/40），加上宽高比
#    后 12 张都检出、天文误报 0。
TEXT_MODEL_FILE = os.path.join(
    os.path.dirname(__file__), "text_detection", "text_detection_en_ppocrv3_2023may.onnx"
)
# 预览图先放大到这个长边（32 的倍数）再检测，与测量时相同。
TEXT_INPUT_LONG_EDGE = 1312
TEXT_SCORE_THRESHOLD = 0.7
TEXT_MIN_ASPECT = 2.5
# DB 后处理参数，与 OpenCV Zoo 示例相同：二值化阈值、多边形阈值、扩张比例、最多候选数。
TEXT_DB_BINARY_THRESHOLD = 0.3
TEXT_DB_POLYGON_THRESHOLD = 0.5
TEXT_DB_UNCLIP_RATIO = 2.0
TEXT_DB_MAX_CANDIDATES = 200
# 每个关键词按排序依次检查，直到 TEXT_CHECKED_CANDIDATES 个候选通过（被淘汰的不计）。按文案
# 顺序匹配时一个关键词通常用 1～3 段素材（88 秒的 K3 用了 6 个关键词的 18 段），更靠后的
# 候选只在下载失败或人脸被拒时才会用到；只查到第一个通过的候选会漏掉第二、三段素材
# （K3 里带标题的 Egg Nebula 是 "light beam" 的第三段）。
TEXT_CHECKED_CANDIDATES = 3
# 默认开启。独立验证集（9 个新的太空主题、61 个关键词各自前 5 个候选，共 230 个，不含可行性
# 测量的图片，人工复核）：裁剪区域内叠加文字（标题、说明、时间戳、坐标轴）28 张检出 26 张
# （93%），实物上的文字（标牌、标志）算上时 40 张检出 33 张；没有文字的 190 张误报 6 张（3.2%，
# 都是厂房和设备照片），其中天文图片 129 张误报 0 张。每个关键词平均淘汰 0.64 个候选。
TEXT_FILTER_DEFAULT = True

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
# 描述的段落之间常只剩两个空格；句号后接大写单词视为句子结束，但 "D. Coe" 这类缩写不算。
_CREDIT_END = re.compile(r"\s{2,}|(?<!\b[A-Z])\.\s+(?=[A-Z][a-z]+\s)")
# 署名后面接着的话题标签、@账号和 "Follow us" 一类社交媒体文字。
_CREDIT_SOCIAL_TAIL = re.compile(
    r"\s*(?:#\w|@\w|\b(?:follow us|read more|learn more|more info|for more)\b|https?://|www\.)", re.I
)
_FREE_USE_CREDIT = re.compile(
    r"(?<![A-Za-z])(?:"
    + "|".join(re.escape(name) for name in ("NASA", *NASA_CENTERS, *FREE_USE_PARTNERS))
    + r")(?![A-Za-z])"
)
_PERSON_NAME_TOKEN = re.compile(r"^(?:[A-Z][a-z]+(?:[-'’][A-Za-z]+)*|[A-Z]{2,}|[A-Z]\.)$")
_DESCRIPTION_GRAPHIC = re.compile(
    r"^\W*(?:this|the|a|an)\s+(?:[\w'’-]+\s+){0,2}(?:" + "|".join(TEXT_GRAPHIC_DESCRIPTION_OPENINGS) + r")\b",
    re.I,
)
_HTML_TAG = re.compile(r"<[^>]*>")
_URL = re.compile(r"https?://\S+|www\.\S+")
_NAME_TOKEN = re.compile(r"^(?:[A-Z][a-z]+(?:[-'’][A-Z]?[a-z]+)*|[A-Z]\.)$")
_NON_NAME = frozenset(word.lower() for word in NON_NAME_WORDS)


def _term_pattern(terms, extra_patterns=()) -> re.Pattern:
    alternatives = [r"\b(?:" + "|".join(re.escape(term) for term in terms) + r")\b", *extra_patterns]
    return re.compile("|".join(alternatives), re.I)


_PERSON_RISK = _term_pattern(PERSON_RISK_TERMS)
_CREW_PHOTO = re.compile("|".join(CREW_PHOTO_PATTERNS), re.I)
_EVENT = _term_pattern(EVENT_TERMS, EVENT_PATTERNS)
_PHOTO_OFFICE_ROLE = _term_pattern(PHOTO_OFFICE_ROLE_TERMS)
_PHOTO_OFFICE_NAMED_PERSON = re.compile("|".join(PHOTO_OFFICE_NAMED_PERSON_PATTERNS))
_TEXT_GRAPHIC = _term_pattern(TEXT_GRAPHIC_TERMS)
_TEXT_GRAPHIC_DESCRIPTION = _term_pattern(TEXT_GRAPHIC_DESCRIPTION_TERMS)


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


def search_cache_variant() -> dict[str, Any]:
    """
    写入搜索缓存键的过滤版本和相关设置。

    即使所有设置都是默认值也始终写入：在加入版本之前保存的缓存是旧规则的结果，
    键与默认设置相同就会被继续使用。
    """
    return {
        "filters": FILTER_VERSION,
        "face_filter": face_filter_enabled(),
        "text_filter": text_filter_enabled(),
        "media_types": list(configured_media_types()),
    }


def _text(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        value = " ".join(str(part) for part in value)
    return " ".join(html.unescape(str(value or "")).split())


def _plain_text(value: Any) -> str:
    """去掉 HTML 标签和链接：描述里的链接地址（如 nasa.gov/audience/…）会造成误报。"""
    return " ".join(_URL.sub(" ", _HTML_TAG.sub(" ", _text(value))).split())


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


def person_name_keywords(data: dict) -> tuple[int, int]:
    """返回 (像人名的关键词数, 关键词总数)，规则见 NON_NAME_WORDS 上方的说明。"""
    keywords = [_text(keyword) for keyword in data.get("keywords") or [] if _text(keyword)]
    credits = {_text(data.get(field)).lower() for field in ("photographer", "secondary_creator")}
    names = 0
    for keyword in keywords:
        tokens = keyword.split()
        if (
            2 <= len(tokens) <= 3
            and all(_NAME_TOKEN.match(token) for token in tokens)
            and not any(token.lower() in _NON_NAME for token in tokens)
            and keyword.lower() not in credits
        ):
            names += 1
    return names, len(keywords)


def identifiable_person_risk(data: dict) -> str | None:
    """
    可能有可识别人物时返回命中的依据；没有时返回 None。

    依次检查：NHQ 编号；标题或关键词里的人物词；标题、关键词或描述里的活动用语；
    中心摄影编号加描述里的人物角色词；以人名为主的关键词。
    """
    nasa_id = str(data.get("nasa_id") or "")
    for pattern in PERSON_RISK_ID_PATTERNS:
        if re.search(pattern, nasa_id):
            return nasa_id
    title_keywords = f"{_text(data.get('title'))} {_text(data.get('keywords'))}"
    match = _PERSON_RISK.search(_CREW_PHOTO.sub(" ", title_keywords))
    if match:
        return match.group(0).lower()
    description = _plain_text(data.get("description"))
    match = _EVENT.search(f"{title_keywords} {description}")
    if match:
        return " ".join(match.group(0).lower().split())
    if any(re.search(pattern, nasa_id) for pattern in PHOTO_OFFICE_ID_PATTERNS):
        match = _PHOTO_OFFICE_ROLE.search(description) or _PHOTO_OFFICE_NAMED_PERSON.search(description)
        if match:
            return f"{nasa_id}: {match.group(0).lower()}"
    names, keywords = person_name_keywords(data)
    if names >= PERSON_NAME_MIN_COUNT and names >= PERSON_NAME_MIN_SHARE * keywords:
        return f"person names {names}/{keywords}"
    return None


def text_graphic_risk(data: dict) -> str | None:
    """标题、关键词、描述开头或描述中的徽标说法显示是标志、文字卡或图表时，返回命中的词。"""
    match = _TEXT_GRAPHIC.search(f"{_text(data.get('title'))} {_text(data.get('keywords'))}")
    if match:
        return match.group(0).lower()
    match = _DESCRIPTION_GRAPHIC.search(_text(data.get("description")))
    if match:
        return match.group(0).strip().lower()
    match = _TEXT_GRAPHIC_DESCRIPTION.search(_plain_text(data.get("description")))
    return match.group(0).lower() if match else None


def _clean_credit(text: Any) -> str:
    """截到段落或句子结尾，去掉整个包住署名的括号（"(NASA/Keegan Barber)"）。"""
    if isinstance(text, (list, tuple)):
        text = " ".join(str(part) for part in text)
    credit = _CREDIT_END.split(html.unescape(str(text or "")).strip(), maxsplit=1)[0]
    credit = _CREDIT_SOCIAL_TAIL.split(credit, maxsplit=1)[0]
    credit = " ".join(credit.split()).strip(" .;,")
    if credit.startswith("(") and credit.endswith(")"):
        depth = 0
        for index, char in enumerate(credit):
            depth += (char == "(") - (char == ")")
            if depth == 0 and index < len(credit) - 1:
                break  # "(NASA) and (ESA)" 不是整体包住
        else:
            credit = credit[1:-1].strip(" .;,")
    return credit[:200]


def free_use_credit(credit: str) -> bool:
    """署名包含 NASA、NASA 中心或 FREE_USE_PARTNERS 中核实过可自由使用（含商用）的合作方时为 True。"""
    return bool(_FREE_USE_CREDIT.search(str(credit or "")))


def _photographer_credit(name: str) -> str:
    """摄影师字段：2～4 个词的人名写成 "NASA/姓名"；机构名（"SpaceX"）原样返回。"""
    tokens = name.split()
    if 2 <= len(tokens) <= 4 and all(_PERSON_NAME_TOKEN.match(token) for token in tokens) \
            and not free_use_credit(name):
        return f"NASA/{name}"
    return name


def extract_credit(data: dict, metadata: dict | None = None) -> str:
    """描述中的 "Credit: …" 行，其次元数据 Credit 字段、次要作者和摄影师，最后是 NASA。"""
    match = _CREDIT_LINE.search(str(data.get("description") or ""))
    if match:
        credit = _clean_credit(match.group(1))
        if credit:
            return credit
    for field in ("XMP:Credit", "IPTC:Credit", "Photoshop:Credit"):
        credit = _clean_credit((metadata or {}).get(field))
        if credit:
            return credit
    for field in ("secondary_creator", "photographer"):
        credit = _clean_credit(data.get(field))
        if credit:
            return _photographer_credit(credit) if field == "photographer" else credit
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
    credit = extract_credit(data, metadata)
    if not free_use_credit(credit):
        logger.info(f"nasa credit filter: skipped {nasa_id}, credit={credit!r}")
        return None, "third_party_credit"
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
        "credit": credit,
        # 文字检查在下载原图之前看这张约 640 px 的预览图。
        "preview": variants.get("small") or variants.get("medium") or "",
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
            root, _ = os.path.splitext(image_path)
            prepared_path = f"{root}-{aspect.name}-{out_width}x{out_height}.jpg"
            # 人脸检查和渲染会先后用到同一张处理后的图片。
            if os.path.isfile(prepared_path) and os.path.getsize(prepared_path) > 0:
                return prepared_path
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
            temp_path = f"{prepared_path}.part"
            image.save(temp_path, "JPEG", quality=92)
            os.replace(temp_path, prepared_path)
            return prepared_path
    except Exception as exc:
        logger.error(f"failed to prepare nasa image: error={type(exc).__name__}, detail={exc}")
        return ""


# ---------------------------------------------------------------- 人脸检查

_face_lock = threading.Lock()
_face_detector_state: dict[str, Any] = {}
_face_counts: dict[tuple[str, float, int], int] = {}
_edge_face_counts: dict[tuple[str, float, int, str], int] = {}


def face_filter_enabled() -> bool:
    """读取 nasa_face_filter，默认开启。"""
    value = config.app.get("nasa_face_filter", True)
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "no", "off", ""}
    return bool(value)


def _face_detector():
    """按需加载 OpenCV 和 YuNet 人脸检测器；不可用时只警告一次并返回 None。"""
    with _face_lock:
        if "loaded" not in _face_detector_state:
            try:
                import cv2
                import numpy

                detector = cv2.FaceDetectorYN.create(
                    FACE_MODEL_FILE, "", (FACE_DETECT_WIDTH, FACE_DETECT_WIDTH),
                    FACE_SCORE_THRESHOLD, FACE_NMS_THRESHOLD, 5000,
                )
                _face_detector_state["loaded"] = (numpy, detector)
            except Exception as exc:
                _face_detector_state["loaded"] = None
                logger.warning(
                    "nasa face filter is unavailable, only the text filters are used "
                    f"(install it with `uv sync --extra face`): error={type(exc).__name__}, detail={exc}"
                )
        return _face_detector_state["loaded"]


def face_extra_installed() -> bool:
    """可选依赖 face（opencv-python-headless）是否已安装；只检查，不加载也不记录警告。"""
    import importlib.util

    return importlib.util.find_spec("cv2") is not None


def face_filter_active() -> bool:
    """nasa_face_filter 开启并且 OpenCV 可用。"""
    return face_filter_enabled() and _face_detector() is not None


def count_faces(image_path: str) -> int | None:
    """
    返回处理后图片中的人脸数；检测器不可用或读取失败时返回 None。

    先缩到 FACE_DETECT_WIDTH 宽再检测，只计置信度和尺寸都达到阈值的人脸；同一文件的
    结果会缓存。检测器对象不能并发使用，检测时加锁。
    """
    loaded = _face_detector()
    if loaded is None:
        return None
    numpy, detector = loaded
    try:
        stat = os.stat(image_path)
        key = (os.path.abspath(image_path), stat.st_mtime, stat.st_size)
        if key in _face_counts:
            return _face_counts[key]
        with Image.open(image_path) as image:
            image = image.convert("RGB")
            if image.width > FACE_DETECT_WIDTH:
                height = round(image.height * FACE_DETECT_WIDTH / image.width)
                image = image.resize((FACE_DETECT_WIDTH, height), Image.LANCZOS)
            bgr = numpy.ascontiguousarray(numpy.asarray(image)[:, :, ::-1])
        with _face_lock:
            detector.setInputSize((bgr.shape[1], bgr.shape[0]))
            _, faces = detector.detect(bgr)
        count = sum(
            1 for face in (faces if faces is not None else [])
            if face[14] >= FACE_SCORE_THRESHOLD and min(face[2], face[3]) >= FACE_MIN_SIZE
        )
        _face_counts[key] = count
        return count
    except Exception as exc:
        logger.warning(f"nasa face check failed: error={type(exc).__name__}, detail={exc}")
        return None


def count_edge_faces(image_path: str, video_aspect: VideoAspect) -> int | None:
    """
    在原图上找被裁剪边缘切掉一部分的脸，返回人脸数；检测器不可用或读取失败时返回 None。

    检测区域是居中裁剪区域向四周各扩 FACE_EDGE_MARGIN 个裁剪宽度（不超出原图），按与
    count_faces 相同的比例（裁剪宽度缩到 FACE_DETECT_WIDTH）缩小。只计置信度不低于
    FACE_EDGE_SCORE_THRESHOLD、至少 FACE_EDGE_MIN_INSIDE 落在裁剪区域内的脸；完全在
    裁剪区域外的脸不会出现在视频里，不计。
    """
    loaded = _face_detector()
    if loaded is None:
        return None
    numpy, detector = loaded
    try:
        aspect = VideoAspect(video_aspect)
        stat = os.stat(image_path)
        key = (os.path.abspath(image_path), stat.st_mtime, stat.st_size, aspect.value)
        if key in _edge_face_counts:
            return _edge_face_counts[key]
        with Image.open(image_path) as image:
            width, height = image.size
            crop_width, crop_height = _center_crop_size(width, height, aspect)
            crop_left, crop_top = (width - crop_width) // 2, (height - crop_height) // 2
            margin = round(crop_width * FACE_EDGE_MARGIN)
            window = (max(0, crop_left - margin), max(0, crop_top - margin),
                      min(width, crop_left + crop_width + margin), min(height, crop_top + crop_height + margin))
            scale = FACE_DETECT_WIDTH / crop_width
            if scale < 1 and image.format == "JPEG":
                image.draft("RGB", (math.ceil(width * scale), math.ceil(height * scale)))
            ratio = image.width / width
            region = image.convert("RGB").crop(tuple(round(value * ratio) for value in window))
            size = (max(1, round((window[2] - window[0]) * scale)), max(1, round((window[3] - window[1]) * scale)))
            region = region.resize(size, Image.LANCZOS)
            bgr = numpy.ascontiguousarray(numpy.asarray(region)[:, :, ::-1])
        with _face_lock:
            detector.setInputSize((bgr.shape[1], bgr.shape[0]))
            _, faces = detector.detect(bgr)
        x0, y0 = (crop_left - window[0]) * scale, (crop_top - window[1]) * scale
        x1, y1 = x0 + crop_width * scale, y0 + crop_height * scale
        count = 0
        for face in faces if faces is not None else []:
            fx, fy, fw, fh = (float(value) for value in face[:4])
            if face[14] < FACE_EDGE_SCORE_THRESHOLD or min(fw, fh) < FACE_MIN_SIZE:
                continue
            inside_width = max(0.0, min(fx + fw, x1) - max(fx, x0))
            inside_height = max(0.0, min(fy + fh, y1) - max(fy, y0))
            if inside_width * inside_height >= FACE_EDGE_MIN_INSIDE * fw * fh:
                count += 1
        _edge_face_counts[key] = count
        return count
    except Exception as exc:
        logger.warning(f"nasa edge face check failed: error={type(exc).__name__}, detail={exc}")
        return None


def _prepared_image(
    item: MaterialInfo, save_dir: str, video_aspect: VideoAspect, clip_duration: float, image_motion: ImageMotion,
) -> tuple[str, str]:
    """下载并处理图片，返回（原图路径, 处理后的路径）；失败时对应路径为空字符串。"""
    if not save_dir:
        save_dir = utils.storage_dir("cache_videos")
    os.makedirs(save_dir, exist_ok=True)
    image_path = _download_image(item.url, save_dir)
    if not image_path:
        return "", ""
    return image_path, prepare_image(image_path, video_aspect, clip_duration, image_motion)


def _shows_face(item: MaterialInfo, prepared_path: str, image_path: str = "",
                video_aspect: VideoAspect | None = None) -> bool:
    if not face_filter_enabled():
        return False
    faces, where = count_faces(prepared_path), "faces"
    # 裁剪图上没有检出时，再看原图里裁剪边缘被切掉的脸。
    if not faces and image_path and video_aspect is not None:
        faces, where = count_edge_faces(image_path, video_aspect), "faces at the crop edge"
    if faces:
        source_info = item.source_info if isinstance(item.source_info, dict) else {}
        logger.info(f"nasa face filter: skipped {source_info.get('asset_id') or item.url}, {where}={faces}")
        return True
    return False


def screen_image(
    item: MaterialInfo,
    save_dir: str = "",
    *,
    video_aspect: VideoAspect,
    clip_duration: float,
    image_motion: ImageMotion = ImageMotion.zoom,
) -> bool:
    """
    下载并处理图片，检查人脸；可以使用时返回 True。

    按文案顺序匹配时先用它筛选，被拒绝的图片换成同一关键词的下一个候选；
    下载和处理结果会被随后的 save_image_clip 复用。
    """
    image_path, prepared_path = _prepared_image(item, save_dir, video_aspect, clip_duration, image_motion)
    return bool(prepared_path) and not _shows_face(item, prepared_path, image_path, video_aspect)


def save_image_clip(
    item: MaterialInfo,
    save_dir: str = "",
    *,
    video_aspect: VideoAspect,
    clip_duration: float,
    image_motion: ImageMotion = ImageMotion.zoom,
) -> str:
    """下载 NASA 图片，裁剪缩小并检查人脸后渲染成片段，返回片段路径；失败或有人脸时返回空字符串。"""
    generation_progress.downloading()
    image_path, prepared_path = _prepared_image(item, save_dir, video_aspect, clip_duration, image_motion)
    if not prepared_path or _shows_face(item, prepared_path, image_path, video_aspect):
        return ""
    try:
        # 处理后的图片已是目标比例并带有放大余量，直接渲染成成片尺寸，合成阶段不再缩放。
        return video.render_image_zoom_video(
            prepared_path, int(math.ceil(clip_duration)), image_motion=image_motion,
            output_size=VideoAspect(video_aspect).to_resolution(),
        )
    except Exception as exc:
        logger.error(f"failed to render nasa image clip: error={type(exc).__name__}, detail={exc}")
        return ""


# ---------------------------------------------------------------- 文字检查

_text_lock = threading.Lock()
_text_detector_state: dict[str, Any] = {}
_text_lines: dict[tuple[str, float, int, str], list] = {}


def text_filter_enabled() -> bool:
    """读取 nasa_text_filter，默认值见 TEXT_FILTER_DEFAULT。"""
    value = config.app.get("nasa_text_filter", TEXT_FILTER_DEFAULT)
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "no", "off", ""}
    return bool(value)


def _text_detector():
    """按需加载 OpenCV 和 PP-OCRv3 文字检测模型；不可用时只警告一次并返回 None。"""
    with _text_lock:
        if "loaded" not in _text_detector_state:
            try:
                import cv2
                import numpy

                model = cv2.dnn_TextDetectionModel_DB(cv2.dnn.readNet(TEXT_MODEL_FILE))
                # 与 OpenCV Zoo 示例相同的后处理阈值和归一化参数。
                model.setBinaryThreshold(TEXT_DB_BINARY_THRESHOLD).setPolygonThreshold(TEXT_DB_POLYGON_THRESHOLD)
                model.setUnclipRatio(TEXT_DB_UNCLIP_RATIO).setMaxCandidates(TEXT_DB_MAX_CANDIDATES)
                model.setInputMean((123.675, 116.28, 103.53))
                model.setInputScale(1.0 / 255.0 / numpy.array([0.229, 0.224, 0.225]))
                _text_detector_state["loaded"] = (cv2, numpy, model)
            except Exception as exc:
                _text_detector_state["loaded"] = None
                logger.warning(
                    "nasa text filter is unavailable, images are not checked for text "
                    f"(install it with `uv sync --extra face`): error={type(exc).__name__}, detail={exc}"
                )
        return _text_detector_state["loaded"]


def text_filter_active() -> bool:
    """nasa_text_filter 开启并且 OpenCV 可用。"""
    return text_filter_enabled() and _text_detector() is not None


def crop_text_lines(image_path: str, video_aspect: VideoAspect) -> list[dict] | None:
    """
    返回图片上与居中裁剪区域相交的文字行 [{"score", "height"}]（height 是占裁剪高度的比例）；
    检测器不可用或读取失败时返回 None。

    只计置信度不低于 TEXT_SCORE_THRESHOLD、外接矩形宽高比不低于 TEXT_MIN_ASPECT 的文字行。
    裁剪区域按图片自身尺寸计算，因此预览图和原图给出相同的区域。
    """
    loaded = _text_detector()
    if loaded is None:
        return None
    cv2, numpy, model = loaded
    try:
        aspect = VideoAspect(video_aspect)
        stat = os.stat(image_path)
        key = (os.path.abspath(image_path), stat.st_mtime, stat.st_size, aspect.value)
        if key in _text_lines:
            return _text_lines[key]
        image = cv2.imdecode(numpy.fromfile(image_path, dtype=numpy.uint8), cv2.IMREAD_COLOR)
        if image is None:
            raise ValueError("unreadable image")
        height, width = image.shape[:2]
        scale = TEXT_INPUT_LONG_EDGE / max(width, height)
        size = (max(32, round(width * scale / 32) * 32), max(32, round(height * scale / 32) * 32))
        with _text_lock:
            model.setInputSize(size)
            polygons, scores = model.detect(image)
        crop_width, crop_height = _center_crop_size(width, height, aspect)
        left, top = (width - crop_width) // 2, (height - crop_height) // 2
        lines = []
        for polygon, score in zip(polygons, scores):
            x, y, box_width, box_height = cv2.boundingRect(numpy.asarray(polygon, dtype=numpy.int32))
            if score < TEXT_SCORE_THRESHOLD or box_height <= 0 or box_width / box_height < TEXT_MIN_ASPECT:
                continue
            if x + box_width <= left or x >= left + crop_width or y + box_height <= top or y >= top + crop_height:
                continue
            lines.append({"score": round(float(score), 3), "height": round(box_height / crop_height, 4)})
        _text_lines[key] = lines
        return lines
    except Exception as exc:
        logger.warning(f"nasa text check failed: error={type(exc).__name__}, detail={exc}")
        return None


def _preview_url(item: MaterialInfo) -> str:
    source_info = item.source_info if isinstance(item.source_info, dict) else {}
    preview = _public_asset_url(source_info.get("preview"))
    if preview:
        return preview
    asset_id = str(source_info.get("asset_id") or "")
    return f"https://images-assets.nasa.gov/image/{quote(asset_id)}/{quote(asset_id)}~small.jpg" if asset_id else ""


def _text_result_path(preview_url: str, video_aspect: VideoAspect, save_dir: str) -> str:
    """
    文字检查结果的缓存文件。文件名由预览图地址、FILTER_VERSION、目标比例（决定裁剪区域）
    和全部检测参数决定，其中任何一项改变都会重新检测。
    """
    settings = {
        "filter_version": FILTER_VERSION,
        "preview": preview_url,
        "aspect": VideoAspect(video_aspect).value,
        "model": os.path.basename(TEXT_MODEL_FILE),
        "input_long_edge": TEXT_INPUT_LONG_EDGE,
        "score_threshold": TEXT_SCORE_THRESHOLD,
        "min_aspect": TEXT_MIN_ASPECT,
        "db": [TEXT_DB_BINARY_THRESHOLD, TEXT_DB_POLYGON_THRESHOLD, TEXT_DB_UNCLIP_RATIO, TEXT_DB_MAX_CANDIDATES],
    }
    digest = hashlib.sha256(json.dumps(settings, sort_keys=True).encode()).hexdigest()
    return os.path.join(save_dir, f"nasa-text-{digest}.json")


def _load_text_result(path: str) -> list[dict] | None:
    """读取缓存的文字行；文件不存在或内容不完整时返回 None（重新检测）。"""
    try:
        with open(path, encoding="utf-8") as cached:
            lines = json.load(cached)["lines"]
    except FileNotFoundError:
        return None
    except Exception as exc:
        logger.warning(f"discarding invalid nasa text check cache: {path}, error={type(exc).__name__}")
        return None
    numeric = (int, float)
    if isinstance(lines, list) and all(
        isinstance(line, dict) and isinstance(line.get("score"), numeric) and isinstance(line.get("height"), numeric)
        for line in lines
    ):
        return lines
    logger.warning(f"discarding invalid nasa text check cache: {path}")
    return None


def _save_text_result(path: str, lines: list[dict]) -> None:
    """先写临时文件再整体替换，并发任务不会读到写了一半的结果；写入失败只影响缓存。"""
    temp_path = ""
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", prefix=".nasa-text-", suffix=".json", dir=os.path.dirname(path), delete=False
        ) as temp:
            temp_path = temp.name
            json.dump({"lines": lines}, temp)
        os.replace(temp_path, path)
        temp_path = ""
    except OSError as exc:
        logger.warning(f"failed to cache nasa text check: error={type(exc).__name__}, detail={exc}")
    finally:
        if temp_path and os.path.exists(temp_path):
            os.remove(temp_path)


def _preview_text_lines(preview_url: str, video_aspect: VideoAspect, save_dir: str) -> list[dict] | None:
    """返回预览图裁剪区域里的文字行：先查结果缓存，未命中时下载预览图并检测，结果写入缓存。"""
    result_path = _text_result_path(preview_url, video_aspect, save_dir)
    lines = _load_text_result(result_path)
    if lines is not None:
        return lines
    preview_path = _download_image(preview_url, save_dir)
    lines = crop_text_lines(preview_path, video_aspect) if preview_path else None
    # 下载或检测失败（None）不缓存，下次任务再试。
    if lines is not None:
        _save_text_result(result_path, lines)
    return lines


def screen_text(items: List[MaterialInfo], video_aspect: VideoAspect, search_term: str = "") -> List[MaterialInfo]:
    """
    按排序依次检查 NASA 候选的预览图，淘汰裁剪区域里有文字的候选，返回剩下的候选（顺序不变）。

    通过 TEXT_CHECKED_CANDIDATES 个后停止检查，后面的候选原样保留。预览图缺失、下载或检测
    失败时保留候选：无法判断时不淘汰。预览图和检测结果都缓存在素材缓存目录，重复任务不会
    再次下载预览图，也不会再次检测。
    """
    save_dir = utils.storage_dir("cache_videos")
    os.makedirs(save_dir, exist_ok=True)
    kept, passed = [], 0
    for index, item in enumerate(items):
        if passed >= TEXT_CHECKED_CANDIDATES:
            kept.extend(items[index:])
            break
        preview = _preview_url(item) if item.provider == "nasa" else ""
        lines = _preview_text_lines(preview, video_aspect, save_dir) if preview else None
        if lines:
            source_info = item.source_info if isinstance(item.source_info, dict) else {}
            tallest = max(lines, key=lambda line: line["height"])
            logger.info(
                f"nasa text filter: skipped {source_info.get('asset_id') or item.url} for {search_term!r}, "
                f"text lines in the crop area={len(lines)}, tallest={tallest['height']:.1%} of the crop height, "
                f"score={tallest['score']:.2f}"
            )
            continue
        kept.append(item)
        passed += 1
    return kept


# ---------------------------------------------------------------- 署名
# 使用条款要求注明 NASA 为来源，同时不得暗示 NASA 认可或参与制作。下面的文字只说明
# 图片来源（"Görüntüler: NASA" / "Images: NASA"），不使用 "NASA 呈现"、"与 NASA 合作"
# 一类说法。

CREDITS_FILE = "credits.txt"
CREDITS_HEADING = "Görsel kaynakları / Image credits"
CREDITS_FOOTER = "Görüntüler: NASA ve ortakları / Images: NASA and partners"
CREDITS_DISCLAIMER = (
    "Bu video NASA tarafından onaylanmamıştır. / This video is not endorsed by NASA."
)
# credits.txt 每行开头的素材源名称。Pixabay 等库存素材源只在作为 NASA 的后备
# （video_source_fallback）时出现；它们的许可不要求署名，这里写作者名和素材页面。
CREDIT_SOURCE_LABELS = {"nasa": "NASA", "pixabay": "Pixabay", "pexels": "Pexels", "coverr": "Coverr"}
# 屏幕署名最多列出的机构数，其余写成 "ve diğerleri" / "and others"。
CREDIT_OVERLAY_MAX_AGENCIES = 2
# 署名里两个以上大写字母的词视为机构缩写（NASA、ESA、JPL、STScI…）；人名不会命中。
_AGENCY = re.compile(r"\b[A-Z][A-Za-z]*[A-Z][A-Za-z]*\b")


def _is_turkish(language: str) -> bool:
    return str(language or "").strip().lower().startswith("tr")


def used_sources(material_sources: Any) -> list[dict]:
    """返回实际使用的 NASA 图片记录，按出现顺序去重。"""
    sources, seen = [], set()
    for record in material_sources or []:
        if not isinstance(record, dict) or str(record.get("provider") or "").lower() != "nasa":
            continue
        key = record.get("asset_id") or record.get("source_page") or record.get("local_file")
        if key in seen:
            continue
        seen.add(key)
        sources.append(record)
    return sources


def _credited_sources(material_sources: Any) -> list[dict]:
    """credits.txt 列出的素材：NASA 图片和后备库存素材源的视频，按出现顺序去重。"""
    sources, seen = [], set()
    for record in material_sources or []:
        if not isinstance(record, dict):
            continue
        provider = str(record.get("provider") or "").lower()
        if provider not in CREDIT_SOURCE_LABELS:
            continue
        key = (provider, record.get("asset_id") or record.get("source_page") or record.get("local_file"))
        if key in seen:
            continue
        seen.add(key)
        sources.append(record)
    return sources


def _credit_line(record: dict) -> str:
    """一条署名：NASA 图片用署名；Pixabay 等不要求署名的素材源写作者名（没有时写素材源）。"""
    provider = str(record.get("provider") or "").lower()
    label = CREDIT_SOURCE_LABELS[provider]
    if provider == "nasa":
        return f"[{label}] {record.get('credit') or DEFAULT_CREDIT}"
    creator = record.get("creator") if isinstance(record.get("creator"), dict) else {}
    return f"[{label}] {str(creator.get('name') or '').strip() or label}"


def credits_text(material_sources: Any) -> str:
    """
    credits.txt 的内容；没有 NASA 图片时为空。

    每行写明素材源（[NASA]、[Pixabay]…）和署名，下一行是素材页面。有后备素材源时
    它的视频也列出，结尾的来源行加上该素材源。
    """
    if not used_sources(material_sources):
        return ""
    lines = [CREDITS_HEADING, "=" * len(CREDITS_HEADING), ""]
    other_labels = []
    for index, record in enumerate(_credited_sources(material_sources), start=1):
        provider = str(record.get("provider") or "").lower()
        asset_id = str(record.get("asset_id") or "")
        page = record.get("source_page") or (
            DETAILS_PAGE.format(nasa_id=quote(asset_id)) if asset_id and provider == "nasa" else ""
        )
        lines.append(f"{index}. {_credit_line(record)}")
        if page:
            lines.append(f"   {page}")
        if provider != "nasa" and CREDIT_SOURCE_LABELS[provider] not in other_labels:
            other_labels.append(CREDIT_SOURCE_LABELS[provider])
    footer = CREDITS_FOOTER
    if other_labels:
        others = ", ".join(other_labels)
        footer = f"Görüntüler: NASA ve ortakları, {others} / Images: NASA and partners, {others}"
    lines += ["", footer, CREDITS_DISCLAIMER, ""]
    return "\n".join(lines)


def write_credits_file(task_dir: str, material_sources: Any) -> str:
    """有 NASA 图片时写入 credits.txt 并返回路径；没有时删除旧文件并返回空字符串。"""
    credits_path = os.path.join(task_dir, CREDITS_FILE)
    text = credits_text(material_sources)
    try:
        if not text:
            if os.path.isfile(credits_path):
                os.remove(credits_path)
            return ""
        temp_path = f"{credits_path}.part"
        with open(temp_path, "w", encoding="utf-8", newline="\n") as file:
            file.write(text)
        os.replace(temp_path, credits_path)
        return credits_path
    except OSError as exc:
        logger.warning(f"failed to write nasa credits: error={type(exc).__name__}, detail={exc}")
        return ""


def credit_agencies(material_sources: Any) -> list[str]:
    """署名中出现的机构缩写，NASA 在前，其余按出现的图片数和先后排序。"""
    counts: Counter = Counter()
    first_seen: dict[str, int] = {}
    for record in used_sources(material_sources):
        for agency in dict.fromkeys(_AGENCY.findall(str(record.get("credit") or DEFAULT_CREDIT))):
            counts[agency] += 1
            first_seen.setdefault(agency, len(first_seen))
    counts["NASA"] += 0
    first_seen.setdefault("NASA", -1)
    return sorted(counts, key=lambda agency: (agency != "NASA", -counts[agency], first_seen[agency]))


def overlay_credit_text(material_sources: Any, language: str = "") -> str:
    """屏幕署名的一行字，例如 "Görüntüler: NASA, ESA ve diğerleri"；没有 NASA 图片时为空。"""
    if not used_sources(material_sources):
        return ""
    agencies = credit_agencies(material_sources)
    shown = ", ".join(agencies[:CREDIT_OVERLAY_MAX_AGENCIES])
    if _is_turkish(language):
        return f"Görüntüler: {shown}" + (" ve diğerleri" if len(agencies) > CREDIT_OVERLAY_MAX_AGENCIES else "")
    return f"Images: {shown}" + (" and others" if len(agencies) > CREDIT_OVERLAY_MAX_AGENCIES else "")


def social_attribution(material_sources: Any, language: str = "") -> str:
    """发布文案末尾的一行来源说明；没有 NASA 图片时为空。"""
    if not used_sources(material_sources):
        return ""
    if _is_turkish(language):
        return "Görüntüler: NASA ve ortakları (images.nasa.gov)"
    return "Images: NASA and partners (images.nasa.gov)"
