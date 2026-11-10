"""
NASA's Scientific Visualization Studio (svs.gsfc.nasa.gov) as a stock source: "nasa_svs".

Renders computed from mission data (LRO's elevation and albedo maps, eclipse and libration
sequences), shown in the real-footage channel with an on-screen label ("NASA SVS · LRO
verisinden görselleştirme", ``scene_label``) and credited to the Studio.

- Search: ``/api/search/?search=…&sort_by_1=relevance&media=movies`` (no key). Only
  ``Visualization`` pages are data renders: ``Animation`` pages (the Conceptual Image
  Lab's artist renders, studio "cil") and ``Produced Video`` pages (narrated pieces with
  people, music and titles) are skipped, and so are pages whose title, keywords or
  description call them a concept, an artist's rendering or an illustration.
- Relevance: tags = title, keywords and missions; the description's opening as low-weight
  tags (as for NASA), so the anchor rule and relevance work as for the other sources.
- Media: the page's MP4 movies; a native 9:16 version first (portrait videos), else the
  best 16:9 version up to 2160 lines. Versions named fancy, label, title, music, YT,
  ProRes… are skipped (graphics, licensed music), "plain"/"nocard"/"nolro" preferred. The
  JSON has neither durations nor file sizes and its sizes can be wrong, so the chosen
  file is probed remotely (ffmpeg header read; ``ffmpeg_inputs.remote_input`` on
  svs.gsfc.nasa.gov only). The smallest MP4 of the same orientation is the analysis copy.
- Clean window, ranged extraction, audio: as for NASA videos (``nasa_video``; the clip
  is cut with -ss/-t and has no audio, so licensed music never comes along).
- Credit: the page's "Please give credit for this item to:" line (HTML), else the
  studio's name; the people with their roles from the page JSON.
"""

from __future__ import annotations

import html
import re
import subprocess
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from typing import Any, List
from urllib.parse import urlsplit

import requests
from loguru import logger

from app.config import config
from app.models.schema import MaterialInfo, VideoAspect
from app.services import nasa, nasa_video, task_materials
from app.utils import ffmpeg_inputs, utils

PROVIDER = "nasa_svs"
HOST = nasa_video.SVS_HOST
API_ROOT = f"https://{HOST}/api"
PAGE_URL = f"https://{HOST}/{{page_id}}/"
# Changes when the selection rules change: the search cache keys older results out.
FILTER_VERSION = 1
SEARCH_LIMIT = 30
MAX_DETAILED = 8
DETAIL_WORKERS = 4
TIMEOUT = 30
MIN_SHORT_SIDE = 720
MAX_LONG_SIDE = 3840
DATA_PAGE_TYPES = frozenset({"Visualization"})
CONCEPT_STUDIOS = frozenset({"cil"})
# A page that says it is not data: an artist's concept, an illustration, a conceptual animation.
_CONCEPT = re.compile(
    r"\bartist(?:'s|’s|s)?\s+(?:concept|conception|impression|illustration|rendering)\b|\bconceptual\b"
    r"|\bconcept (?:animation|art|video)\b|\billustrat(?:ion|ed)\b|\bbeauty shots?\b|\bcgi\b", re.I)
# Movie versions to skip: graphics or text on the picture, music, captions, editing masters.
_DIRTY = re.compile(r"fancy|label|title|caption|subtitle|text|music|youtube|yt(?=[_\-.\d])|narrat|prores"
                    r"|master|promo|_hq\b|graphic|legend|overlay|scale", re.I)
_CLEAN = re.compile(r"plain|nocard|nolro|nolabel|notext|clean", re.I)
_DURATION = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
_VIDEO_SIZE = re.compile(r"Video:.*?(\d{2,5})x(\d{2,5})")
_CREDIT_LINE = re.compile(r"Please give credit for this item to:\s*(?:<[^>]*>\s*)*([^<]+)", re.I)
STUDIO_CREDITS = {
    "svs": "NASA's Scientific Visualization Studio",
    "gms": "NASA's Goddard Space Flight Center",
    "cil": "NASA's Goddard Space Flight Center Conceptual Image Lab",
}
DEFAULT_CREDIT = STUDIO_CREDITS["svs"]
SITE = f"https://{HOST}"


# ---------------------------------------------------------------- requests


def _get(url: str, params: dict | None = None, *, as_json: bool = True) -> Any:
    try:
        response = requests.get(url, params=params, timeout=TIMEOUT, proxies=config.proxy or None,
                                headers={"User-Agent": "MoneyPrinterTurbo"})
    except requests.RequestException as exc:
        logger.error(f"nasa svs request failed: {type(exc).__name__}: {exc}")
        return None
    if response.status_code == 429:
        logger.error("nasa svs rate limit exceeded: status=429")
        from app.services import source_chain

        source_chain.note_rate_limited(PROVIDER)
        return None
    if response.status_code >= 400:
        logger.error(f"nasa svs request failed: status={response.status_code}")
        return None
    if not as_json:
        return response.text
    try:
        return response.json()
    except ValueError:
        logger.error("nasa svs returned a non-JSON response")
        return None


def https(url: Any) -> str:
    """SVS returns http:// media links; https works and is the only scheme the remote input allows."""
    parts = urlsplit(str(url or ""))
    if parts.hostname != HOST:
        return ""
    return parts._replace(scheme="https").geturl().replace(" ", "%20")


# ---------------------------------------------------------------- the page


def _titles(values) -> list[str]:
    titles = []
    for value in values or []:
        title = value.get("title") if isinstance(value, dict) else value
        if isinstance(title, str) and title.strip():
            titles.append(title.strip())
    return titles


def concept_reason(page: dict) -> str | None:
    """Why a page is not a data visualization (its type, studio or wording), if it is not."""
    page_type = str(page.get("page_type") or page.get("result_type") or "")
    if page_type and page_type not in DATA_PAGE_TYPES:
        return f"type:{page_type}"
    if str(page.get("studio") or "").lower() in CONCEPT_STUDIOS:
        return f"studio:{page.get('studio')}"
    text = " ".join([str(page.get("title") or ""), " ".join(_titles(page.get("keywords"))),
                     nasa._plain_text(page.get("description"))[:600]])
    match = _CONCEPT.search(text)
    return f"concept:{match.group(0).lower()}" if match else None


def mission_name(page: dict) -> str:
    """The mission or spacecraft the data came from, short ("LRO"), or "" when the page names none."""
    names = [title.split(" (")[0].strip() for title in _titles(page.get("missions"))]
    acronym = next((name for name in names if re.fullmatch(r"[A-Z0-9][A-Z0-9\-]{1,9}", name)), "")
    return acronym or (names[0] if names and len(names[0]) <= 24 else "")


def scene_label(page: dict) -> dict:
    """The on-screen label of the page's scenes, in Turkish and English."""
    mission = mission_name(page)
    if mission:
        return {"tr": f"NASA SVS · {mission} verisinden görselleştirme", "en": f"NASA SVS · {mission} data visualization"}
    return {"tr": "NASA SVS · veri görselleştirmesi", "en": "NASA SVS · data visualization"}


def movies(page: dict) -> list[dict]:
    """The page's MP4 movies: {"url", "filename", "width", "height"} (https)."""
    found, seen = [], set()
    for group in page.get("media_groups") or []:
        for entry in (group or {}).get("items") or []:
            instance = (entry or {}).get("instance") or {}
            if instance.get("media_type") != "Movie":
                continue
            name = str(instance.get("filename") or "")
            url = https(instance.get("url"))
            if not url or not name.lower().endswith((".mp4", ".m4v")) or url in seen:
                continue
            seen.add(url)
            found.append({"url": url, "filename": name, "width": int(instance.get("width") or 0),
                          "height": int(instance.get("height") or 0)})
    return found


def choose_media(candidates: list[dict], video_aspect: VideoAspect) -> tuple[dict, dict] | None:
    """
    (the file to cut the clip from, the analysis copy), or None.

    A native 9:16 version comes first for portrait videos (it fills the frame without a
    crop); otherwise the largest 16:9 version up to 2160 lines. Graphics, text, music and
    master versions are skipped; "plain" versions win ties.
    """
    usable = [m for m in candidates if not _DIRTY.search(m["filename"]) and m["width"] and m["height"]
              and min(m["width"], m["height"]) >= 360 and max(m["width"], m["height"]) <= MAX_LONG_SIDE]
    if not usable:
        return None
    portrait = VideoAspect(video_aspect) == VideoAspect.portrait

    def rank(movie):
        vertical = movie["height"] > movie["width"]
        short_side = min(movie["width"], movie["height"])
        return (
            vertical != portrait,  # the output's orientation first
            short_side < MIN_SHORT_SIDE,  # then at least 720 lines
            not _CLEAN.search(movie["filename"]),  # then a plain version
            -short_side,  # then the sharpest
        )

    best = sorted(usable, key=rank)[0]
    if min(best["width"], best["height"]) < MIN_SHORT_SIDE:
        return None
    # The analysis copy is the smallest file of the same version (the name without its
    # resolution): another version ("tour_nodata") may differ in what is drawn on it.
    family = [m for m in usable if _family(m["filename"]) == _family(best["filename"])]
    analysis = min(family, key=lambda m: min(m["width"], m["height"])) if family else best
    return best, analysis


def _family(filename: str) -> str:
    stem = filename.rsplit(".", 1)[0].lower()
    stem = re.sub(r"\d{3,4}x\d{3,4}|\d{3,4}p(?:\d{2})?|(?:^|[_\-])(?:4k|uhd|hd)(?=$|[_\-])", "", stem)
    return re.sub(r"[_\-.]+", "_", stem).strip("_")


def probe(url: str) -> tuple[float | None, int | None, int | None]:
    """(duration, width, height) of a remote movie from its header (ffmpeg reads the moov box only)."""
    command = [utils.get_ffmpeg_binary(), "-hide_banner", "-nostdin", *ffmpeg_inputs.remote_input(url, (HOST,))]
    try:
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                timeout=60)
    except subprocess.TimeoutExpired:
        return None, None, None
    duration = _DURATION.search(result.stderr or "")
    size = _VIDEO_SIZE.search(result.stderr or "")
    seconds = (int(duration.group(1)) * 3600 + int(duration.group(2)) * 60 + float(duration.group(3))
               if duration else None)
    return seconds, (int(size.group(1)) if size else None), (int(size.group(2)) if size else None)


def page_credit(page_id: Any, page: dict) -> str:
    """The page's credit line ("Please give credit for this item to: …"), else the studio's name."""
    text = _get(PAGE_URL.format(page_id=page_id), as_json=False)
    match = _CREDIT_LINE.search(text or "")
    if match:
        credit = " ".join(html.unescape(match.group(1)).split())
        if credit:
            return credit[:200]
    return STUDIO_CREDITS.get(str(page.get("studio") or "").lower(), DEFAULT_CREDIT)


def people(page: dict, limit: int = 3) -> list[str]:
    """ "Role: Name" for the first people credited on the page."""
    entries = []
    for credit in page.get("credits") or []:
        names = [p.get("name") for p in (credit or {}).get("people") or [] if isinstance(p, dict) and p.get("name")]
        if names and credit.get("role"):
            entries.append(f"{credit['role']}: {', '.join(names)}")
    return entries[:limit]


def relevance_tags(page: dict) -> list[str]:
    """Tags for material_tag_relevance: keywords and missions, the title last (as NASA's)."""
    tags = [*_titles(page.get("keywords")), *_titles(page.get("missions"))]
    title = str(page.get("title") or "").strip()
    if title:
        tags.append(title)
    return list(dict.fromkeys(tag[:100] for tag in tags))[:50]


# ---------------------------------------------------------------- search


def _detailed(result: dict, search_term: str, minimum_duration: float,
              video_aspect: VideoAspect) -> tuple[MaterialInfo | None, str]:
    page_id = result.get("id")
    page = _get(f"{API_ROOT}/{page_id}/")
    if not isinstance(page, dict):
        return None, "no_page"
    reason = concept_reason(page)
    if reason:
        return None, reason.split(":")[0]
    if nasa.illustration_risk({"title": page.get("title"), "keywords": _titles(page.get("keywords")),
                               "description": page.get("description")}):
        return None, "concept"
    chosen = choose_media(movies(page), video_aspect)
    if chosen is None:
        return None, "no_media"
    best, analysis = chosen
    duration, width, height = probe(best["url"])
    if not duration:
        return None, "probe_failed"
    if duration < minimum_duration:
        return None, "too_short"
    width, height = width or best["width"], height or best["height"]
    description = {"description": page.get("description")}
    main_image = https(((page.get("main_image") or {}).get("url")) if isinstance(page.get("main_image"), dict)
                       else "")
    item = MaterialInfo()
    item.provider = PROVIDER
    item.url = best["url"]
    item.duration = int(duration)
    item.source_info = {
        "provider": PROVIDER,
        "media_type": "video",
        "search_term": search_term,
        "asset_id": str(page_id),
        "source_page": PAGE_URL.format(page_id=page_id),
        "credit": page_credit(page_id, page),
        "creator": {"name": "; ".join(people(page))} if people(page) else None,
        "preview": main_image,
        "rendition": {"id": best["filename"][:80], "width": width, "height": height},
        "video": {"duration": round(duration, 2), "width": width, "height": height,
                  "renditions": {"preview": analysis["url"], "large": best["url"], "orig": best["url"]}},
        "tags": relevance_tags(page),
        "description_tags": nasa.description_tags(description),
        "scene_label": scene_label(page),
        "ai_generated": False,
    }
    if item.source_info["creator"] is None:
        item.source_info.pop("creator")
    return item, ""


def search_videos(
    search_term: str,
    minimum_duration: int,
    video_aspect: VideoAspect = VideoAspect.portrait,
    *,
    crop_to_aspect: bool = False,
) -> List[MaterialInfo]:
    """SVS data visualizations for a term; the clean window is decided later (``nasa_video.screen``)."""
    del crop_to_aspect
    logger.info(f"searching videos on nasa svs: term={search_term!r}")
    body = _get(f"{API_ROOT}/search/", {"search": search_term, "sort_by_1": "relevance", "limit": SEARCH_LIMIT,
                                        "media": "movies"})
    results = body.get("results") if isinstance(body, dict) else None
    if not isinstance(results, list):
        return []
    skipped: Counter = Counter()
    survivors = []
    for result in results:
        if not isinstance(result, dict) or result.get("id") is None:
            skipped["invalid"] += 1
            continue
        reason = concept_reason(result)
        if reason:
            skipped[reason.split(":")[0]] += 1
        else:
            survivors.append(result)
    checked = survivors[:MAX_DETAILED]
    with ThreadPoolExecutor(max_workers=DETAIL_WORKERS, thread_name_prefix="nasa-svs-detail") as executor:
        detailed = list(executor.map(
            lambda result: _detailed(result, search_term, float(minimum_duration), video_aspect), checked))
    items = []
    for item, reason in detailed:
        if item is None:
            skipped[reason] += 1
        else:
            items.append(item)
    logger.info(f"nasa svs search: term={search_term!r}, total={body.get('count')}, results={len(results)}, "
                f"checked={len(checked)}, kept={len(items)}, skipped={dict(skipped)}, "
                f"unchecked={len(survivors) - len(checked)}")
    task_materials.source_filtered(PROVIDER, search_term, dict(skipped), len(survivors) - len(checked))
    return items


def search_cache_variant() -> dict:
    """The selection rules' version and the settings they read, for the search cache key."""
    return {"filters": FILTER_VERSION, "illustrations": nasa.exclude_illustrations_enabled()}


def is_svs(record: Any) -> bool:
    return isinstance(record, dict) and str(record.get("provider") or "").lower() == PROVIDER
