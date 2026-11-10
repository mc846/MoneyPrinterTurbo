"""Snapshot of the materials a video task used, kept in the task's folder.

Every task that downloads materials gets ``storage/tasks/<task_id>/materials/``:

- the material files the video used: for NASA the original image, the prepared
  (cropped) image and the zoom clip, for stock video the downloaded video, for local
  materials the uploaded file and the clip rendered from it. They are hard links to
  the material cache when it is on the same drive, so they take no extra space and
  deleting the cache or the task leaves the other intact; otherwise copies.
- ``candidates/``: small previews of the first candidates of every search term.
- ``materials.json``: which file every scene used, where it came from, which part of
  it, why it was chosen and which candidates were passed over (``SCHEMA_VERSION``).

The pipeline reports to a recorder bound to the task by ``task_scope``, a context
variable like ``generation_progress``; worker threads inherit it through
``copy_context``. Every hook does nothing without a recorder, so code outside a task
and ``[app] save_task_materials = false`` keep the old behaviour. Recording never
fails a task: errors are logged and the video goes on.

The manifest is written when the materials are downloaded, after every assembled
video and when the task ends, so a failed task keeps what it had downloaded.

Scenes keep what upstream's local video projects (``app/services/video_project.py``)
need to rebuild one: a stable scene ID, the footage file, its SHA-256 and duration.
"""

import hashlib
import io
import json
import os
import re
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

import requests
from loguru import logger
from PIL import Image

from app.config import config
from app.services import task_artifacts
from app.utils import utils

SCHEMA_VERSION = 1
FOLDER = "materials"
MANIFEST = "materials.json"
CANDIDATES = "candidates"
# Candidates kept per term: the ranked list is usually 10-50 long, and only its head
# competes for a scene.
CANDIDATES_PER_TERM = 5
PREVIEW_SIZE = 320
_PREVIEW_MAX_BYTES = 10 * 1024 * 1024
_PREVIEW_TIMEOUT = (5, 20)
_PREVIEW_WORKERS = 4
_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")
_COMBINED_VIDEO = re.compile(r"combined-(\d+)\.mp4$")

_current: ContextVar["_Recorder | None"] = ContextVar("task_materials", default=None)


def enabled() -> bool:
    """``[app] save_task_materials``, on unless set to a false value."""
    value = config.app.get("save_task_materials", True)
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "no", "off", ""}
    return bool(value)


def recording() -> bool:
    return _current.get() is not None


def folder(task_id: str) -> Path:
    return Path(utils.task_dir()) / str(task_id) / FOLDER


def load(task_dir: str | Path) -> dict | None:
    """The task's manifest, or None if it has none or it cannot be read."""
    path = Path(task_dir) / FOLDER / MANIFEST
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return manifest if isinstance(manifest, dict) else None


def material_sources(manifest: dict) -> list[dict]:
    """The ``material_sources`` records of script.json, rebuilt from a manifest.

    Materials a revision added (``added_by``) belong to that revision's video only."""
    return [
        dict(material["source"])
        for material in manifest.get("materials") or []
        if isinstance(material, dict) and isinstance(material.get("source"), dict)
        and not material.get("added_by")
    ]


def credits_text(task_dir: str | Path) -> str | None:
    """credits.txt for the task from its manifest; None without a manifest."""
    manifest = load(task_dir)
    if manifest is None:
        return None
    from app.services import nasa

    return nasa.credits_text(material_sources(manifest))


# ------------------------------------------------------------------ recording


def _info(item) -> dict:
    info = getattr(item, "source_info", None)
    return info if isinstance(info, dict) else {}


def _provider(item) -> str:
    return str(getattr(item, "provider", "") or _info(item).get("provider") or "")


def _key(item) -> tuple[str, str]:
    asset_id = _info(item).get("asset_id")
    if asset_id not in (None, ""):
        return _provider(item), str(asset_id)
    return _provider(item), str(getattr(item, "url", "") or "")


class _Recorder:
    def __init__(self, task_id: str, params):
        self.task_id = str(task_id)
        self.params = params
        self.lock = threading.RLock()
        self.write_lock = threading.Lock()
        self.terms: list[str] = []
        self.anchors: list[str] = []
        self.searches: dict[tuple[str, str], dict] = {}
        self.fallbacks: dict[str, dict] = {}
        self.source_filters: dict[tuple[str, str], dict] = {}
        self.rejections: dict[tuple[str, str], list[dict]] = {}
        self.derived: dict[str, dict[str, str]] = {}
        self.downloads: list[tuple[Any, str, dict]] = []
        self.local: dict[str, str] = {}
        self.timelines: dict[int, list[dict]] = {}
        self.stored: dict[str, dict] = {}
        self.previews: dict[tuple[str, str], dict] = {}

    def has_data(self) -> bool:
        return bool(self.searches or self.downloads or self.local or self.timelines)


@contextmanager
def task_scope(task_id: str, params):
    """Record the materials of the task run inside this block, if enabled."""
    try:
        active = enabled()
    except Exception:
        active = False
    if not active:
        yield None
        return
    recorder = _Recorder(task_id, params)
    token = _current.set(recorder)
    try:
        yield recorder
    finally:
        _current.reset(token)
        if recorder.has_data():
            _write(recorder, final=True)


def started(search_terms: Iterable[str], anchors: Sequence[str] = ()) -> None:
    """The terms the materials are searched for, in script order, and the task's anchors."""
    recorder = _current.get()
    if recorder:
        with recorder.lock:
            recorder.terms = [str(term) for term in search_terms or []]
            recorder.anchors = [str(anchor) for anchor in anchors or []]


def searched(provider: str, term: str, ordered: Sequence, dropped: Sequence = ()) -> None:
    """Candidates of one term in rank order, and ``(item, reason)`` pairs the ranking removed."""
    recorder = _current.get()
    if recorder:
        with recorder.lock:
            recorder.searches[(str(provider), str(term))] = {
                "ordered": list(ordered),
                "dropped": list(dropped),
            }


def source_decision(term: str, decision: dict) -> None:
    """Which stock source of the fallback chain a term's candidates came from and why
    (source_chain.chain_search): the sources tried with their candidate and suitable
    counts, the chosen source and the reason. materials.json → terms[].source_chain."""
    recorder = _current.get()
    if recorder:
        with recorder.lock:
            recorder.fallbacks[str(term)] = dict(decision)


def fallback(term: str, **decision) -> None:
    """The older name of ``source_decision`` (keyword arguments)."""
    source_decision(term, decision)


def _decided_source(decision: dict | None) -> str:
    """The source a recorded decision chose ("chosen"; the single fallback wrote "used")."""
    decision = decision or {}
    return str(decision.get("chosen") or decision.get("used") or "")


def source_filtered(provider: str, term: str, skipped: dict, unchecked: int = 0) -> None:
    """Results a source dropped before ranking, counted by reason (NASA search)."""
    recorder = _current.get()
    if recorder:
        with recorder.lock:
            recorder.source_filters[(str(provider), str(term))] = {
                "skipped": {str(reason): int(count) for reason, count in dict(skipped).items()},
                "unchecked": int(unchecked),
            }


def rejected(item, filter_name: str, detail: str = "") -> None:
    """A candidate a filter turned down after ranking (NASA text and face filters)."""
    _record_filter(item, filter_name, "rejected", detail)


def failed(item, stage: str, detail: str = "") -> None:
    """A chosen candidate that could not be downloaded or prepared.

    The first record of a candidate wins: a filter's rejection or the specific NASA
    failure explains the empty download that follows it better than "no file".
    """
    recorder = _current.get()
    if recorder:
        with recorder.lock:
            if recorder.rejections.get(_key(item)):
                return
        _record_filter(item, stage, "failed", detail)


def _record_filter(item, filter_name: str, result: str, detail: str) -> None:
    recorder = _current.get()
    if recorder:
        with recorder.lock:
            entries = recorder.rejections.setdefault(_key(item), [])
            if result == "rejected":
                # A filter's verdict explains the candidate better than the empty download it caused.
                entries[:] = [entry for entry in entries if entry["result"] != "failed"]
            entry = {"filter": filter_name, "result": result}
            if detail:
                entry["detail"] = detail
            if entry not in entries:
                entries.append(entry)


def derived_files(clip_path: str, *, original: str = "", prepared: str = "") -> None:
    """Files a material clip was made from (NASA: downloaded image and its crop)."""
    recorder = _current.get()
    if recorder and clip_path:
        with recorder.lock:
            recorder.derived[os.path.realpath(clip_path)] = {
                role: path for role, path in (("original", original), ("prepared", prepared)) if path
            }


def downloaded(item, path: str, source_record: dict) -> None:
    """A material the task downloaded and will use, with its script.json record."""
    recorder = _current.get()
    if recorder and path:
        with recorder.lock:
            recorder.downloads.append((item, str(path), dict(source_record)))


def local_material(original: str, processed: str) -> None:
    """A local material: the uploaded file and the clip the video uses."""
    recorder = _current.get()
    if recorder and processed:
        with recorder.lock:
            recorder.local[os.path.realpath(processed)] = str(original or processed)


def materials_ready() -> None:
    """The materials are downloaded: write the folder and the manifest."""
    recorder = _current.get()
    if recorder:
        _write(recorder)


def timeline(combined_video_path: str, segments: Sequence[dict]) -> None:
    """The clips one assembled video consists of: source file, start and duration."""
    recorder = _current.get()
    if not recorder:
        return
    match = _COMBINED_VIDEO.search(os.path.basename(str(combined_video_path)))
    with recorder.lock:
        index = int(match.group(1)) if match else len(recorder.timelines) + 1
        recorder.timelines[index] = [dict(segment) for segment in segments]
    _write(recorder)


# ------------------------------------------------------------------ writing


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _name_part(value: str, limit: int = 60) -> str:
    return _UNSAFE_NAME.sub("_", str(value or "")).strip("._")[:limit] or "x"


def _source_label(path: str) -> str:
    """Where a file came from, relative to storage/ so no user folder ends up in the manifest."""
    real = os.path.realpath(path)
    storage = os.path.realpath(utils.storage_dir())
    try:
        if os.path.commonpath([real, storage]) == storage:
            return Path(os.path.relpath(real, storage)).as_posix()
    except ValueError:
        pass
    return os.path.basename(real)


def store_file(source: str | Path, target: Path) -> str:
    """Hard-link ``source`` to ``target`` (same drive), else copy it. Returns how."""
    source, target = Path(source), Path(target)
    if target.exists():
        try:
            if os.path.samefile(source, target):
                return "hardlink"
        except OSError:
            pass
        target.unlink()
    try:
        os.link(source, target)
        return "hardlink"
    except OSError:
        shutil.copy2(source, target)
        return "copy"


def _stored(recorder: _Recorder, folder_path: Path, source: str, name: str) -> dict | None:
    """Put ``source`` in the materials folder once; later writes reuse the entry."""
    real = os.path.realpath(source)
    entry = recorder.stored.get(real)
    if entry and (folder_path / entry["path"]).exists():
        return entry
    if not os.path.isfile(real):
        return None
    target = folder_path / f"{name}{Path(real).suffix.lower()}"
    how = store_file(real, target)
    entry = {
        "path": target.name,
        "sha256": _sha256(target),
        "size": target.stat().st_size,
        "stored_as": how,
        "source": _source_label(real),
    }
    recorder.stored[real] = entry
    return entry


def _relevance(item, term: str, anchors: Sequence[str]) -> dict | None:
    """Tag relevance as ranked by material._order_material_candidates; None without tags."""
    from app.services import material

    tags = material._material_tag_words(item)
    if tags is None:
        return None
    ratio, hits = material._tag_relevance(term, item)
    matched = []
    for anchor in anchors:
        words = set(material._relevance_words(anchor))
        if words and words <= tags:
            matched.append(anchor)
    return {"score": round(float(ratio), 4), "tag_hits": int(hits), "anchors": matched}


def _term_provider(recorder: _Recorder, term: str) -> str:
    decided = _decided_source(recorder.fallbacks.get(term))
    if decided:
        return decided
    params_source = str(getattr(recorder.params, "video_source", "") or "")
    if (params_source, term) in recorder.searches:
        return params_source
    return next((provider for provider, searched_term in recorder.searches if searched_term == term), "")


def _preview_url(item) -> str:
    if _provider(item) == "nasa":
        from app.services import nasa

        return nasa._preview_url(item)
    preview = _info(item).get("preview")
    return preview if isinstance(preview, str) and preview.startswith("https://") else ""


def _download_preview(url: str, target: Path) -> None:
    """Save a small JPEG of the preview at ``url``; NASA previews the text filter cached are reused."""
    from app.services import material

    cached = Path(utils.storage_dir("cache_videos")) / f"nasa-{utils.md5(url)}{Path(url.split('?')[0]).suffix.lower() or '.jpg'}"
    if cached.is_file() and cached.stat().st_size > 0:
        data = cached.read_bytes()
    else:
        with requests.get(
            url, proxies=config.proxy, verify=material._get_tls_verify(),
            timeout=_PREVIEW_TIMEOUT, stream=True,
        ) as response:
            response.raise_for_status()
            chunks, size = [], 0
            for chunk in response.iter_content(chunk_size=256 * 1024):
                size += len(chunk)
                if size > _PREVIEW_MAX_BYTES:
                    raise ValueError("preview is larger than 10 MB")
                chunks.append(chunk)
            data = b"".join(chunks)
    with Image.open(io.BytesIO(data)) as image:
        image = image.convert("RGB")
        image.thumbnail((PREVIEW_SIZE, PREVIEW_SIZE))
        temporary = target.with_name(f".{target.name}.tmp")
        image.save(temporary, "JPEG", quality=80)
    os.replace(temporary, target)


def _save_previews(recorder: _Recorder, folder_path: Path, wanted: list[tuple]) -> None:
    """Save the wanted previews once per task and point their candidate entries at them."""
    pending = {}
    for key, url, name, _entry in wanted:
        if key not in recorder.previews:
            pending.setdefault(key, (url, name))
    if pending:
        candidates_path = folder_path / CANDIDATES
        candidates_path.mkdir(exist_ok=True)

        def save(item):
            key, (url, name) = item
            target = candidates_path / f"{name}.jpg"
            try:
                _download_preview(url, target)
                return key, {"path": f"{CANDIDATES}/{target.name}"}
            except Exception as exc:
                return key, {"path": None, "error": type(exc).__name__}

        with ThreadPoolExecutor(max_workers=_PREVIEW_WORKERS, thread_name_prefix="material-preview") as pool:
            for key, result in pool.map(save, pending.items()):
                recorder.previews[key] = result
    for key, _url, _name, entry in wanted:
        preview = recorder.previews.get(key) or {}
        entry["preview"] = preview.get("path")
        if preview.get("error"):
            entry["preview_error"] = preview["error"]


def _candidate_entries(recorder, term_index, term, ordered, used_by_term, previews):
    used_here = used_by_term.get(term, set())
    used_anywhere = {key: used_term for used_term, keys in used_by_term.items() for key in keys}
    higher_used = False
    entries = []
    for rank, item in enumerate(ordered[:CANDIDATES_PER_TERM], start=1):
        key = _key(item)
        info = _info(item)
        filters = list(recorder.rejections.get(key, []))
        entry = {
            "rank": rank,
            "provider": _provider(item),
            "asset_id": info.get("asset_id"),
            "source_page": info.get("source_page"),
            "duration": getattr(item, "duration", None),
            "relevance": _relevance(item, term, recorder.anchors),
            "filters": filters,
        }
        if key in used_here:
            entry["status"] = "selected"
            higher_used = True
        elif any(f["result"] == "rejected" for f in filters):
            entry["status"] = "rejected"
            entry["reason"] = "filtered: " + ", ".join(
                sorted({f["filter"] for f in filters if f["result"] == "rejected"})
            )
        elif filters:
            entry["status"] = "failed"
            entry["reason"] = "could not be used: " + "; ".join(
                f"{f['filter']}" + (f" ({f['detail']})" if f.get("detail") else "") for f in filters
            )
        elif key in used_anywhere:
            entry["status"] = "not_selected"
            entry["reason"] = f"already used for the term {used_anywhere[key]!r}"
        elif higher_used:
            entry["status"] = "not_selected"
            entry["reason"] = "a higher-ranked candidate was used"
        else:
            entry["status"] = "not_selected"
            entry["reason"] = "not needed: the voiceover was already covered"
        entry["item"] = _item_record(item)
        entry["preview"] = None
        url = _preview_url(item)
        if url:
            name = f"{term_index:02d}-{rank:02d}-{_name_part(entry['provider'], 20)}-{_name_part(entry['asset_id'] or rank)}"
            previews.append((key, url, name, entry))
        entries.append(entry)
    return entries


STOCK_PROVIDERS = ("pexels", "pixabay", "coverr", "nasa")


def _item_record(item) -> dict | None:
    """What a revision needs to download a candidate later (task_revisions adds scenes).

    Only stock sources, whose media URLs are public; query strings are dropped, so no
    key or token is stored. The source info is the search cache's whitelist."""
    from app.services import material, material_cache

    provider = _provider(item)
    url = material._safe_public_url(getattr(item, "url", ""))
    if provider not in STOCK_PROVIDERS or not url:
        return None
    return {
        "provider": provider,
        "url": url,
        "duration": getattr(item, "duration", None),
        "source_info": material_cache._cached_source_info(item),
    }


def manifest_path(task_id: str) -> Path:
    return folder(task_id) / MANIFEST


def stored_path(task_id: str, entry: dict) -> Path:
    """Path of a file entry of the manifest (``materials[].files[role]``)."""
    return folder(task_id) / entry["path"]


def cache_path(entry: dict) -> Path | None:
    """Where the file came from under storage/ (None if it was outside storage)."""
    source = str(entry.get("source") or "")
    if "/" not in source:
        return None
    return Path(utils.storage_dir()) / source


def add_material(task_id: str, item, roles: dict[str, str], source_record: dict, *,
                 search_term: str, term_index: int | None, added_by: str,
                 relevance: dict | None = None) -> dict:
    """Store files of a material a revision added and append it to materials.json."""
    path = manifest_path(task_id)
    manifest = json.loads(path.read_text(encoding="utf-8"))
    folder_path = folder(task_id)
    number = len(manifest.get("materials") or []) + 1
    info = _info(item)
    provider = _provider(item)
    stem = f"{number:02d}-{_name_part(provider, 20)}-{_name_part(info.get('asset_id') or number)}"
    files = {}
    for role in ("original", "prepared", "clip", "video", "local"):
        if roles.get(role) and os.path.isfile(roles[role]):
            target = folder_path / f"{stem}-{role}{Path(roles[role]).suffix.lower()}"
            how = store_file(roles[role], target)
            files[role] = {
                "path": target.name, "sha256": _sha256(target), "size": target.stat().st_size,
                "stored_as": how, "source": _source_label(roles[role]),
            }
    material = {
        "id": f"m{number:02d}",
        "provider": provider,
        "asset_id": info.get("asset_id"),
        "source_page": info.get("source_page"),
        "credit": info.get("credit"),
        "creator": source_record.get("creator"),
        "search_term": search_term,
        "term_index": term_index,
        "duration": getattr(item, "duration", None),
        "relevance": relevance,
        "filters": [],
        "files": files,
        "source": dict(source_record),
        "added_by": added_by,
    }
    manifest.setdefault("materials", []).append(material)
    manifest["updated_at"] = _now()
    task_artifacts._write_json_atomic(path, manifest)
    return material


def add_search_results(task_id: str, recorder: _Recorder, words: str, scene_id: str, limit: int) -> dict:
    """Write a revision search (scene_edits.search) to materials.json as a candidate list.

    ``recorder`` captured the search: ranked results, filter rejections and the fallback
    decision. The new ``terms`` entry has ``scene`` (the scene it was searched for); its
    candidates are "available" or "rejected", with small previews like the task's own."""
    path = manifest_path(task_id)
    manifest = json.loads(path.read_text(encoding="utf-8"))
    decision = recorder.fallbacks.get(words) or {}
    provider = _decided_source(decision) or next((p for p, term in recorder.searches if term == words), "")
    search = recorder.searches.get((provider, words)) or {"ordered": [], "dropped": []}
    index = max([term.get("index", 0) for term in manifest.get("terms") or []] or [0]) + 1
    entries, previews = [], []
    for rank, item in enumerate(search["ordered"][:limit], start=1):
        key = _key(item)
        info = _info(item)
        filters = list(recorder.rejections.get(key, []))
        rejected = [f["filter"] for f in filters if f["result"] == "rejected"]
        entry = {
            "rank": rank, "provider": _provider(item), "asset_id": info.get("asset_id"),
            "source_page": info.get("source_page"), "duration": getattr(item, "duration", None),
            "relevance": _relevance(item, words, recorder.anchors), "filters": filters,
            "status": "rejected" if rejected else "available",
            "reason": "filtered: " + ", ".join(sorted(set(rejected))) if rejected else None,
            "item": _item_record(item), "preview": None,
        }
        url = _preview_url(item)
        if url:
            name = f"{index:02d}-{rank:02d}-{_name_part(entry['provider'], 20)}-{_name_part(entry['asset_id'] or rank)}"
            previews.append((key, url, name, entry))
        entries.append(entry)
    _save_previews(recorder, folder(task_id), previews)
    term_entry = {
        "index": index, "term": words, "provider": provider or None, "scene": scene_id,
        "searched_at": _now(), "source_chain": decision or None,
        "source_filters": recorder.source_filters.get((provider, words)),
        "candidates_found": len(search["ordered"]), "candidates": entries,
        "dropped": [{"provider": _provider(item), "asset_id": _info(item).get("asset_id"), "reason": reason}
                    for item, reason in search["dropped"][:limit]],
        "dropped_count": len(search["dropped"]),
    }
    manifest.setdefault("terms", []).append(term_entry)
    manifest["updated_at"] = _now()
    task_artifacts._write_json_atomic(path, manifest)
    return term_entry


def _settings(params) -> dict:
    names = (
        "video_source", "video_source_fallback", "video_aspect", "video_concat_mode",
        "video_clip_duration", "video_clip_speed", "match_materials_to_script",
        "crop_materials_to_aspect", "material_tag_relevance", "exclude_ai_generated_materials",
        "video_terms_style", "image_motion", "video_count",
    )
    settings = {}
    for name in names:
        value = getattr(params, name, None)
        settings[name] = getattr(value, "value", value)
    settings["candidates_per_term"] = CANDIDATES_PER_TERM
    settings["performance"] = performance_settings(params)
    return settings


PERFORMANCE_FIELDS = ("final_render_engine", "material_concurrency", "video_clip_concurrency", "video_codec",
                      "n_threads")


def performance_settings(params) -> dict:
    """Settings that change only how fast a video is made, as this task used them.

    The batch process applies its preset's values to config.app before the task runs, so a
    batch task records its preset's settings. Revisions start from these (task_revisions)."""
    recorded = {name: config.app.get(name) for name in PERFORMANCE_FIELDS if name != "n_threads"}
    recorded["n_threads"] = getattr(params, "n_threads", None)
    return recorded


def _task_status(task_id: str) -> str:
    from app.models import const
    from app.services import state as sm

    try:
        task = sm.state.get_task(task_id) or {}
    except Exception:
        return "unknown"
    state = task.get("state")
    if state == const.TASK_STATE_COMPLETE:
        return "complete"
    if state == const.TASK_STATE_FAILED:
        return "failed"
    return "running"


def _write(recorder: _Recorder, final: bool = False) -> None:
    try:
        with recorder.write_lock:
            _write_manifest(recorder, final)
    except Exception as exc:
        logger.warning(
            f"failed to save task materials: task_id={recorder.task_id}, "
            f"error={type(exc).__name__}, detail={exc}"
        )


def _write_manifest(recorder: _Recorder, final: bool) -> None:
    with recorder.lock:
        terms = list(recorder.terms)
        downloads = list(recorder.downloads)
        local = dict(recorder.local)
        timelines = {index: list(segments) for index, segments in recorder.timelines.items()}
    for provider, term in list(recorder.searches):
        if term not in terms:
            terms.append(term)
    folder_path = folder(recorder.task_id)
    folder_path.mkdir(parents=True, exist_ok=True)

    # Materials the video uses, in download order (local materials in upload order).
    materials, by_path, used_by_term = [], {}, {}
    for item, path, source_record in downloads:
        real = os.path.realpath(path)
        if real in by_path:
            continue
        number = len(materials) + 1
        info = _info(item)
        provider = _provider(item)
        term = str(info.get("search_term") or source_record.get("search_term") or "")
        stem = f"{number:02d}-{_name_part(provider, 20)}-{_name_part(info.get('asset_id') or Path(real).stem)}"
        roles = dict(recorder.derived.get(real, {}))
        roles["clip" if roles else "video"] = real
        files = {}
        for role in ("original", "prepared", "clip", "video"):
            if role in roles:
                entry = _stored(recorder, folder_path, roles[role], f"{stem}-{role}")
                if entry:
                    files[role] = entry
        material = {
            "id": f"m{number:02d}",
            "provider": provider,
            "asset_id": info.get("asset_id"),
            "source_page": info.get("source_page"),
            "credit": info.get("credit"),
            "creator": source_record.get("creator"),
            "search_term": term,
            "term_index": terms.index(term) + 1 if term in terms else None,
            "duration": getattr(item, "duration", None),
            "relevance": _relevance(item, term, recorder.anchors),
            "filters": list(recorder.rejections.get(_key(item), [])),
            "files": files,
            "source": source_record,
        }
        materials.append(material)
        by_path[real] = material
        used_by_term.setdefault(term, set()).add(_key(item))
    for processed, original in local.items():
        if processed in by_path:
            continue
        if not os.path.isfile(original):
            # Uploads are named relative to the local materials folder.
            original = os.path.join(utils.storage_dir("local_videos"), os.path.basename(original))
        number = len(materials) + 1
        stem = f"{number:02d}-local-{_name_part(Path(original).stem)}"
        files = {}
        entry = _stored(recorder, folder_path, original, f"{stem}-local")
        if entry:
            files["local"] = entry
        if os.path.realpath(original) != processed:
            entry = _stored(recorder, folder_path, processed, f"{stem}-clip")
            if entry:
                files["clip"] = entry
        material = {
            "id": f"m{number:02d}", "provider": "local", "asset_id": None, "source_page": None,
            "credit": None, "creator": None, "search_term": None, "term_index": None,
            "duration": None, "relevance": None, "filters": [], "files": files,
            "source": {"provider": "local", "local_file": os.path.basename(processed)},
        }
        materials.append(material)
        by_path[processed] = material

    # Search terms with their ranked candidates.
    term_entries, previews = [], []
    for term_index, term in enumerate(terms, start=1):
        provider = _term_provider(recorder, term)
        search = recorder.searches.get((provider, term)) or {"ordered": [], "dropped": []}
        ordered = search["ordered"]
        term_entries.append({
            "index": term_index,
            "term": term,
            "provider": provider or None,
            # The fallback chain's decision for the term (sources tried, candidate and
            # suitable counts, chosen source, reason); None without a chain.
            "source_chain": recorder.fallbacks.get(term),
            "source_filters": recorder.source_filters.get((provider, term)),
            "candidates_found": len(ordered),
            "candidates": _candidate_entries(
                recorder, term_index, term, ordered, used_by_term, previews
            ),
            "dropped": [
                {"provider": _provider(item), "asset_id": _info(item).get("asset_id"), "reason": reason}
                for item, reason in search["dropped"][:CANDIDATES_PER_TERM]
            ],
            "dropped_count": len(search["dropped"]),
        })
    _save_previews(recorder, folder_path, previews)

    # The timeline of every assembled video.
    scenes = []
    for video_index in sorted(timelines):
        for position, segment in enumerate(timelines[video_index], start=1):
            source = os.path.realpath(str(segment.get("source") or ""))
            material = by_path.get(source)
            footage = None
            if material:
                footage = material["files"].get("clip") or material["files"].get("video") or material["files"].get("local")
            scenes.append({
                "id": f"v{video_index}-s{position:02d}",
                "video": video_index,
                "index": position,
                "material": material["id"] if material else None,
                "search_term": material["search_term"] if material else None,
                "term_index": material["term_index"] if material else None,
                "anchors": (material["relevance"] or {}).get("anchors", []) if material else [],
                "provider": material["provider"] if material else None,
                "asset_id": material["asset_id"] if material else None,
                "source_page": material["source_page"] if material else None,
                "credit": material["credit"] if material else None,
                "file": footage["path"] if footage else None,
                "sha256": footage["sha256"] if footage else None,
                "source_file": None if material else _source_label(source),
                "start": round(float(segment.get("start") or 0.0), 3),
                "duration": round(float(segment.get("duration") or 0.0), 3),
                "relevance": material["relevance"] if material else None,
                "filters": material["filters"] if material else [],
            })

    status = _task_status(recorder.task_id)
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "task_id": recorder.task_id,
        "status": status if final or status in ("complete", "failed") else "running",
        "stage": "video" if timelines else "materials",
        "updated_at": _now(),
        "settings": _settings(recorder.params),
        "anchors": list(recorder.anchors),
        "terms": term_entries,
        "materials": materials,
        "scenes": scenes,
    }
    task_artifacts._write_json_atomic(folder_path / MANIFEST, manifest)
