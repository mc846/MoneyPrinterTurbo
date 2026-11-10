"""Revisions of a finished task: its saved materials with a new text, voice, subtitles or music.

``storage/tasks/<task>/revisions/rNNN/`` holds each revision: ``script.txt``,
``audio.mp3``, ``subtitle.srt``, ``final.mp4`` and ``revision.json`` (parent revision,
what changed, the scenes with their durations, settings, audit, timings). The task's
own output is r001: only its ``revision.json`` is written, the files stay where the
pipeline put them. Revisions are never changed: a new one is built in a hidden folder
and renamed into place; ``verify`` compares the files with the recorded SHA-256.

The materials stay shared (``materials/materials.json``); revisions refer to its
scenes and materials by ID. A revision changes only the text, the voice and its rate,
the subtitle style, the background music and the NASA on-screen credit
(``EDITABLE``). Durations follow the new voiceover by the rules in
``revision_timing``; materials are downloaded only for an added scene. The network is
used only for TTS, the audit of a changed text and such downloads.

Command line: ``python cli.py revizyon <task_id> [options]`` (``cli_main``). The
WebUI starts the same command in a background process (``start_background``).
"""

import argparse
import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from loguru import logger

from app import paths
from app.models.schema import MaterialInfo, VideoAspect, VideoFitMode, VideoParams
from app.services import revision_timing as timing
from app.services import task_artifacts, task_materials
from app.utils import ffmpeg_inputs, utils

# MoviePy measures durations here with ffmpeg; its commands carry the protocol whitelist too.
ffmpeg_inputs.guard_libraries()

SCHEMA_VERSION = 1
FOLDER = "revisions"
REVISION_FILE = "revision.json"
FIRST = "r001"
PROGRESS_FILE = ".ilerleme.json"
LOG_FILE = ".islem.log"
ACTION = "revizyon"

VOICE_FIELDS = ("voice_name", "voice_rate")
SUBTITLE_FIELDS = (
    "subtitle_enabled", "font_name", "font_size", "text_fore_color", "stroke_color",
    "stroke_width", "subtitle_position", "custom_position",
)
MUSIC_FIELDS = ("bgm_type", "bgm_file", "bgm_volume")
EDITABLE = (*VOICE_FIELDS, *SUBTITLE_FIELDS, *MUSIC_FIELDS, "nasa_credit_overlay")
IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp", ".bmp")
# The pipeline renders a little longer than the voiceover and the concat trims it.
_RENDER_MARGIN = 0.1


class RevisionError(RuntimeError):
    """A revision cannot be made (nothing changed, a missing file, a busy task...)."""


# ------------------------------------------------------------------ folders and records


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def task_folder(task_id: str) -> Path:
    root = Path(utils.task_dir()).resolve()
    folder = (root / str(task_id)).resolve()
    if folder.parent != root or not folder.is_dir():
        raise RevisionError(f"no such task: {task_id}")
    return folder


def revisions_dir(task_id: str) -> Path:
    return task_folder(task_id) / FOLDER


def _revision_dir(task_id: str, revision: str) -> Path:
    if not (len(revision) == 4 and revision[0] == "r" and revision[1:].isdigit()):
        raise RevisionError(f"invalid revision name: {revision!r}")
    return revisions_dir(task_id) / revision


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def load(task_id: str, revision: str) -> dict:
    path = _revision_dir(task_id, revision) / REVISION_FILE
    if revision == FIRST and not path.exists():
        return ensure_first(task_id)
    if not path.exists():
        raise RevisionError(f"no such revision: {revision}")
    return _read(path)


def names(task_id: str) -> list[str]:
    folder = revisions_dir(task_id)
    found = sorted(
        entry.name for entry in folder.iterdir()
        if entry.is_dir() and (entry / REVISION_FILE).exists()
    ) if folder.is_dir() else []
    return found or [FIRST]


def history(task_id: str) -> list[dict]:
    """All revisions, r001 first."""
    ensure_first(task_id)
    return [load(task_id, name) for name in names(task_id)]


def latest(task_id: str) -> str:
    return names(task_id)[-1]


def file_path(task_id: str, revision: dict, key: str) -> Path | None:
    entry = revision.get(key) or {}
    if not entry.get("file"):
        return None
    return (_revision_dir(task_id, revision["revision"]) / entry["file"]).resolve()


def script_text(task_id: str, revision: dict) -> str:
    entry = revision.get("script") or {}
    if "text" in entry:
        return entry["text"]
    return file_path(task_id, revision, "script").read_text(encoding="utf-8")


def verify(task_id: str, revision: str) -> list[str]:
    """Files of the revision whose content no longer matches revision.json."""
    record = load(task_id, revision)
    changed = []
    for key in ("script", "audio", "subtitle", "video"):
        entry = record.get(key) or {}
        path = file_path(task_id, record, key)
        if path is None or not entry.get("sha256"):
            continue
        if not path.is_file() or task_materials._sha256(path) != entry["sha256"]:
            changed.append(key)
    return changed


def _media_duration(path: Path) -> float:
    from moviepy.video.io.ffmpeg_reader import ffmpeg_parse_infos

    return round(float(ffmpeg_parse_infos(str(path)).get("duration") or 0.0), 3)


def _file_entry(path: Path, folder: Path, **extra) -> dict:
    return {
        "file": os.path.relpath(path, folder).replace(os.sep, "/"),
        "sha256": task_materials._sha256(path),
        **extra,
    }


def _material_kind(material: dict) -> str:
    files = material.get("files") or {}
    if "prepared" in files:
        return timing.IMAGE
    local = files.get("local")
    if local and Path(local["path"]).suffix.lower() in IMAGE_SUFFIXES:
        return timing.IMAGE
    return timing.VIDEO


def _footage(task_id: str, material: dict) -> Path:
    files = material.get("files") or {}
    entry = files.get("clip") or files.get("video") or files.get("local")
    if not entry:
        raise RevisionError(f"material {material.get('id')} has no footage file")
    return task_materials.stored_path(task_id, entry)


def _materials(task_id: str) -> tuple[dict, dict]:
    manifest = task_materials.load(task_folder(task_id))
    if manifest is None:
        raise RevisionError(
            "the task has no materials/materials.json; revisions need a task made with "
            "[app] save_task_materials = true"
        )
    return manifest, {material["id"]: material for material in manifest.get("materials") or []}


def _editable(params: dict) -> dict:
    return {name: params.get(name) for name in EDITABLE}


def _publish(work: Path, target: Path) -> None:
    """Revisions are never rewritten: a finished folder is renamed into place once."""
    if target.exists():
        raise RevisionError(f"{target.name} already exists")
    os.rename(work, target)


def ensure_first(task_id: str) -> dict:
    """Record the task's own output as r001; its files stay in the task folder."""
    folder = task_folder(task_id)
    target = folder / FOLDER / FIRST
    if (target / REVISION_FILE).exists():
        return _read(target / REVISION_FILE)
    data = _read(folder / "script.json")
    manifest, materials = _materials(task_id)
    final_video = folder / "final-1.mp4"
    audio = folder / "audio.mp3"
    if not final_video.is_file() or not audio.is_file():
        raise RevisionError("the task has no finished video to revise")
    params = dict(data.get("params") or {})
    script = str(data.get("script") or params.get("video_script") or "").strip()
    scenes = []
    for position, scene in enumerate(
            [s for s in manifest.get("scenes") or [] if s.get("video") == 1], start=1):
        material = materials.get(scene.get("material") or "")
        if not material:
            continue
        kind = _material_kind(material)
        scenes.append({
            "id": scene["id"], "material": material["id"], "kind": kind,
            "term_index": material.get("term_index"), "search_term": material.get("search_term"),
            "order": float(position), "start": scene.get("start") or 0.0,
            "duration": scene.get("duration") or 0.0,
            "source_duration": _media_duration(_footage(task_id, material)) if kind == timing.VIDEO else None,
            "status": "kept",
        })
    if not scenes:
        raise RevisionError("materials.json has no scenes for the task's video")
    subtitle = folder / "subtitle.srt"
    target_folder = folder / FOLDER / FIRST
    record = {
        "schema_version": SCHEMA_VERSION,
        "revision": FIRST,
        "task_id": str(task_id),
        "parent": None,
        "source": "task",
        "created_at": datetime.fromtimestamp(final_video.stat().st_mtime, timezone.utc).isoformat(timespec="seconds"),
        "recorded_at": _now(),
        "changes": {},
        "script": {"text": script, "words": len(script.split()),
                   "sha256": hashlib.sha256(script.encode("utf-8")).hexdigest()},
        "audio": _file_entry(audio, target_folder, duration=_media_duration(audio)),
        "subtitle": _file_entry(subtitle, target_folder) if subtitle.is_file() else None,
        "video": _file_entry(final_video, target_folder, duration=_media_duration(final_video)),
        "params": params,
        "settings": _editable(params),
        "bgm": {"type": params.get("bgm_type"), "file": params.get("bgm_file") or None,
                "volume": params.get("bgm_volume")},
        "scenes": scenes,
        "dropped": [], "returned": [], "added": [], "extended": None, "warnings": [],
        "audit": None, "check_required": False, "check_reasons": [],
        "material_sources": list(data.get("material_sources") or []),
    }
    work = folder / FOLDER / f".{FIRST}-{uuid.uuid4().hex[:8]}.tmp"
    work.mkdir(parents=True)
    try:
        task_artifacts._write_json_atomic(work / REVISION_FILE, record)
        _publish(work, target)
    except Exception:
        shutil.rmtree(work, ignore_errors=True)
        if (target / REVISION_FILE).exists():  # another process recorded it first
            return _read(target / REVISION_FILE)
        raise
    return record


def _next_name(task_id: str) -> str:
    numbers = [int(name[1:]) for name in names(task_id)]
    return f"r{max(numbers) + 1:03d}"


# ------------------------------------------------------------------ progress


def progress_path(task_id: str) -> Path:
    return revisions_dir(task_id) / PROGRESS_FILE


def read_progress(task_id: str) -> dict | None:
    try:
        return _read(progress_path(task_id))
    except (OSError, ValueError, RevisionError):
        return None


class _Progress:
    def __init__(self, task_id: str, revision: str, base: str):
        self.path = progress_path(task_id)
        self.record = {"revision": revision, "base": base, "started": _now(), "stage": "", "pid": os.getpid()}

    def __call__(self, stage: str, **extra) -> None:
        self.record.update(stage=stage, updated=_now(), **extra)
        logger.info(f"revizyon {self.record['revision']}: {stage}")
        try:
            task_artifacts._write_json_atomic(self.path, self.record)
        except OSError:
            pass


# ------------------------------------------------------------------ steps (tests replace these)


def _synthesize(params: VideoParams, script: str, audio_file: Path):
    """TTS of the script; returns the subtitle maker (word timings) or raises."""
    from app.services import voice

    sub_maker = voice.tts(
        text=script, voice_name=voice.parse_voice_name(params.voice_name),
        voice_rate=params.voice_rate, voice_file=str(audio_file),
    )
    if sub_maker is None or not audio_file.is_file():
        raise RevisionError("TTS failed; check the voice and the connection")
    return sub_maker


def _subtitles(params: VideoParams, script: str, sub_maker, audio_file: Path, subtitle_file: Path) -> bool:
    """subtitle.srt for the new voiceover, by the task's subtitle provider."""
    from app.config import config
    from app.services import subtitle, voice

    provider = str(config.app.get("subtitle_provider", "edge") or "").strip().lower()
    word_level = getattr(params, "subtitle_display_mode", "sentence") == "word_by_word"
    if provider == "whisper":
        subtitle.create(audio_file=str(audio_file), subtitle_file=str(subtitle_file), word_level=word_level)
        if subtitle_file.is_file() and not word_level:
            subtitle.correct(subtitle_file=str(subtitle_file), video_script=script)
    elif provider == "edge" and sub_maker is not None:
        voice.create_subtitle(text=script, sub_maker=sub_maker, subtitle_file=str(subtitle_file),
                              word_level=word_level)
    return subtitle_file.is_file() and bool(subtitle.file_to_subtitles(str(subtitle_file)))


def _audit(subject: str, script: str) -> dict:
    from app.services import batch_package

    return batch_package.audit_script(subject, script)


# ------------------------------------------------------------------ added scenes


def _candidate_item(candidate: dict, search_term: str) -> MaterialInfo | None:
    """The candidate as a downloadable item; older manifests lack the URL (NASA's can be rebuilt)."""
    record = candidate.get("item") or {}
    info = dict(record.get("source_info") or {})
    info.setdefault("provider", candidate.get("provider"))
    info.setdefault("asset_id", candidate.get("asset_id"))
    info.setdefault("source_page", candidate.get("source_page"))
    info["search_term"] = search_term
    url = record.get("url")
    if not url and candidate.get("provider") == "nasa" and candidate.get("asset_id"):
        from urllib.parse import quote

        asset = quote(str(candidate["asset_id"]))
        url = f"https://images-assets.nasa.gov/image/{asset}/{asset}~orig.jpg"
    if not url:
        return None
    return MaterialInfo(provider=candidate.get("provider") or record.get("provider") or "",
                        url=url, duration=int(record.get("duration") or candidate.get("duration") or 0),
                        source_info=info)


def _candidates(manifest: dict, materials: dict) -> tuple[list[timing.Candidate], dict]:
    """Unused candidates that passed the filters, in rank order, keyed for the planner."""
    used = {(m.get("provider"), str(m.get("asset_id"))) for m in materials.values()}
    found, entries = [], {}
    for term in manifest.get("terms") or []:
        for candidate in term.get("candidates") or []:
            key = (candidate.get("provider"), str(candidate.get("asset_id")))
            if candidate.get("status") in ("selected", "rejected", "failed") or key in used:
                continue
            name = f"{key[0]}:{key[1]}"
            if name in entries:
                continue
            entries[name] = (candidate, term)
            found.append(timing.Candidate(key=name, term_index=term["index"]))
    return found, entries


class _Fetcher:
    """Downloads an added candidate, applies the source's filters and adds it to materials.json."""

    def __init__(self, task_id: str, revision: str, params: VideoParams, entries: dict):
        self.task_id, self.revision, self.params, self.entries = task_id, revision, params, entries
        self.downloads = 0
        self.skipped: list[dict] = []
        self.count = 0

    def __call__(self, candidate: timing.Candidate) -> timing.Scene | None:
        from app.services import material, nasa

        entry, term = self.entries[candidate.key]
        item = _candidate_item(entry, term["term"])
        if item is None:
            self.skipped.append({"candidate": candidate.key, "reason": "no download address recorded"})
            return None
        cache = utils.storage_dir("cache_videos", create=True)
        aspect = VideoAspect(self.params.video_aspect)
        roles, kind, duration = {}, timing.VIDEO, None
        self.downloads += 1
        try:
            if item.provider == "nasa":
                if nasa.text_filter_active() and not nasa.screen_text([item], aspect, term["term"]):
                    self.skipped.append({"candidate": candidate.key, "reason": "text in the crop area"})
                    return None
                original, prepared = nasa._prepared_image(
                    item, cache, aspect, self.params.video_clip_duration, self.params.image_motion)
                if not prepared and not entry.get("item") and "~orig." in item.url:
                    # An older manifest has no address; the rebuilt ~orig may be missing or too
                    # large to open (Pillow's decompression limit), the ~large rendition often works.
                    item.url = item.url.replace("~orig.", "~large.")
                    original, prepared = nasa._prepared_image(
                        item, cache, aspect, self.params.video_clip_duration, self.params.image_motion)
                if not prepared:
                    # Unavailable (e.g. 403 for a rebuilt ~orig), too large to open, or below the
                    # output resolution after cropping (nasa.prepare_image); the log has which.
                    self.skipped.append({"candidate": candidate.key,
                                         "reason": "image could not be downloaded or prepared"})
                    return None
                if nasa._shows_face(item, prepared, original, aspect):
                    self.skipped.append({"candidate": candidate.key, "reason": "face"})
                    return None
                roles, kind, footage = {"original": original, "prepared": prepared}, timing.IMAGE, prepared
            else:
                path = material.save_video(video_url=item.url, save_dir=cache)
                if not path:
                    self.skipped.append({"candidate": candidate.key, "reason": "download failed"})
                    return None
                roles, footage = {"video": path}, path
                duration = _media_duration(Path(path))
        except Exception as exc:
            self.skipped.append({"candidate": candidate.key, "reason": f"{type(exc).__name__}"})
            return None
        record = material._material_source_record(item, footage)
        added = task_materials.add_material(
            self.task_id, item, roles, record, search_term=term["term"], term_index=term["index"],
            added_by=self.revision, relevance=entry.get("relevance"),
        )
        self.count += 1
        return timing.Scene(
            id=f"{self.revision}-a{self.count:02d}", material=added["id"], kind=kind,
            term_index=term["index"], start=0.0, source_duration=duration,
        )


# ------------------------------------------------------------------ rendering


def _scene(record: dict) -> timing.Scene:
    return timing.Scene(
        id=record["id"], material=record["material"], kind=record["kind"],
        term_index=record.get("term_index"), order=float(record.get("order") or 0.0),
        start=float(record.get("start") or 0.0), source_duration=record.get("source_duration"),
    )


def _image_source(task_id: str, material: dict, work: Path) -> Path:
    """The prepared image (NASA) or uploaded image (local) to render: in the cache, where the
    zoom clip of the same duration is reused; restored there from the task if cleaned."""
    files = material["files"]
    entry = files.get("prepared") or files.get("local")
    stored = task_materials.stored_path(task_id, entry)
    cached = task_materials.cache_path(entry)
    if cached is None:
        cached = work / f"image-{material['id']}{stored.suffix}"
    if not cached.is_file():
        cached.parent.mkdir(parents=True, exist_ok=True)
        task_materials.store_file(stored, cached)
    return cached


def _render_clip(task_id: str, material: dict, scene: timing.Scene, duration: float,
                 params: VideoParams, work: Path, number: int) -> Path:
    from app.services import video

    width, height = VideoAspect(params.video_aspect).to_resolution()
    output = work / f"clip-{number:03d}.mp4"
    if scene.kind == timing.IMAGE:
        image = _image_source(task_id, material, work)
        zoom = video.render_image_zoom_video(
            str(image), max(1, math.ceil(duration)), image_motion=params.image_motion,
            output_size=(width, height),
        )
        item = video.SubClippedVideoClip(file_path=zoom, start_time=0.0, end_time=duration,
                                         width=width, height=height)
        fit = VideoFitMode.cover
    else:
        source = _footage(task_id, material)
        clip = video._open_video_clip_quietly(str(source))
        try:
            clip_width, clip_height = clip.size
        finally:
            video.close_clip(clip)
        item = video.SubClippedVideoClip(
            file_path=str(source), start_time=scene.start, end_time=scene.start + duration,
            width=clip_width, height=clip_height,
        )
        fit = video._clip_fit_mode(clip_width, clip_height, width, height, params.video_fit_mode,
                                   bool(params.crop_materials_to_aspect))
    video._normalize_clip_with_ffmpeg(item, (width, height), fit, str(output))
    return output


def _frame_counts(durations: list[float], fps: int) -> list[int]:
    """Frames per clip with the boundaries rounded on the running total."""
    counts, previous, total = [], 0, 0.0
    for duration in durations:
        total += duration
        edge = round(total * fps)
        counts.append(max(1, edge - previous))
        previous = edge
    return counts


def _resolve_bgm(params: VideoParams, parent: dict, music_changed: bool) -> str:
    """The music file to mix: a random choice is made once and kept by later revisions."""
    from app.services import video

    if not params.bgm_type:
        return ""
    if params.bgm_type == "random" and not music_changed:
        previous = (parent.get("bgm") or {}).get("file")
        if previous:
            resolved = video.get_bgm_file("custom", previous)
            if resolved:
                return resolved
    return video.get_bgm_file(params.bgm_type, params.bgm_file or "")


# ------------------------------------------------------------------ creating a revision


def create(task_id: str, *, script: str | None = None, settings: dict | None = None,
           base: str | None = None) -> dict:
    """Make the next revision from ``base`` (default: the latest). Returns its revision.json."""
    from app.services import batch_review, nasa, video

    started = time.monotonic()
    first = ensure_first(task_id)
    base_name = base or latest(task_id)
    parent = load(task_id, base_name)
    params_dict = dict(parent["params"])
    changes = {}
    for name, value in (settings or {}).items():
        if name not in EDITABLE:
            raise RevisionError(f"{name} cannot be changed in a revision")
        if params_dict.get(name) != value:
            changes[name] = {"from": params_dict.get(name), "to": value}
            params_dict[name] = value
    parent_script = script_text(task_id, parent)
    new_script = batch_review.normalize_script(script) if script is not None else parent_script
    script_changed = new_script != parent_script
    if script_changed:
        if not new_script.strip():
            raise RevisionError("the text is empty")
        changes["script"] = {"from_words": len(parent_script.split()), "to_words": len(new_script.split())}
        params_dict["video_script"] = new_script
    if not changes:
        raise RevisionError("nothing changed")
    params = VideoParams(**params_dict)
    manifest, materials = _materials(task_id)

    folder = revisions_dir(task_id)
    folder.mkdir(exist_ok=True)
    name = _next_name(task_id)
    progress = _Progress(task_id, name, base_name)
    work = folder / f".{name}-{uuid.uuid4().hex[:8]}.tmp"
    work.mkdir()
    timings, network = {}, {"tts": False, "audit": False, "downloads": 0}
    try:
        # 1. Voiceover and subtitles: reused unless the text or the voice changed.
        step = time.monotonic()
        audio, subtitle = work / "audio.mp3", work / "subtitle.srt"
        parent_audio = file_path(task_id, parent, "audio")
        parent_subtitle = file_path(task_id, parent, "subtitle")
        voice_changed = script_changed or any(field in changes for field in VOICE_FIELDS)
        reuse = not voice_changed and (not params.subtitle_enabled or parent_subtitle is not None)
        if reuse:
            progress("voice: reusing the voiceover")
            task_materials.store_file(parent_audio, audio)
            if params.subtitle_enabled:
                task_materials.store_file(parent_subtitle, subtitle)
        else:
            progress("voice: text to speech")
            network["tts"] = True
            sub_maker = _synthesize(params, new_script, audio)
            if params.subtitle_enabled and not _subtitles(params, new_script, sub_maker, audio, subtitle):
                subtitle.unlink(missing_ok=True)
        audio_duration = _media_duration(audio)
        timings["voice"] = round(time.monotonic() - step, 1)

        # 2. Audit of a changed text.
        audit, reasons = None, []
        if script_changed:
            step = time.monotonic()
            progress("audit")
            network["audit"] = True
            audit = _audit(str(params.video_subject or ""), new_script)
            if batch_review.audit_blocks(audit):
                reasons.append(batch_review.describe_audit(audit))
            timings["audit"] = round(time.monotonic() - step, 1)

        # 3. Scenes fitted to the voiceover.
        step = time.monotonic()
        progress("scenes")
        base_scenes = [_scene(record) for record in parent["scenes"]]
        present = {scene.id for scene in base_scenes}
        reserve = [_scene(record) for record in first["scenes"] if record["id"] not in present]
        candidates, entries = _candidates(manifest, materials)
        fetcher = _Fetcher(task_id, name, params, entries)
        plan = timing.plan(base_scenes, audio_duration, float(params.video_clip_duration),
                           reserve=reserve, candidates=candidates, fetch=fetcher)
        network["downloads"] = fetcher.downloads
        manifest, materials = _materials(task_id)
        timings["scenes"] = round(time.monotonic() - step, 1)

        # 4. Clips, assembly and the final video.
        step = time.monotonic()
        clips = []
        frames = _frame_counts([planned.duration for planned in plan.scenes], video.fps)
        for number, planned in enumerate(plan.scenes, start=1):
            progress(f"clips {number}/{len(plan.scenes)}")
            duration = planned.duration
            if number == len(plan.scenes):
                # A little past the voiceover, as the pipeline renders; a video stays in its source.
                room = math.inf
                if planned.scene.kind == timing.VIDEO:
                    room = (planned.scene.source_duration or 0.0) - planned.scene.start - duration
                frames[-1] += round(min(_RENDER_MARGIN, max(0.0, room)) * video.fps)
            # Clip boundaries on whole frames, rounded on the running total, so the clips add
            # up to the voiceover instead of losing a fraction of a frame each.
            duration = (frames[number - 1] + 0.5) / video.fps
            clips.append(_render_clip(task_id, materials[planned.scene.material], planned.scene,
                                      duration, params, work, number))
        progress("assembling")
        combined = work / "combined.mp4"
        video.concat_video_clips_with_ffmpeg(
            clip_files=[str(clip) for clip in clips], output_file=str(combined),
            threads=params.n_threads, output_dir=str(work), max_duration=audio_duration,
        )
        used = list(dict.fromkeys(planned.scene.material for planned in plan.scenes))
        sources = [dict(materials[material_id]["source"]) for material_id in used]
        credit = ""
        if params.nasa_credit_overlay and any(s.get("provider") == "nasa" for s in sources):
            credit = nasa.overlay_credit_text(sources, params.video_language)
        music_changed = any(field in changes for field in MUSIC_FIELDS)
        bgm = _resolve_bgm(params, parent, music_changed)
        progress("final video")
        final = work / "final.mp4"
        # The chosen file goes in as a custom file, not as bgm_file_override: generate_video loops
        # and cuts only its own music to the video (an override is taken as already fitted).
        render_params = params.model_copy(update={"bgm_type": "custom" if bgm else "", "bgm_file": bgm or ""})
        video.generate_video(
            video_path=str(combined), audio_path=str(audio),
            subtitle_path=str(subtitle) if subtitle.is_file() else "", output_file=str(final),
            params=render_params, **({"credit_text": credit} if credit else {}),
        )
        if not final.is_file():
            raise RevisionError("the final video was not written")
        for clip in clips:
            clip.unlink(missing_ok=True)
        combined.unlink(missing_ok=True)
        timings["render"] = round(time.monotonic() - step, 1)

        (work / "script.txt").write_text(new_script + "\n", encoding="utf-8")
        warning = timing.term_matching_warning(script_changed, bool(params.match_materials_to_script))
        target = folder / name
        record = {
            "schema_version": SCHEMA_VERSION,
            "revision": name,
            "task_id": str(task_id),
            "parent": base_name,
            "source": "revision",
            "created_at": _now(),
            "changes": changes,
            "script": {**_file_entry(work / "script.txt", work), "words": len(new_script.split())},
            "audio": _file_entry(audio, work, duration=audio_duration,
                                 reused_from=base_name if reuse else None),
            "subtitle": _file_entry(subtitle, work, reused_from=base_name if reuse else None)
            if subtitle.is_file() else None,
            "video": _file_entry(final, work, duration=_media_duration(final)),
            "params": params.model_dump(mode="json"),
            "settings": _editable(params.model_dump(mode="json")),
            "bgm": {"type": params.bgm_type, "file": os.path.basename(bgm) if bgm else None,
                    "volume": params.bgm_volume},
            "scenes": [
                {
                    "id": planned.scene.id, "material": planned.scene.material, "kind": planned.scene.kind,
                    "term_index": planned.scene.term_index,
                    "search_term": materials[planned.scene.material].get("search_term"),
                    "order": planned.scene.order, "start": planned.scene.start,
                    "duration": planned.duration, "source_duration": planned.scene.source_duration,
                    "status": planned.status,
                }
                for planned in plan.scenes
            ],
            "dropped": [{"id": s.id, "material": s.material, "reason": plan.dropped_reasons.get(s.id)}
                        for s in plan.dropped],
            "returned": [s.id for s in plan.returned],
            "added": [{"id": s.id, "material": s.material, "candidate": c.key, "term_index": c.term_index}
                      for c, s in plan.added],
            "skipped_candidates": fetcher.skipped,
            "extended": plan.extended,
            "warnings": [warning] if warning else [],
            "audit": audit,
            "check_required": bool(reasons),
            "check_reasons": reasons,
            "material_sources": sources,
            "network": network,
            "timings": {**timings, "total": round(time.monotonic() - started, 1)},
        }
        task_artifacts._write_json_atomic(work / REVISION_FILE, record)
        progress("done")
        _publish(work, target)
        return record
    except Exception:
        shutil.rmtree(work, ignore_errors=True)
        raise


def scene_rows(task_id: str, revision: dict) -> list[dict]:
    """The revision's scenes for display: term, duration, kind and a small preview image."""
    manifest, materials = _materials(task_id)
    folder = task_materials.folder(task_id)
    previews = {
        (candidate.get("provider"), str(candidate.get("asset_id"))): candidate["preview"]
        for term in manifest.get("terms") or [] for candidate in term.get("candidates") or []
        if candidate.get("preview")
    }
    rows = []
    for scene in revision.get("scenes") or []:
        material = materials.get(scene.get("material") or "", {})
        preview = previews.get((material.get("provider"), str(material.get("asset_id"))))
        path = folder / preview if preview else None
        if (path is None or not path.is_file()) and material:
            files = material.get("files") or {}
            image = files.get("prepared") or (files.get("local") if _material_kind(material) == timing.IMAGE else None)
            path = task_materials.stored_path(task_id, image) if image else None
        rows.append({
            "id": scene["id"], "term": scene.get("search_term") or material.get("search_term") or "",
            "duration": scene.get("duration"), "kind": scene.get("kind"), "status": scene.get("status"),
            "provider": material.get("provider"), "asset_id": material.get("asset_id"),
            "preview": str(path) if path is not None and path.is_file() else None,
        })
    return rows


# ------------------------------------------------------------------ batch package


def refresh_package(batch_dir: str | Path, index: int, revision: str | None = None) -> Path:
    """The upload package of a batch item for one revision, in <item folder>/revizyonlar/rNNN/.

    The original package stays: it may already be published, and the batch report and
    kalite.json describe it. Each revision gets its own folder next to it."""
    from app.services import batch_package
    from app.services import batch_production as bp

    batch_dir = Path(batch_dir)
    state = bp.BatchState.load(batch_dir)
    item = next((entry for entry in state.items if entry.index == int(index)), None)
    if item is None or not item.task_id or not item.folder:
        raise RevisionError(f"item {index} of the batch has no finished task")
    name = revision or latest(item.task_id)
    if name == FIRST:
        raise RevisionError("r001 is the task's own video; its package is the batch's package")
    record = load(item.task_id, name)
    preset = bp.load_preset(state.preset)
    original = batch_dir / item.folder
    audit = record.get("audit")
    if audit is None:
        try:
            audit = _read(original / "kalite.json").get("dogruluk")
        except (OSError, ValueError):
            audit = None
    target = original / "revizyonlar" / name
    work = original / "revizyonlar" / f".{name}-{uuid.uuid4().hex[:8]}.tmp"
    work.mkdir(parents=True)
    try:
        batch_package.build_package(
            item, {"videos": [str(file_path(item.task_id, record, "video"))],
                   "script": script_text(item.task_id, record)},
            preset, work, [], revision={
                "name": name,
                "subtitle": str(file_path(item.task_id, record, "subtitle") or ""),
                "material_sources": record.get("material_sources") or [],
                "params": record.get("params") or {},
                "audit": audit,
                "check_reasons": record.get("check_reasons") or [],
            },
        )
        if target.exists():
            shutil.rmtree(target)
        os.rename(work, target)
    except Exception:
        shutil.rmtree(work, ignore_errors=True)
        raise
    return target


# ------------------------------------------------------------------ command line and WebUI


def build_settings(args: argparse.Namespace) -> dict:
    settings = {}
    if args.ses:
        settings["voice_name"] = args.ses
    if args.ses_hizi is not None:
        settings["voice_rate"] = args.ses_hizi
    if args.altyazi:
        settings["subtitle_enabled"] = args.altyazi == "acik"
    for flag, name in (("yazi_tipi", "font_name"), ("yazi_boyutu", "font_size"),
                       ("yazi_rengi", "text_fore_color"), ("kontur_rengi", "stroke_color"),
                       ("kontur_kalinligi", "stroke_width"), ("altyazi_konumu", "subtitle_position"),
                       ("altyazi_ozel_konum", "custom_position"), ("muzik_dosyasi", "bgm_file"),
                       ("muzik_duzeyi", "bgm_volume")):
        value = getattr(args, flag)
        if value is not None:
            settings[name] = value
    if args.muzik is not None:
        settings["bgm_type"] = {"yok": "", "rastgele": "random", "dosya": "custom"}[args.muzik]
    if args.nasa_atif:
        settings["nasa_credit_overlay"] = args.nasa_atif == "acik"
    return settings


_FLAGS = {
    "voice_name": "--ses", "voice_rate": "--ses-hizi", "font_name": "--yazi-tipi",
    "font_size": "--yazi-boyutu", "text_fore_color": "--yazi-rengi", "stroke_color": "--kontur-rengi",
    "stroke_width": "--kontur-kalinligi", "subtitle_position": "--altyazi-konumu",
    "custom_position": "--altyazi-ozel-konum", "bgm_file": "--muzik-dosyasi", "bgm_volume": "--muzik-duzeyi",
}


def settings_arguments(settings: dict) -> list[str]:
    """``cli.py revizyon`` flags for changed settings (the inverse of ``build_settings``)."""
    arguments = []
    for name, value in settings.items():
        if name == "subtitle_enabled":
            arguments.append(f"--altyazi={'acik' if value else 'kapali'}")
        elif name == "nasa_credit_overlay":
            arguments.append(f"--nasa-atif={'acik' if value else 'kapali'}")
        elif name == "bgm_type":
            arguments.append(f"--muzik={({'': 'yok', 'random': 'rastgele', 'custom': 'dosya'})[value or '']}")
        elif name in _FLAGS:
            arguments.append(f"{_FLAGS[name]}={value}")
        else:
            raise RevisionError(f"{name} cannot be changed in a revision")
    return arguments


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(
        prog="cli.py revizyon",
        description="Kayıtlı materyallerle bir görevin yeni revizyonunu üretir (metin, ses, altyazı, müzik).",
    )
    command.add_argument("task_id")
    command.add_argument("--metin-dosyasi", help="yeni metin (UTF-8 dosya)")
    command.add_argument("--ses", help="ses adı, ör. tr-TR-AhmetNeural-Male")
    command.add_argument("--ses-hizi", type=float)
    command.add_argument("--altyazi", choices=("acik", "kapali"))
    command.add_argument("--yazi-tipi")
    command.add_argument("--yazi-boyutu", type=int)
    command.add_argument("--yazi-rengi", help="#RRGGBB")
    command.add_argument("--kontur-rengi", help="#RRGGBB")
    command.add_argument("--kontur-kalinligi", type=float)
    command.add_argument("--altyazi-konumu", choices=("top", "center", "bottom", "custom"))
    command.add_argument("--altyazi-ozel-konum", type=float)
    command.add_argument("--muzik", choices=("yok", "rastgele", "dosya"))
    command.add_argument("--muzik-dosyasi")
    command.add_argument("--muzik-duzeyi", type=float)
    command.add_argument("--nasa-atif", choices=("acik", "kapali"))
    command.add_argument("--temel-revizyon", help="hangi revizyondan türesin (varsayılan: en sonuncusu)")
    command.add_argument("--llm-provider", help="denetim için LLM sağlayıcısı (yalnız bu süreçte)")
    command.add_argument("--llm-model")
    return command


def cli_main(argv: list[str]) -> int:
    """``cli.py revizyon``: 0 = made, 1 = failed, 2 = invalid input, 3 = the task is busy."""
    from app.services import batch_review
    from app.utils.logging_utils import configure_terminal_logger, get_logging_options

    args = parser().parse_args(argv)
    configure_terminal_logger(sys.stderr, level=get_logging_options()[0], colorize=False)
    if args.llm_provider or args.llm_model:
        import cli

        try:
            cli.apply_llm_overrides(args)
        except ValueError as exc:
            logger.error(str(exc))
            return 2
    script = None
    if args.metin_dosyasi:
        text_file = Path(args.metin_dosyasi)
        try:
            script = text_file.read_text(encoding="utf-8")
        except OSError as exc:
            logger.error(f"metin dosyası okunamadı: {exc}")
            return 2
        if text_file.name.startswith(".metin-") and text_file.parent.name == FOLDER:
            text_file.unlink(missing_ok=True)  # the WebUI's hand-over file
    try:
        folder = revisions_dir(args.task_id)
        folder.mkdir(exist_ok=True)
        batch_review.acquire_busy(folder, ACTION)
    except RevisionError as exc:
        logger.error(str(exc))
        return 2
    except batch_review.ReviewBusy as exc:
        logger.error(str(exc))
        return 3
    try:
        record = create(args.task_id, script=script, settings=build_settings(args),
                        base=args.temel_revizyon)
    except (RevisionError, timing.RevisionTimingError, ValueError) as exc:
        logger.error(f"revizyon üretilemedi: {exc}")
        _Progress(args.task_id, "", args.temel_revizyon or "")("failed", error=str(exc)[:300])
        return 1
    except Exception as exc:
        logger.exception(f"revizyon üretilemedi: {exc}")
        _Progress(args.task_id, "", args.temel_revizyon or "")("failed", error=f"{type(exc).__name__}: {exc}"[:300])
        return 1
    finally:
        batch_review.release_busy(folder)
    print(json.dumps({"task_id": args.task_id, "revision": record["revision"],
                      "video": str(file_path(args.task_id, record, "video")),
                      "check_required": record["check_required"]}, ensure_ascii=False))
    return 0


PACKAGE_ACTION = "paketi-yenile"


def package_cli(argv: list[str], prog: str = "batch.py") -> int:
    """``batch.py paketi-yenile <klasör> <sıra> [--revizyon rNNN]``: 0 = written, 1 = failed, 2 = invalid, 3 = busy."""
    from app.services import batch_production as bp
    from app.services import batch_review
    from app.utils.logging_utils import configure_terminal_logger, get_logging_options

    command_parser = argparse.ArgumentParser(
        prog=f"{prog} {PACKAGE_ACTION}",
        description="Toplu üretimdeki bir görevin revizyonu için yükleme paketini <paket>/revizyonlar/rNNN/ "
                    "altına yazar; özgün paket olduğu gibi kalır.",
    )
    command_parser.add_argument("batch_dir")
    command_parser.add_argument("index", type=int)
    command_parser.add_argument("--revizyon", help="varsayılan: görevin son revizyonu")
    args = command_parser.parse_args(argv)
    configure_terminal_logger(sys.stderr, level=get_logging_options()[0], colorize=False)
    try:
        state = bp.BatchState.load(Path(args.batch_dir))
        item = next((entry for entry in state.items if entry.index == args.index), None)
        if item is None or not item.task_id:
            raise RevisionError(f"{args.index}. konunun görevi yok")
        preset = bp.load_preset(state.preset)
        # The package's texts and translations use the preset's LLM, in this process only.
        bp.apply_app_overrides(preset)
        bp._cli_module().apply_llm_overrides(bp._parse_cli(bp.build_task_argv(preset, item)))
        folder = revisions_dir(item.task_id)
        folder.mkdir(exist_ok=True)
        batch_review.acquire_busy(folder, PACKAGE_ACTION)
    except (RevisionError, OSError, ValueError, bp.PresetError) as exc:
        logger.error(str(exc))
        return 2
    except batch_review.ReviewBusy as exc:
        logger.error(str(exc))
        return 3
    try:
        target = refresh_package(args.batch_dir, args.index, args.revizyon)
    except Exception as exc:
        logger.exception(f"paket yenilenemedi: {exc}")
        return 1
    finally:
        batch_review.release_busy(folder)
    print(json.dumps({"package": str(target)}, ensure_ascii=False))
    return 0


def start_package_refresh(batch_dir: str | Path, index: int, task_id: str, revision: str) -> int:
    """``batch.py paketi-yenile`` in its own process, locked like a revision of the task."""
    from app.services import batch_review, batch_runner

    folder = revisions_dir(task_id)
    marker = batch_review.busy(folder)
    if marker:
        raise batch_review.ReviewBusy(marker)
    arguments = [PACKAGE_ACTION, str(Path(batch_dir).resolve()), str(index), "--revizyon", revision]
    environment = dict(os.environ, PYTHONIOENCODING="utf-8")
    with open(folder / LOG_FILE, "a", encoding="utf-8") as log:
        process = subprocess.Popen(
            [*batch_runner._entry(), *arguments], stdin=subprocess.DEVNULL, stdout=log,
            stderr=subprocess.STDOUT, cwd=batch_runner._working_directory(), env=environment,
            **batch_runner._process_options(),
        )
    batch_runner._children[process.pid] = process
    if not batch_review.mark_busy(folder, PACKAGE_ACTION, process.pid):
        raise batch_review.ReviewBusy(batch_review.busy(folder) or {})
    return process.pid


def command(task_id: str, arguments: list[str]) -> list[str]:
    if paths.is_source_checkout():
        return [sys.executable, "-X", "utf8", str(paths.SOURCE_ROOT / "cli.py"), "revizyon", str(task_id), *arguments]
    # pip installs reach cli.py through the launcher (mpt cli).
    return [sys.executable, "-X", "utf8", "-m", "app.launcher", "cli", "revizyon", str(task_id), *arguments]


def write_text_handover(task_id: str, script: str) -> Path:
    """The WebUI passes an edited text to the background process in a file; the process removes it."""
    folder = revisions_dir(task_id)
    folder.mkdir(exist_ok=True)
    path = folder / f".metin-{uuid.uuid4().hex[:12]}.txt"
    path.write_text(script, encoding="utf-8")
    return path


def busy(task_id: str) -> dict | None:
    from app.services import batch_review

    folder = revisions_dir(task_id)
    return batch_review.busy(folder) if folder.is_dir() else None


def start_background(task_id: str, arguments: list[str]) -> int:
    """Run ``cli.py revizyon`` in its own process (the WebUI process never changes config.app)."""
    from app.services import batch_review, batch_runner

    folder = revisions_dir(task_id)
    folder.mkdir(exist_ok=True)
    marker = batch_review.busy(folder)
    if marker:
        raise batch_review.ReviewBusy(marker)
    progress_path(task_id).unlink(missing_ok=True)
    environment = dict(os.environ, PYTHONIOENCODING="utf-8")
    with open(folder / LOG_FILE, "a", encoding="utf-8") as log:
        process = subprocess.Popen(
            command(task_id, arguments), stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
            cwd=batch_runner._working_directory(), env=environment, **batch_runner._process_options(),
        )
    batch_runner._children[process.pid] = process
    if not batch_review.mark_busy(folder, ACTION, process.pid):
        raise batch_review.ReviewBusy(batch_review.busy(folder) or {})
    return process.pid
