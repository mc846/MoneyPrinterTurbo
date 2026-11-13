"""Scene operations of a revision: replace, remove and crop, with their rules in one place.

A revision (``task_revisions``) can change single scenes besides the text, voice,
subtitles and music. The CLI (``cli.py revizyon --sahne-…``, ``--kirpma``) and the WebUI
revision panel build the same ``SceneOp`` list and use the functions here;
``task_revisions.create`` applies them to the base revision's scenes before the
durations are fitted (``revision_timing``).

- Replace with a saved candidate: one of the candidates materials.json keeps for the
  scene's term (or found for the scene by a search). A candidate a filter turned down
  or that could not be used cannot be chosen; ``candidate_choices`` says why.
- Replace with a new search: the user's words, searched with the task's source and
  fallback, filters and relevance settings. The results become candidates in
  materials.json (``search``); the CLI takes the first usable one, the WebUI lets the
  user choose.
- Replace with a local file: an image or a video. The NASA face and text checks run on
  an image as warnings only; the credit is "kullanıcı".
- Remove: the scene is dropped and the duration rules spread its time over the rest.
  The last remaining scene cannot be removed.
- Crop (image scenes only): the crop window's position, x and y from 0 to 1 (0.5 is the
  centre; y matters only for a source taller than the video). The window keeps its size,
  so it must still meet the output resolution. The position is part of the prepared
  image's file name and so of the zoom clip's cache key (``nasa.prepare_image``).

A replaced scene keeps its ID; revision.json records each change as old → new, and a
new material enters materials.json marked as added by the revision.
"""

import os
import shutil
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

from loguru import logger
from PIL import Image

from app.models.schema import MaterialInfo, VideoAspect
from app.services import task_materials

REPLACE, REMOVE, CROP = "replace", "remove", "crop"
LOCAL_CREDIT = "kullanıcı"
CENTER = (0.5, 0.5)
SEARCH_RESULTS = 12
IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp", ".bmp")
VIDEO_SUFFIXES = (".mp4", ".mov", ".avi", ".flv", ".mkv", ".webm")
UNUSABLE = ("rejected", "failed")


class SceneEditError(ValueError):
    """A scene operation that breaks a rule (unknown scene, filtered candidate, last scene...)."""


@dataclass(frozen=True)
class SceneOp:
    op: str
    scene: str
    candidate: str = ""
    search: str = ""
    path: str = ""
    position: tuple[float, float] | None = None
    prompt: str = ""  # fiction (ai_manual) tasks: the Veo prompt of the new clip

    def to_dict(self) -> dict:
        return {key: value for key, value in asdict(self).items() if value not in ("", None)}

    @classmethod
    def from_dict(cls, data: dict) -> "SceneOp":
        position = data.get("position")
        return cls(op=data["op"], scene=data["scene"], candidate=data.get("candidate", ""),
                   search=data.get("search", ""), path=data.get("path", ""),
                   position=tuple(position) if position else None, prompt=data.get("prompt", ""))

    def describe(self) -> str:
        if self.op == REMOVE:
            return f"{self.scene}: remove"
        if self.op == CROP:
            return f"{self.scene}: crop {self.position[0]:.2f},{self.position[1]:.2f}"
        what = self.candidate or (f"search {self.search!r}" if self.search else os.path.basename(self.path))
        return f"{self.scene}: replace with {what}"


# ---------------------------------------------------------------- command line


def parse_position(text: str) -> tuple[float, float]:
    """"0.3" (x only) or "0.3,0.6" (x,y); each from 0 to 1."""
    parts = [part.strip() for part in str(text).replace(";", ",").split(",") if part.strip()]
    try:
        values = [float(part) for part in parts]
    except ValueError as exc:
        raise SceneEditError(f"crop position must be numbers from 0 to 1: {text!r}") from exc
    if len(values) == 1:
        values.append(CENTER[1])
    if len(values) != 2 or not all(0.0 <= value <= 1.0 for value in values):
        raise SceneEditError(f"crop position must be x or x,y from 0 to 1: {text!r}")
    return values[0], values[1]


def _split(value: str, flag: str) -> tuple[str, str]:
    scene, sep, rest = str(value).partition("=")
    if not sep or not scene.strip() or not rest.strip():
        raise SceneEditError(f"{flag} expects <scene>=<value>, got {value!r}")
    return scene.strip(), rest.strip().strip('"')


def from_arguments(args) -> list[SceneOp]:
    """SceneOps from the revizyon command's --sahne-degistir/-ara/-dosya/-kaldir and --kirpma flags."""
    ops = []
    for value in args.sahne_degistir or []:
        scene, candidate = _split(value, "--sahne-degistir")
        ops.append(SceneOp(REPLACE, scene, candidate=candidate))
    for value in args.sahne_ara or []:
        scene, words = _split(value, "--sahne-ara")
        ops.append(SceneOp(REPLACE, scene, search=words))
    prompts = dict(_split(value, "--sahne-istemi") for value in getattr(args, "sahne_istemi", None) or [])
    for value in args.sahne_dosya or []:
        scene, path = _split(value, "--sahne-dosya")
        ops.append(SceneOp(REPLACE, scene, path=path, prompt=prompts.pop(scene, "")))
    if prompts:
        raise SceneEditError(f"--sahne-istemi needs a --sahne-dosya for the same scene: {', '.join(prompts)}")
    for value in args.sahne_kaldir or []:
        ops.append(SceneOp(REMOVE, str(value).strip()))
    for value in args.kirpma or []:
        scene, position = _split(value, "--kirpma")
        ops.append(SceneOp(CROP, scene, position=parse_position(position)))
    return ops


def to_arguments(ops: list[SceneOp]) -> list[str]:
    """The revizyon command's flags for these operations (the WebUI starts the command with them)."""
    arguments = []
    for op in ops:
        if op.op == REMOVE:
            arguments.append(f"--sahne-kaldir={op.scene}")
        elif op.op == CROP:
            arguments.append(f"--kirpma={op.scene}={op.position[0]:.3f},{op.position[1]:.3f}")
        elif op.candidate:
            arguments.append(f"--sahne-degistir={op.scene}={op.candidate}")
        elif op.search:
            arguments.append(f"--sahne-ara={op.scene}={op.search}")
        else:
            arguments.append(f"--sahne-dosya={op.scene}={op.path}")
            if op.prompt:
                arguments.append(f"--sahne-istemi={op.scene}={op.prompt}")
    return arguments


# ---------------------------------------------------------------- candidates


def candidate_key(candidate: dict) -> str:
    return f"{candidate.get('provider')}:{candidate.get('asset_id')}"


def candidate_choices(manifest: dict, materials: dict, scene: dict, scenes: list[dict] | None = None) -> list[dict]:
    """Candidates the scene can take: its term's saved candidates and the searches made for it.

    Each has ``selectable`` and, when false, ``reason`` (English, for logs and the CLI)
    and ``reason_code`` (filtered, failed, current, other_scene with ``used_by``,
    no_address; the WebUI translates it). A filtered or failed candidate cannot be
    chosen; neither can one without a download address (older manifests keep none for
    Pixabay; NASA's is rebuilt) nor one another scene of ``scenes`` uses."""
    in_use = {}
    for other in scenes or []:
        used = materials.get(other.get("material") or "", {})
        if used and other.get("id") != scene.get("id"):
            in_use[(used.get("provider"), str(used.get("asset_id")))] = other["id"]
    current = materials.get(scene.get("material") or "", {})
    choices, seen = [], set()
    for term in manifest.get("terms") or []:
        if term.get("index") != scene.get("term_index") and term.get("scene") != scene.get("id"):
            continue
        for candidate in term.get("candidates") or []:
            key = candidate_key(candidate)
            if key in seen:
                continue
            seen.add(key)
            entry = dict(candidate, key=key, term=term.get("term"), term_index=term.get("index"),
                         search=bool(term.get("scene")))
            identity = (candidate.get("provider"), str(candidate.get("asset_id")))
            if candidate.get("status") in UNUSABLE:
                entry["selectable"] = False
                entry["reason"] = candidate.get("reason") or candidate.get("status")
                entry["reason_code"] = "filtered" if candidate.get("status") == "rejected" else "failed"
                entry["filters_rejected"] = sorted({f.get("filter") for f in candidate.get("filters") or []
                                                    if f.get("result") == "rejected" and f.get("filter")})
            elif identity == (current.get("provider"), str(current.get("asset_id"))):
                entry["selectable"] = False
                entry["reason"] = "the scene uses it now"
                entry["reason_code"] = "current"
            elif identity in in_use:
                entry["selectable"] = False
                entry["reason"] = f"used by another scene ({in_use[identity]})"
                entry["reason_code"] = "other_scene"
                entry["used_by"] = in_use[identity]
            elif not (candidate.get("item") or {}).get("url") and candidate.get("provider") != "nasa":
                entry["selectable"] = False
                entry["reason"] = "no download address recorded"
                entry["reason_code"] = "no_address"
            else:
                entry["selectable"] = True
                entry["reason"] = None
                entry["reason_code"] = None
            choices.append(entry)
    return choices


TASK_CHAIN = ""  # search_sources' value for "the task's source and fallback chain"


def search_sources(task_id: str) -> dict:
    """Where a new search can look: {"chain": [primary, *fallbacks], "sources": [...]}.

    ``chain`` is the task's own (the default); ``sources`` are the stock sources a single
    search can use: NASA, and the others whose API key is set."""
    from app.config import config
    from app.services import source_chain
    from app.services import task_revisions as revisions

    params = revisions.ensure_first(task_id)["params"]
    primary = str(params.get("video_source") or "")
    chain = [primary, *source_chain.parse_lenient(params.get("video_source_fallback"))]
    sources = [source for source in source_chain.STOCK_SOURCES if source_chain.has_api_key(config.app, source)]
    return {"chain": chain if primary in source_chain.STOCK_SOURCES else [], "sources": sources}


def search(task_id: str, scene: dict, words: str, source: str = TASK_CHAIN) -> dict:
    """Search ``words`` for a scene with the task's filters and relevance settings: on the
    task's source and fallback chain (the default) or on one stock ``source``. The results
    become a candidate list of materials.json (``terms[]`` with ``scene``; each candidate
    keeps its provider) and the entry is returned."""
    from app.services import material
    from app.services import task_revisions as revisions

    words = " ".join(str(words).split())
    if not words:
        raise SceneEditError("the search is empty")
    first = revisions.ensure_first(task_id)
    params = first["params"]
    manifest, _materials = revisions._materials(task_id)
    anchors = list(manifest.get("anchors") or [])
    options = {
        "crop_to_aspect": bool(params.get("crop_materials_to_aspect")),
        "tag_relevance": params.get("material_tag_relevance") or "off",
        "exclude_ai_generated": bool(params.get("exclude_ai_generated_materials")),
        "anchors": anchors,
    }
    if source:
        source = str(source).strip().lower()
        if source not in search_sources(task_id)["sources"]:
            raise SceneEditError(f"{source!r} is not a stock source with an API key")
        search_videos, _order = material._stock_source_search(source, **options)
    else:
        source = str(params.get("video_source") or "")
        if source not in task_materials.STOCK_PROVIDERS:
            raise SceneEditError(f"searching needs a stock source; the task used {source!r}")
        search_videos, _order = material._stock_source_search(source, **options)
        chain = material.resolve_fallback_chain(source, params.get("video_source_fallback") or "")
        if chain:
            search_videos, _order = material.chain_source_search(source, chain, **options)
    recorder = task_materials._Recorder(task_id, None)
    recorder.anchors = anchors
    token = task_materials._current.set(recorder)
    try:
        search_videos(search_term=words, minimum_duration=int(params.get("video_clip_duration") or 5),
                      video_aspect=VideoAspect(params.get("video_aspect") or VideoAspect.portrait))
    finally:
        task_materials._current.reset(token)
    return task_materials.add_search_results(task_id, recorder, words, scene["id"], SEARCH_RESULTS)


# ---------------------------------------------------------------- checks


def _resolution_ok(image_path: str, aspect: VideoAspect) -> bool:
    from app.services import material

    with Image.open(image_path) as image:
        width, height = image.size
    crop_width, crop_height = material._center_crop_size(width, height, aspect)
    target_width, target_height = aspect.to_resolution()
    return crop_width >= target_width and crop_height >= target_height


def _image_for_crop(task_id: str, material_record: dict) -> Path | None:
    files = material_record.get("files") or {}
    entry = files.get("original") or files.get("local")
    if not entry or Path(entry["path"]).suffix.lower() not in IMAGE_SUFFIXES:
        return None
    return task_materials.stored_path(task_id, entry)


def validate(task_id: str, ops: list[SceneOp], scenes: list[dict], manifest: dict, materials: dict,
             aspect: VideoAspect) -> None:
    """Refuse operations that break a rule, before anything is downloaded or rendered."""
    by_id = {scene["id"]: scene for scene in scenes}
    replaced, removed = set(), set()
    for op in ops:
        scene = by_id.get(op.scene)
        if scene is None:
            raise SceneEditError(f"no scene {op.scene} in the base revision")
        if op.op == REMOVE:
            if op.scene in removed:
                raise SceneEditError(f"{op.scene} is removed twice")
            removed.add(op.scene)
        elif op.op == REPLACE:
            if op.scene in replaced:
                raise SceneEditError(f"{op.scene} is replaced twice")
            replaced.add(op.scene)
            if op.candidate:
                choice = next((c for c in candidate_choices(manifest, materials, scene, scenes)
                               if c["key"] == op.candidate), None)
                if choice is None:
                    raise SceneEditError(f"{op.candidate} is not a candidate of {op.scene}'s term")
                if not choice["selectable"]:
                    raise SceneEditError(f"{op.candidate} cannot be used: {choice['reason']}")
            elif op.path:
                suffix = Path(op.path).suffix.lower()
                if not os.path.isfile(op.path):
                    raise SceneEditError(f"no such file: {op.path}")
                if suffix not in IMAGE_SUFFIXES + VIDEO_SUFFIXES:
                    raise SceneEditError(f"{suffix or 'this file'} is neither an image nor a video")
            elif not op.search:
                raise SceneEditError(f"{op.scene}: replace needs a candidate, a search or a file")
        elif op.op == CROP:
            if op.position is None or not all(0.0 <= value <= 1.0 for value in op.position):
                raise SceneEditError(f"{op.scene}: crop position must be from 0 to 1")
            replacement = next((o for o in ops if o.op == REPLACE and o.scene == op.scene), None)
            if replacement is None:
                if scene.get("kind") != "image":
                    raise SceneEditError(f"{op.scene} is a video scene; only image scenes can be cropped")
                image = _image_for_crop(task_id, materials.get(scene["material"], {}))
                if image is None:
                    raise SceneEditError(f"{op.scene} has no source image to crop")
                if not _resolution_ok(str(image), aspect):
                    raise SceneEditError(f"{op.scene}: the cropped area would be below the output resolution")
        else:
            raise SceneEditError(f"unknown scene operation {op.op!r}")
    if removed and len(by_id) - len(removed) < 1:
        raise SceneEditError("the last scene cannot be removed")
    if removed & replaced:
        raise SceneEditError(f"{sorted(removed & replaced)[0]} is both removed and replaced")


# ---------------------------------------------------------------- local files


def local_file_warnings(path: str, aspect: VideoAspect, clip_duration: float, motion) -> tuple[str, list[str]]:
    """The NASA checks on a user's image, as warnings: (prepared image or "", warnings)."""
    from app.services import nasa

    warnings = []
    if Path(path).suffix.lower() not in IMAGE_SUFFIXES:
        return "", warnings
    lines = nasa.crop_text_lines(path, aspect)
    if lines:
        warnings.append(f"text in the crop area ({len(lines)} line(s))")
    prepared = nasa.prepare_image(path, aspect, clip_duration, motion)
    if not prepared:
        warnings.append("below the output resolution; the image is enlarged")
    faces = nasa.count_faces(prepared or path) or 0
    if not faces and prepared:
        faces = nasa.count_edge_faces(path, aspect) or 0
    if faces:
        warnings.append(f"face(s) in the image: {faces}")
    return prepared, warnings


def add_fiction_clip(task_id: str, revision: str, scene: dict, path: str, previous: dict | None,
                     prompt: str = "") -> tuple[dict, list[str]]:
    """
    A new clip for a fiction (ai_manual) scene: validated like the batch intake (readable video, at least the
    scene's duration, short side ≥ 720; another aspect and sound are warnings), copied into the task, recorded
    as ai_manual with the tool, the prompt (the edited one, else the scene's), the SHA-256 and the label.
    """
    from datetime import datetime

    from app.services import fiction
    from app.utils import utils

    source = Path(path)
    if source.suffix.lower() not in fiction.VIDEO_SUFFIXES:
        raise SceneEditError(f"{scene['id']}: a fiction scene takes a video clip, not {source.name}")
    info = fiction.probe(source)
    status, reason, warnings = fiction.validate_clip(info, float(scene.get("duration") or 0))
    if status != fiction.ARRIVED:
        raise SceneEditError(f"{scene['id']}: {reason}")
    folder = Path(utils.task_dir(task_id)) / "ai_clips"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{scene['id']}-{revision}{source.suffix.lower()}"
    shutil.copy2(source, target)
    previous = previous or {}
    asset = str(previous.get("asset_id") or scene["id"])
    info_record = {
        "provider": fiction.PROVIDER, "asset_id": asset, "search_term": scene.get("search_term") or "",
        "credit": previous.get("tool") or fiction.TOOL, "tool": previous.get("tool") or fiction.TOOL,
        "prompt": (prompt or previous.get("prompt") or "").strip(), "style_version": previous.get("style_version") or "",
        "sha256": fiction._sha256(target), "uploaded_at": datetime.now().isoformat(timespec="seconds"),
        "scene_label": previous.get("scene_label") or fiction.SCENE_LABEL, "ai_generated": True,
    }
    from app.services import material

    item = MaterialInfo(provider=fiction.PROVIDER, url=str(target), duration=max(1, int(info["duration"])),
                        source_info=info_record)
    # The whitelisted record (outside a task run the materials recorder is inactive: nothing else happens).
    record = material._material_source_record(item, str(target))
    added = task_materials.add_material(task_id, item, {"video": str(target)}, record,
                                        search_term=scene.get("search_term") or "", term_index=scene.get("term_index"),
                                        added_by=revision)
    for warning in warnings:
        logger.warning(f"revizyon {revision}: {scene['id']} kurgu klibi: {warning}")
    return added, warnings


def add_local_file(task_id: str, revision: str, scene: dict, path: str, aspect: VideoAspect,
                   clip_duration: float, motion) -> tuple[dict, list[str]]:
    """Copy a user's file into the local materials folder and add it to materials.json."""
    from app.services import material_upload

    source = Path(path)
    folder = Path(material_upload.uploaded_material_dir())
    if source.resolve().parent != folder.resolve():
        target = folder / f"{uuid.uuid4().hex}{source.suffix.lower()}"
        shutil.copy2(source, target)
        source = target
    prepared, warnings = local_file_warnings(str(source), aspect, clip_duration, motion)
    is_image = source.suffix.lower() in IMAGE_SUFFIXES
    roles = {"local": str(source)}
    if prepared:
        roles["prepared"] = prepared
    if not is_image:
        roles = {"video": str(source)}
    item = MaterialInfo(provider="local", url=str(source), duration=int(clip_duration),
                        source_info={"provider": "local", "asset_id": source.name, "credit": LOCAL_CREDIT,
                                     "search_term": scene.get("search_term")})
    record = {"provider": "local", "local_file": source.name, "duration": int(clip_duration),
              "asset_id": source.name, "credit": LOCAL_CREDIT}
    added = task_materials.add_material(task_id, item, roles, record, search_term=scene.get("search_term") or "",
                                        term_index=scene.get("term_index"), added_by=revision)
    for warning in warnings:
        logger.warning(f"revizyon {revision}: {scene['id']} yerel dosya: {warning}")
    return added, warnings
