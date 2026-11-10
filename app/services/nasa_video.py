"""
NASA videos as stock footage (``nasa_media_types`` includes "video").

Search: the NASA Image and Video Library API with ``media_type=video``. The text level
filters of the images run on every result (identifiable person, text and graphics,
third party copyright, non-free credit), plus a block list of programme formats that
are talks, briefings or hosted shows (``BLOCKED_FORMATS``, title, keywords and the
asset's own title) and limits on length and frame size. The asset manifest gives the
renditions (``~orig``, ``~large``, ``~medium``, ``~small``, ``~preview``, ``~mobile``),
the metadata the duration and the frame size.

Clean window: a video is used only for a stretch of at least the clip length that is
one shot with no text, no face and no black frame. The low resolution copy
(``~preview``, 480x270, about 0.6 Mbit/s) is read in chunks of ``CHUNK_SECONDS`` up to
``ANALYZE_SECONDS``, each chunk with one ffmpeg command that writes a frame every
``SAMPLE_SECONDS`` and reports ffmpeg's shot changes (scene score). Each frame goes
through the text detector, on the part of the frame the fit mode shows, and the YuNet
face detector. The earliest clean window is chosen (``choose_window``); a video
without one is turned down and the next candidate is tried. The decision is cached by
asset, clip length and shown area.

Extraction: only the chosen window is read from the high resolution copy, with
``ffmpeg -ss/-t`` straight from the server (HTTP range requests; the bytes read are
recorded). That is the application's one remote ffmpeg input:
``ffmpeg_inputs.remote_input`` allows https (with tls and tcp under it) on NASA's asset
host for that input alone and forces the MP4 demuxer.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from typing import Any, List
from urllib.parse import quote, urlsplit

from loguru import logger
from PIL import Image, ImageStat

from app.models.schema import MaterialInfo, VideoAspect
from app.services import nasa, task_materials
from app.utils import ffmpeg_inputs, utils

# Changes when the window rules change: older cached decisions are not reused.
ANALYSIS_VERSION = 1
# Recorded programmes rather than footage: hosted series, briefings, talks, coverage.
BLOCKED_FORMATS = (
    "we asked a nasa expert", "science live", "media briefing", "news conference", "press conference",
    "briefing", "town hall", "webinar", "interview", "panel discussion", "q&a", "live coverage",
    "launch coverage", "live broadcast", "what's up", "whats up", "this week @nasa", "this week at nasa",
    "nasa 360", "nasa explorers", "space to ground", "behind the spacecraft", "nasa edge", "podcast",
    "nasa minute", "explainer", "ask nasa", "asknasa", "teaser",
    "lecture", "seminar", "colloquium", "symposium", "keynote", "ceremony", "trailer",
)
VIDEO_MAX_DURATION = 900  # seconds: longer uploads are lectures, briefings and live streams
VIDEO_MIN_HEIGHT = 720
ANALYZE_SECONDS = 120  # the earliest clean window is looked for in the first two minutes
CHUNK_SECONDS = 30
SAMPLE_SECONDS = 1
SCENE_THRESHOLD = 0.3
FRAME_WIDTH = 960  # frames are enlarged for the detectors; faces in a 480 px frame are small
BLACK_MAX_LUMA = 48  # a frame whose brightest pixel is darker is black (fade, leader)
FLAT_MAX_STDDEV = 1.5  # a frame of one colour
ANALYSIS_RENDITIONS = ("preview", "small", "mobile", "medium")
# The copy the window is cut from. ~large is a 1080p-class copy (about 5 Mbit/s) and
# enough for blur and contain (the whole frame on a canvas 1080 px wide) and for any
# source of at most 1080p (its ~orig has the same pixels at 3-4 times the bitrate).
# Cover of a larger source (4K: the 9:16 strip needs no enlarging) reads ~orig, at
# 40-85 Mbit/s measured.
LARGE_COPY_HEIGHT = 1080
EXTRACT_RENDITIONS = ("large", "orig", "medium")
EXTRACT_RENDITIONS_FULL = ("orig", "large", "medium")
EXTRACT_MAX_WIDTH = {"blur": 1920, "contain": 1920}
ASSET_HOST = "images-assets.nasa.gov"
# The hosts whose videos are read with range requests (ffmpeg_inputs.remote_input): NASA's
# image and video library, and the Scientific Visualization Studio (nasa_svs), whose
# renders are up to gigabytes and serve byte ranges as well.
SVS_HOST = "svs.gsfc.nasa.gov"
REMOTE_HOSTS = (ASSET_HOST, SVS_HOST)
# Sources whose candidates are videos handled here (clean window, ranged window).
VIDEO_PROVIDERS = ("nasa", "nasa_svs")
_MAX_DETAILED = 12
_DETAIL_WORKERS = 4
_RENDITION = re.compile(r"~(orig|large|medium|small|preview|mobile)\.mp4$", re.I)
_SHOWINFO = re.compile(r"\bn:\s*(\d+)\s+pts:\s*-?\d+\s+pts_time:([\d.]+)")
_BYTES_READ = re.compile(r"Statistics: (\d+) bytes read")
_detector_lock = threading.Lock()


# ---------------------------------------------------------------- search


def blocked_format(data: dict, metadata: dict | None = None) -> str | None:
    """The programme format a result belongs to (a talk, a briefing, a hosted show…), if any."""
    text = " ".join([
        nasa._text(data.get("title")), nasa._text(data.get("keywords")),
        nasa._text((metadata or {}).get("AVAIL:Title")),
    ]).lower()
    return next((name for name in BLOCKED_FORMATS if name in text), None)


def renditions(files: List[Any]) -> dict[str, str]:
    """The MP4 renditions of an asset by name (``orig``, ``large``…), as https URLs."""
    found: dict[str, str] = {}
    for href in files:
        url = nasa._public_asset_url(href)
        match = _RENDITION.search(urlsplit(url).path) if url else None
        if match:
            found.setdefault(match.group(1).lower(), url)
    return found


def _seconds(value: Any) -> float | None:
    """"0:01:33", "1:02:03.5", "12.5 s" or a number → seconds."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if value > 0 else None
    text = str(value or "").strip().lower()
    try:
        if ":" in text:
            seconds = 0.0
            for part in text.split(":"):
                seconds = seconds * 60 + float(part)
            return seconds or None
        match = re.fullmatch(r"([\d.]+)\s*s?", text)
        return (float(match.group(1)) or None) if match else None
    except ValueError:
        return None


def video_info(metadata: dict | None) -> tuple[float | None, int | None, int | None]:
    """(duration in seconds, width, height) from the asset metadata."""
    metadata = metadata if isinstance(metadata, dict) else {}
    duration = None
    for key in ("QuickTime:Duration", "QuickTime:MediaDuration", "QuickTime:TrackDuration", "Composite:Duration"):
        duration = _seconds(metadata.get(key))
        if duration:
            break
    width = height = None
    match = re.match(r"\s*(\d+)\s*[x×]\s*(\d+)", str(metadata.get("Composite:ImageSize") or ""))
    if match:
        width, height = int(match.group(1)), int(match.group(2))
    else:
        try:
            width = int(metadata.get("QuickTime:ImageWidth") or 0) or None
            height = int(metadata.get("QuickTime:ImageHeight") or 0) or None
        except (TypeError, ValueError):
            width = height = None
    return duration, width, height


def _detailed(data: dict, search_term: str, clip_seconds: float) -> tuple[MaterialInfo | None, str]:
    nasa_id = str(data["nasa_id"])
    asset = nasa._get_json(f"{nasa.API_ROOT}/asset/{quote(nasa_id)}")
    collection = asset.get("collection") if isinstance(asset, dict) else None
    files = [item.get("href") for item in (collection or {}).get("items") or [] if isinstance(item, dict)]
    copies = renditions(files)
    if not copies:
        return None, "no_video"
    metadata_url = next((nasa._public_asset_url(f) for f in files if str(f).endswith("/metadata.json")), "")
    metadata = nasa._get_json(metadata_url) if metadata_url else None
    metadata = metadata if isinstance(metadata, dict) else {}
    if blocked_format(data, metadata):
        return None, "format"
    # The asset's own title, keywords and description can differ from the search result's.
    if nasa.illustration_risk({"nasa_id": nasa_id, "title": metadata.get("AVAIL:Title"),
                               "keywords": metadata.get("AVAIL:Keywords"),
                               "description": metadata.get("AVAIL:Description")}):
        return None, "illustration"
    if nasa.third_party_copyright(data, metadata):
        return None, "copyright"
    credit = nasa.extract_credit(data, metadata)
    if not nasa.free_use_credit(credit):
        return None, "credit"
    duration, width, height = video_info(metadata)
    if not duration:
        return None, "no_duration"
    if duration < clip_seconds:
        return None, "too_short"
    if duration > VIDEO_MAX_DURATION:
        return None, "too_long"
    if not width or not height or min(width, height) < VIDEO_MIN_HEIGHT:
        return None, "resolution"
    if not any(name in copies for name in ANALYSIS_RENDITIONS):
        return None, "no_analysis_copy"
    preview = next((nasa._public_asset_url(f) for f in files if str(f).endswith("~small.jpg")), "")
    item = MaterialInfo()
    item.provider = "nasa"
    item.url = copies.get("large") or copies.get("orig") or next(iter(copies.values()))
    item.duration = int(duration)
    item.source_info = {
        "provider": "nasa",
        "media_type": "video",
        "search_term": search_term,
        "asset_id": nasa_id,
        "source_page": nasa.DETAILS_PAGE.format(nasa_id=quote(nasa_id)),
        "credit": credit,
        "preview": preview,
        # The frame size of the original; the window is cut from one of the copies.
        "rendition": {"id": "video", "width": width, "height": height},
        "video": {"duration": round(duration, 2), "width": width, "height": height, "renditions": copies},
        "tags": nasa.relevance_tags(data),
        "description_tags": nasa.description_tags(data),
        "ai_generated": False,
    }
    return item, ""


def search_videos(
    search_term: str,
    minimum_duration: int,
    video_aspect: VideoAspect = VideoAspect.portrait,
    *,
    crop_to_aspect: bool = False,
) -> List[MaterialInfo]:
    """
    NASA videos for a search term that passed the text level filters and the limits.

    Whether a video has a clean window is decided when it is about to be used
    (``screen``): that needs its frames. ``video_aspect`` and ``crop_to_aspect`` are
    accepted for the common search signature; the fit mode decides how a landscape
    video fills a portrait frame.
    """
    del video_aspect, crop_to_aspect
    if "video" not in nasa.configured_media_types():
        return []
    logger.info(f"searching videos on nasa: term={search_term!r}")
    body = nasa._get_json(
        f"{nasa.API_ROOT}/search", {"q": search_term, "media_type": "video", "page_size": nasa._SEARCH_PAGE_SIZE}
    )
    collection = body.get("collection") if isinstance(body, dict) else None
    raw_items = collection.get("items") if isinstance(collection, dict) else None
    if not isinstance(raw_items, list):
        if body is not None:
            logger.error("nasa video search returned an unsupported response")
        return []
    skipped: Counter = Counter()
    survivors = []
    for raw_item in raw_items:
        data = nasa._item_data(raw_item)
        if not data or not data.get("nasa_id") or str(data.get("media_type") or "") != "video":
            skipped["invalid"] += 1
        elif blocked_format(data):
            skipped["format"] += 1
        elif nasa.identifiable_person_risk(data):
            skipped["person"] += 1
        elif nasa.text_graphic_risk(data):
            skipped["text_graphic"] += 1
        elif nasa.illustration_risk(data):
            skipped["illustration"] += 1
        elif nasa.third_party_copyright(data):
            skipped["copyright"] += 1
        else:
            survivors.append(data)
    checked = survivors[:_MAX_DETAILED]
    with ThreadPoolExecutor(max_workers=_DETAIL_WORKERS, thread_name_prefix="nasa-video-detail") as executor:
        results = list(executor.map(lambda data: _detailed(data, search_term, float(minimum_duration)), checked))
    items = []
    for item, reason in results:
        if item is None:
            skipped[reason] += 1
        else:
            items.append(item)
    total_hits = (collection.get("metadata") or {}).get("total_hits")
    logger.info(
        f"nasa video search: term={search_term!r}, total_hits={total_hits}, results={len(raw_items)}, "
        f"checked={len(checked)}, kept={len(items)}, skipped={dict(skipped)}, "
        f"unchecked={len(survivors) - len(checked)}"
    )
    task_materials.source_filtered("nasa", search_term, dict(skipped), len(survivors) - len(checked))
    return items


def search_media(
    search_term: str,
    minimum_duration: int,
    video_aspect: VideoAspect = VideoAspect.portrait,
    *,
    crop_to_aspect: bool = False,
) -> List[MaterialInfo]:
    """
    The NASA source: videos and images in the order of ``nasa_media_types``.

    The order is also the type priority within the source (``media_type_rank``): with
    ["video", "image"] a term uses a video with a clean window first and an image when
    none of its videos has one.
    """
    items: List[MaterialInfo] = []
    for media_type in nasa.configured_media_types():
        if media_type == "video":
            items.extend(search_videos(search_term, minimum_duration, video_aspect, crop_to_aspect=crop_to_aspect))
        elif media_type == "image":
            items.extend(nasa.search_images(search_term, minimum_duration, video_aspect,
                                            crop_to_aspect=crop_to_aspect))
    return items


def is_video(item: Any) -> bool:
    source_info = getattr(item, "source_info", None)
    return (getattr(item, "provider", "") in VIDEO_PROVIDERS and isinstance(source_info, dict)
            and source_info.get("media_type") == "video")


def effective_fit_mode(item: Any, video_aspect: VideoAspect, fit_mode: Any, crop_to_aspect: bool) -> str:
    """How the video will fill the frame (``video._clip_fit_mode`` with its original size)."""
    from app.models.schema import VideoFitMode
    from app.services import video

    try:
        mode = VideoFitMode(getattr(fit_mode, "value", fit_mode) or VideoFitMode.cover.value)
    except ValueError:
        mode = VideoFitMode.cover
    info = ((getattr(item, "source_info", None) or {}).get("video") or {})
    width, height = int(info.get("width") or 1920), int(info.get("height") or 1080)
    target_width, target_height = VideoAspect(video_aspect).to_resolution()
    return video._clip_fit_mode(width, height, target_width, target_height, mode, bool(crop_to_aspect)).value


def media_type_rank(item: Any) -> int:
    """Type priority of a NASA candidate (its type's place in ``nasa_media_types``); 0 for other sources."""
    if getattr(item, "provider", "") != "nasa":
        return 0
    types = nasa.configured_media_types()
    media_type = "video" if is_video(item) else "image"
    return types.index(media_type) if media_type in types else len(types)


# ---------------------------------------------------------------- remote windows


def _run_ffmpeg(command: list[str], timeout: int = 600) -> subprocess.CompletedProcess:
    return subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=timeout)


def bytes_read(stderr: str) -> int:
    """Bytes ffmpeg read from its inputs (its "Statistics" lines, ``-loglevel verbose``)."""
    return sum(int(value) for value in _BYTES_READ.findall(stderr or ""))


def window_command(url: str, start: float, seconds: float, output: str, *, max_width: int | None = None) -> list[str]:
    """The ffmpeg command that cuts ``seconds`` from ``start`` of a remote NASA MP4 (H.264, no audio)."""
    command = [
        utils.get_ffmpeg_binary(), "-y", "-hide_banner", "-loglevel", "verbose", "-nostdin",
        "-ss", f"{max(0.0, float(start)):.3f}", "-t", f"{float(seconds):.3f}",
        *ffmpeg_inputs.remote_input(url, REMOTE_HOSTS),
        "-map", "0:v:0", "-an",
    ]
    if max_width:
        command += ["-vf", f"scale='min({int(max_width)},iw)':-2"]
    command += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p",
                "-movflags", "+faststart", output]
    return command


def fetch_window(url: str, start: float, seconds: float, output: str, *, max_width: int | None = None) -> int:
    """Write the window to ``output``; returns the bytes read from the server."""
    temp = f"{output}.part.mp4"
    result = _run_ffmpeg(window_command(url, start, seconds, temp, max_width=max_width))
    if result.returncode != 0 or not os.path.isfile(temp) or os.path.getsize(temp) == 0:
        if os.path.exists(temp):
            os.remove(temp)
        detail = " ".join(line for line in (result.stderr or "").splitlines() if "error" in line.lower())[-300:]
        raise RuntimeError(f"nasa video window failed: {detail or result.returncode}")
    os.replace(temp, output)
    return bytes_read(result.stderr)


def chunk_command(url: str, start: float, seconds: float, pattern: str) -> list[str]:
    """
    One remote read of the analysis copy: a frame every ``SAMPLE_SECONDS`` (JPEG files
    matching ``pattern``) and the shot changes (showinfo lines of the frames whose
    scene score is above ``SCENE_THRESHOLD``). The first frame always passes the
    selection (time 0 of the read, not a cut): a read without any cut would otherwise
    leave the second output empty, and ffmpeg fails on an output that got no frames.
    """
    graph = (
        f"[0:v:0]split=2[a][b];[a]fps=1/{SAMPLE_SECONDS},scale={FRAME_WIDTH}:-2[frames];"
        f"[b]select='eq(n,0)+gt(scene,{SCENE_THRESHOLD})',showinfo[cuts]"
    )
    return [
        utils.get_ffmpeg_binary(), "-y", "-hide_banner", "-loglevel", "verbose", "-nostdin",
        "-ss", f"{max(0.0, float(start)):.3f}", "-t", f"{float(seconds):.3f}",
        *ffmpeg_inputs.remote_input(url, REMOTE_HOSTS),
        "-filter_complex", graph,
        "-map", "[frames]", "-q:v", "3", pattern,
        "-map", "[cuts]", "-f", "null", "-",
    ]


def read_chunk(url: str, start: float, seconds: float, folder: str, index: int) -> tuple[list, list, int]:
    """([(time, frame path)], [cut times], bytes read) for one chunk, times from the video's start."""
    prefix = f"chunk{index:02d}-"
    result = _run_ffmpeg(chunk_command(url, start, seconds, os.path.join(folder, prefix + "%05d.jpg")))
    if result.returncode != 0:
        raise RuntimeError(f"nasa video analysis read failed ({result.returncode})")
    frames = sorted(name for name in os.listdir(folder) if name.startswith(prefix))
    samples = [(start + number * SAMPLE_SECONDS, os.path.join(folder, name)) for number, name in enumerate(frames)]
    # showinfo numbers the frames it sees: n 0 is the read's first frame, not a cut.
    cuts = [start + float(value) for number, value in _SHOWINFO.findall(result.stderr or "") if int(number)]
    return samples, cuts, bytes_read(result.stderr)


# ---------------------------------------------------------------- clean window


def text_area(fit_mode: str, video_aspect: VideoAspect, width: int, height: int) -> VideoAspect | None:
    """
    The part of the frame the video shows, for the text check: ``None`` for all of it
    (blur and contain show the whole frame, and so does any mode for a video that
    already has the output's orientation), the output aspect for cover's centre strip.
    """
    aspect = VideoAspect(video_aspect)
    target_width, target_height = aspect.to_resolution()
    same_orientation = (width > height) == (target_width > target_height) and (width == height) == (
        target_width == target_height)
    if str(fit_mode) in ("blur", "contain") or same_orientation:
        return None
    return aspect


def blank_frame(frame: str) -> bool:
    """A black frame (fade, leader) or a frame of one colour."""
    with Image.open(frame) as image:
        stat = ImageStat.Stat(image.convert("L"))
    return stat.extrema[0][1] < BLACK_MAX_LUMA or stat.stddev[0] < FLAT_MAX_STDDEV


def frame_problems(frame: str, area: VideoAspect | None) -> list[str]:
    """What makes a frame unusable: "blank", "text" (in the shown area, ``None`` = whole frame), "face"."""
    if blank_frame(frame):
        return ["blank"]
    problems = []
    with _detector_lock:
        if nasa.crop_text_lines(frame, area):
            problems.append("text")
        if nasa.count_faces(frame):
            problems.append("face")
    return problems


def choose_window(samples: list[tuple[float, list[str]]], cuts: list[float], clip_seconds: float,
                  analysed_seconds: float) -> dict:
    """
    The earliest window of ``clip_seconds`` inside one shot whose samples are all clean.

    ``samples`` are (time, problems), ``cuts`` the shot changes. Returns ``start`` (None
    when there is no window) and ``rejected``: the shots before the window (or all of
    them) and why each was passed over: its most common problem ("text", "face",
    "blank"), or ``short_shot`` for a clean shot shorter than the clip.
    """
    bounds = [0.0, *sorted({round(c, 3) for c in cuts if 0 < c < analysed_seconds}), float(analysed_seconds)]
    rejected: Counter = Counter()
    for shot_start, shot_end in zip(bounds, bounds[1:]):
        inside = [(time, problems) for time, problems in samples if shot_start <= time < shot_end]
        if shot_end - shot_start < clip_seconds:
            kinds = Counter(problem for _time, problems in inside for problem in problems)
            rejected[kinds.most_common(1)[0][0] if kinds else "short_shot"] += 1
            continue
        start = shot_start
        while start + clip_seconds <= shot_end + 1e-6:
            window = [problems for time, problems in inside if start <= time < start + clip_seconds]
            bad = [time for time, problems in inside if start <= time < start + clip_seconds and problems]
            if window and not bad:
                return {"start": round(start, 2), "duration": float(clip_seconds), "rejected": dict(rejected),
                        "cuts": len(bounds) - 2}
            if not bad:
                break
            start = max(bad) + SAMPLE_SECONDS
        kinds = Counter(problem for _time, problems in inside for problem in problems)
        rejected[kinds.most_common(1)[0][0] if kinds else "short_shot"] += 1
    return {"start": None, "rejected": dict(rejected), "cuts": len(bounds) - 2}


def _analysis_path(item: MaterialInfo, clip_seconds: float, area: VideoAspect | None) -> str:
    asset = str((item.source_info or {}).get("asset_id") or item.url)
    key = utils.md5(f"{ANALYSIS_VERSION}|{asset}|{float(clip_seconds)}|{area.value if area else 'whole'}")
    return os.path.join(utils.storage_dir("cache_videos", create=True), f"nasa-video-{key}.json")


def detectors_ready() -> bool:
    """The clean window needs both detectors (``uv sync --extra face``) and both filters on."""
    return nasa.face_filter_active() and nasa.text_filter_active()


def analyse(item: MaterialInfo, clip_seconds: float, video_aspect: VideoAspect, fit_mode: str) -> dict:
    """The clean window of a NASA video (cached per asset, clip length and shown area)."""
    video = (item.source_info or {}).get("video") or {}
    area = text_area(fit_mode, video_aspect, int(video.get("width") or 1920), int(video.get("height") or 1080))
    path = _analysis_path(item, clip_seconds, area)
    try:
        with open(path, encoding="utf-8") as file:
            cached = json.load(file)
        if isinstance(cached, dict) and cached.get("version") == ANALYSIS_VERSION:
            return cached
    except (OSError, ValueError):
        pass
    copies = video.get("renditions") or {}
    rendition = next((name for name in ANALYSIS_RENDITIONS if name in copies), "")
    if not rendition:
        return {"start": None, "reason": "no_analysis_copy"}
    limit = min(float(ANALYZE_SECONDS), float(video.get("duration") or ANALYZE_SECONDS))
    samples: list[tuple[float, list[str]]] = []
    cuts: list[float] = []
    read = 0
    result: dict = {"start": None}
    analysed = 0.0
    with tempfile.TemporaryDirectory(prefix="nasa-video-") as folder:
        index = 0
        while analysed < limit:
            # Each chunk after the first starts a sample early, so a cut right at its
            # start is seen (the first frame of a read has nothing to compare with).
            start = max(0.0, analysed - SAMPLE_SECONDS) if index else 0.0
            seconds = min(float(CHUNK_SECONDS), limit - analysed) + (analysed - start)
            try:
                frames, chunk_cuts, chunk_read = read_chunk(copies[rendition], start, seconds, folder, index)
            except Exception as exc:
                logger.warning(f"nasa video analysis failed: {item.url}: {type(exc).__name__}: {exc}")
                return {"start": None, "reason": f"analysis_failed:{type(exc).__name__}"}
            read += chunk_read
            for time, frame in frames:
                if not index or time >= analysed - 1e-6:
                    samples.append((time, frame_problems(frame, area)))
                os.remove(frame)
            cuts.extend(cut for cut in chunk_cuts if not index or cut >= analysed - 1e-6)
            analysed = min(limit, start + seconds)
            index += 1
            result = choose_window(samples, cuts, clip_seconds, analysed)
            if result["start"] is not None or not frames:
                break
    result.update(
        version=ANALYSIS_VERSION,
        analysis_rendition=rendition,
        analysed_seconds=round(analysed, 2),
        analysis_bytes=read,
        samples=len(samples),
        problem_samples=dict(Counter(problem for _time, problems in samples for problem in problems)),
    )
    temp = f"{path}.part"
    try:
        with open(temp, "w", encoding="utf-8") as file:
            json.dump(result, file)
        os.replace(temp, path)
    except OSError:
        pass
    return result


SEGMENT_FIELDS = ("start", "duration", "rejected", "cuts", "analysis_rendition", "analysed_seconds",
                  "analysis_bytes", "problem_samples", "reason")


def screen(item: MaterialInfo, *, clip_seconds: float, video_aspect: VideoAspect, fit_mode: str) -> bool:
    """True when the video has a clean window; ``source_info["segment"]`` records the decision."""
    if not detectors_ready():
        logger.warning("nasa video skipped: the clean window needs the face and text filters "
                       "(uv sync --extra face, nasa_face_filter and nasa_text_filter on)")
        return False
    result = analyse(item, clip_seconds, video_aspect, fit_mode)
    item.source_info["segment"] = {key: result[key] for key in SEGMENT_FIELDS if key in result}
    item.source_info["segment"]["fit"] = str(fit_mode)
    if result.get("start") is None:
        reason = result.get("rejected") or result.get("reason")
        logger.info(f"nasa video filter: skipped {item.source_info.get('asset_id')}, "
                    f"no clean {clip_seconds}s window ({reason})")
        task_materials.rejected(item, "clean_window", json.dumps(reason, sort_keys=True))
        return False
    logger.info(f"nasa video window: {item.source_info.get('asset_id')} from {result['start']}s "
                f"(passed over {result.get('rejected') or {}})")
    return True


def window_frames(item: MaterialInfo, folder: str) -> list[str]:
    """
    Three frames of the chosen clean window (start, middle, end) from the analysis copy,
    for the visual screening (``vision_screen``). Empty without a window.
    """
    info = item.source_info if isinstance(item.source_info, dict) else {}
    segment = info.get("segment") or {}
    copies = (info.get("video") or {}).get("renditions") or {}
    url = next((copies[name] for name in ANALYSIS_RENDITIONS if name in copies), "")
    if segment.get("start") is None or not url:
        return []
    start, length = float(segment["start"]), float(segment.get("duration") or 5)
    frames = []
    for index, second in enumerate((start + 0.3, start + length / 2, start + max(0.3, length - 0.3))):
        target = os.path.join(folder, f"window-{index}.jpg")
        result = _run_ffmpeg([utils.get_ffmpeg_binary(), "-y", "-hide_banner", "-loglevel", "error", "-nostdin",
                              "-ss", f"{second:.3f}", *ffmpeg_inputs.remote_input(url, REMOTE_HOSTS),
                              "-frames:v", "1", "-vf", f"scale={FRAME_WIDTH}:-2", "-q:v", "3", target], timeout=120)
        if result.returncode == 0 and os.path.isfile(target):
            frames.append(target)
    return frames


def save_clip(item: MaterialInfo, save_dir: str = "", *, clip_seconds: float, video_aspect: VideoAspect,
              fit_mode: str) -> str:
    """The clean window of the video, cut from a high resolution copy, as a local MP4 ("" when none)."""
    if not screen(item, clip_seconds=clip_seconds, video_aspect=video_aspect, fit_mode=fit_mode):
        return ""
    segment = item.source_info["segment"]
    video = (item.source_info or {}).get("video") or {}
    copies = video.get("renditions") or {}
    larger = min(int(video.get("width") or 0), int(video.get("height") or 0)) > LARGE_COPY_HEIGHT
    names = EXTRACT_RENDITIONS_FULL if str(fit_mode) == "cover" and larger else EXTRACT_RENDITIONS
    rendition = next((name for name in names if name in copies), "")
    if not rendition:
        return ""
    url = copies[rendition]
    save_dir = save_dir or utils.storage_dir("cache_videos", create=True)
    os.makedirs(save_dir, exist_ok=True)
    max_width = EXTRACT_MAX_WIDTH.get(str(fit_mode))
    key = utils.md5(f"{url}|{segment['start']}|{float(clip_seconds)}|{max_width}")
    target = os.path.join(save_dir, f"nasa-video-{key}.mp4")
    segment["rendition"] = rendition
    if os.path.isfile(target) and os.path.getsize(target) > 0:
        segment.setdefault("window_bytes", 0)
        return target
    try:
        segment["window_bytes"] = fetch_window(url, float(segment["start"]), float(clip_seconds), target,
                                               max_width=max_width)
    except Exception as exc:
        logger.error(f"nasa video window could not be read: {item.url}: {exc}")
        task_materials.failed(item, "download", "the video window could not be read")
        return ""
    return target
