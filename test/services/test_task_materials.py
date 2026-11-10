"""Task materials snapshot: materials/ with linked files, candidate previews and materials.json."""

import hashlib
import io
import json
import os
import subprocess
import time
import wave
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app import asgi
from app.config import config
from app.controllers.v1 import video as video_controller
from app.models import const
from app.models.schema import MaterialInfo, VideoAspect, VideoConcatMode, VideoParams
from app.services import cache_manager, material, nasa, task, task_artifacts, task_materials, video
from app.services import state as sm
from app.utils import utils


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _jpeg(size=(1000, 600), color=(30, 60, 90)):
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, "JPEG")
    return buffer.getvalue()


@pytest.fixture
def storage(tmp_path, monkeypatch):
    """Task folders and the material cache under tmp_path; previews never touch the network."""
    root = tmp_path / "storage"

    def storage_dir(sub_dir="", create=False):
        folder = root / sub_dir if sub_dir else root
        if create:
            folder.mkdir(parents=True, exist_ok=True)
        return str(folder)

    def task_dir(sub_dir=""):
        folder = root / "tasks" / sub_dir if sub_dir else root / "tasks"
        folder.mkdir(parents=True, exist_ok=True)
        return str(folder)

    monkeypatch.setattr(utils, "storage_dir", storage_dir)
    monkeypatch.setattr(utils, "task_dir", task_dir)
    cache = root / "cache_videos"
    cache.mkdir(parents=True)

    class Response:
        def __init__(self, data):
            self.data = data

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def raise_for_status(self):
            pass

        def iter_content(self, chunk_size):
            yield self.data

    downloads = []

    def fake_get(url, **kwargs):
        downloads.append(url)
        return Response(_jpeg((640, 360), (200, 120, 40)))

    monkeypatch.setattr(task_materials.requests, "get", fake_get)
    monkeypatch.setitem(config.app, "save_task_materials", True)
    return {"root": root, "cache": cache, "preview_downloads": downloads}


def _nasa(asset_id, term, tags):
    return MaterialInfo(
        provider="nasa",
        url=f"https://images-assets.nasa.gov/image/{asset_id}/{asset_id}~orig.jpg",
        duration=5,
        source_info={
            "provider": "nasa", "search_term": term, "asset_id": asset_id,
            "source_page": f"https://images.nasa.gov/details/{asset_id}",
            "credit": f"NASA/{asset_id}", "tags": tags,
            "preview": f"https://images-assets.nasa.gov/image/{asset_id}/{asset_id}~small.jpg",
            "ai_generated": False,
        },
    )


def _pixabay(asset_id, term, tags):
    return MaterialInfo(
        provider="pixabay",
        url=f"https://cdn.pixabay.com/video/{asset_id}.mp4",
        duration=9,
        source_info={
            "provider": "pixabay", "search_term": term, "asset_id": asset_id,
            "source_page": f"https://pixabay.com/videos/id-{asset_id}/",
            "creator": {"id": "7", "name": "Kaffeesüchtig"}, "tags": tags,
            "preview": f"https://cdn.pixabay.com/video/{asset_id}-tiny.jpg",
            "ai_generated": False,
        },
    )


SEARCH = {
    ("nasa", "full moon"): [
        _nasa("moon-1", "full moon", ["full moon", "moon"]),
        _nasa("moon-2", "full moon", ["full moon", "moon"]),
        _nasa("moon-3", "full moon", ["moon"]),
    ],
    ("nasa", "vintage satellite"): [_nasa("sat-0", "vintage satellite", ["rocket"])],
    ("pixabay", "vintage satellite"): [
        _pixabay("px-1", "vintage satellite", ["vintage", "satellite"]),
        _pixabay("px-2", "vintage satellite", ["satellite"]),
    ],
}


def _download(task_id, storage, monkeypatch, text_rejected=("moon-1",), unavailable=()):
    """Run the real material stage of a NASA task with a Pixabay fallback, offline.

    ``text_rejected`` assets have text in the crop; ``unavailable`` ones fail to download."""
    cache = storage["cache"]

    def nasa_search(search_term, minimum_duration, video_aspect, **kwargs):
        nasa.task_materials.source_filtered("nasa", search_term, {"person": 2}, 1)
        return list(SEARCH[("nasa", search_term)])

    def pixabay_search(search_term, minimum_duration, video_aspect, **kwargs):
        return list(SEARCH[("pixabay", search_term)])

    def download_image(url, save_dir):
        if any(f"/{asset}/" in url for asset in unavailable):
            return ""
        original = Path(save_dir) / f"nasa-{utils.md5(url)}.jpg"
        original.write_bytes(_jpeg() + url.encode())
        return str(original)

    def prepare_image(image_path, video_aspect, clip_duration, image_motion):
        prepared = Path(image_path).with_name(f"{Path(image_path).stem}-portrait-1080x1920.jpg")
        prepared.write_bytes(_jpeg((540, 960)) + image_path.encode())
        return str(prepared)

    def render_clip(prepared_path, duration, image_motion=None, output_size=None):
        clip = cache / f"{hashlib.sha256(prepared_path.encode()).hexdigest()}.zoom-0123456789abcdef.mp4"
        clip.write_bytes(b"zoom clip of " + prepared_path.encode())
        return str(clip)

    def save_video(video_url, save_dir=""):
        target = cache / f"vid-{utils.md5(video_url)}.mp4"
        target.write_bytes(b"stock video " + video_url.encode())
        return str(target)

    def text_lines(preview_url, video_aspect, save_dir):
        rejected = any(f"/{asset}/" in preview_url for asset in text_rejected)
        return [{"height": 0.12, "score": 0.97}] if rejected else []

    task_artifacts.write_script_data(task_id, {"script": "s", "search_terms": [], "params": {}})
    monkeypatch.setattr(nasa, "search_images", nasa_search)
    monkeypatch.setattr(material, "search_videos_pixabay", pixabay_search)
    monkeypatch.setattr(material, "_has_api_key", lambda key: True)
    monkeypatch.setattr(nasa, "_download_image", download_image)
    monkeypatch.setattr(nasa, "prepare_image", prepare_image)
    monkeypatch.setattr(nasa.video, "render_image_zoom_video", render_clip)
    monkeypatch.setattr(material, "save_video", save_video)
    monkeypatch.setattr(nasa, "text_filter_active", lambda: True)
    monkeypatch.setattr(nasa, "face_filter_active", lambda: False)
    monkeypatch.setattr(nasa, "face_filter_enabled", lambda: False)
    monkeypatch.setattr(nasa, "_preview_text_lines", text_lines)
    monkeypatch.setattr(material.material_cache, "load_material_search_cache", lambda *a, **k: None)
    monkeypatch.setattr(material.material_cache, "save_material_search_cache", lambda *a, **k: None)
    monkeypatch.setitem(config.app, "material_directory", "")
    return material.download_videos(
        task_id=task_id,
        search_terms=["full moon", "vintage satellite"],
        source="nasa",
        fallback_source="pixabay",
        video_aspect=VideoAspect.portrait,
        audio_duration=8,
        max_clip_duration=5,
        match_script_order=True,
        tag_relevance="rank",
        anchors=["moon"],
    )


def _params():
    return VideoParams(video_subject="Ay", video_source="nasa", video_source_fallback="pixabay")


def _manifest(task_id):
    return json.loads((task_materials.folder(task_id) / task_materials.MANIFEST).read_text(encoding="utf-8"))


def test_manifest_records_scenes_materials_and_candidates(storage, monkeypatch):
    task_id = str(uuid4())
    with task_materials.task_scope(task_id, _params()):
        paths = _download(task_id, storage, monkeypatch)
        assert len(paths) == 2
        after_download = _manifest(task_id)
        task_materials.timeline(
            str(Path(utils.task_dir(task_id)) / "combined-1.mp4"),
            [
                {"source": paths[0], "start": 0.0, "duration": 5.0},
                {"source": paths[1], "start": 5.0, "duration": 3.0},
            ],
        )
        sm.state.update_task(task_id, state=const.TASK_STATE_COMPLETE)
    try:
        manifest = _manifest(task_id)
    finally:
        sm.state.delete_task(task_id)
    folder = task_materials.folder(task_id)

    assert after_download["stage"] == "materials" and after_download["status"] == "running"
    assert manifest["schema_version"] == task_materials.SCHEMA_VERSION
    assert manifest["task_id"] == task_id
    assert (manifest["status"], manifest["stage"]) == ("complete", "video")
    assert manifest["anchors"] == ["moon"]
    assert manifest["settings"]["video_source"] == "nasa"

    moon, satellite = manifest["terms"]
    assert (moon["index"], moon["term"], moon["provider"]) == (1, "full moon", "nasa")
    assert moon["source_filters"] == {"skipped": {"person": 2}, "unchecked": 1}
    assert [(c["asset_id"], c["status"]) for c in moon["candidates"]] == [
        ("moon-1", "rejected"), ("moon-2", "selected"), ("moon-3", "not_selected"),
    ]
    rejected, chosen, lower = moon["candidates"]
    assert rejected["reason"] == "filtered: text"
    assert rejected["filters"][0]["filter"] == "text"
    assert lower["reason"] == "a higher-ranked candidate was used"
    assert chosen["relevance"] == {"score": 1.0, "tag_hits": 2, "anchors": ["moon"]}
    assert satellite["provider"] == "pixabay"
    # The chain's decision: every source tried with its counts, the chosen one and why.
    chain = satellite["source_chain"]
    assert (chain["chosen"], chain["reason"], chain["chain"]) == ("pixabay", "met", ["nasa", "pixabay"])
    assert [(e["source"], e["suitable"]) for e in chain["tried"]] == [("nasa", 0), ("pixabay", 2)]
    assert moon["source_chain"]["tried"][0]["source"] == "nasa" and moon["source_chain"]["chosen"] == "nasa"
    assert [(c["asset_id"], c["status"]) for c in satellite["candidates"]] == [
        ("px-1", "selected"), ("px-2", "not_selected"),
    ]

    # Candidate previews: small JPEGs in candidates/; NASA ones too (no cached preview here).
    previews = [c["preview"] for term in manifest["terms"] for c in term["candidates"]]
    assert all(previews) and len(set(previews)) == len(previews)
    for preview in previews:
        assert preview.startswith("candidates/")
        with Image.open(folder / preview) as image:
            assert max(image.size) <= task_materials.PREVIEW_SIZE

    nasa_material, pixabay_material = manifest["materials"]
    assert (nasa_material["id"], nasa_material["asset_id"]) == ("m01", "moon-2")
    assert set(nasa_material["files"]) == {"original", "prepared", "clip"}
    assert set(pixabay_material["files"]) == {"video"}
    for material_entry in manifest["materials"]:
        for entry in material_entry["files"].values():
            stored = folder / entry["path"]
            assert entry["sha256"] == _sha(stored)
            assert entry["size"] == stored.stat().st_size
            assert entry["stored_as"] == "hardlink"
            assert os.path.samefile(stored, utils.storage_dir() + "/" + entry["source"])
    assert nasa_material["files"]["original"]["source"].startswith("cache_videos/nasa-")
    script = json.loads((Path(utils.task_dir(task_id)) / "script.json").read_text(encoding="utf-8"))
    assert task_materials.material_sources(manifest) == script["material_sources"]

    first, second = manifest["scenes"]
    assert (first["id"], first["material"], first["search_term"]) == ("v1-s01", "m01", "full moon")
    assert first["anchors"] == ["moon"]
    assert (first["provider"], first["asset_id"], first["credit"]) == ("nasa", "moon-2", "NASA/moon-2")
    assert first["source_page"] == "https://images.nasa.gov/details/moon-2"
    assert first["file"] == nasa_material["files"]["clip"]["path"]
    assert first["sha256"] == nasa_material["files"]["clip"]["sha256"]
    assert (first["start"], first["duration"]) == (0.0, 5.0)
    assert (second["material"], second["start"], second["duration"]) == ("m02", 5.0, 3.0)


def test_a_candidate_that_could_not_be_downloaded_says_so(storage, monkeypatch):
    task_id = str(uuid4())
    with task_materials.task_scope(task_id, _params()):
        _download(task_id, storage, monkeypatch, text_rejected=(), unavailable=("moon-1",))
    moon = _manifest(task_id)["terms"][0]
    first, second, third = moon["candidates"]
    assert (first["asset_id"], first["status"]) == ("moon-1", "failed")
    assert first["filters"] == [{
        "filter": "download", "result": "failed",
        "detail": "the image could not be downloaded or opened",
    }]
    assert "download" in first["reason"]
    assert (second["asset_id"], second["status"]) == ("moon-2", "selected")
    assert third["reason"] == "a higher-ranked candidate was used"


def test_credits_follow_from_the_manifest_unchanged(storage, monkeypatch):
    task_id = str(uuid4())
    with task_materials.task_scope(task_id, _params()):
        _download(task_id, storage, monkeypatch)
    task_dir = Path(utils.task_dir(task_id))
    script = json.loads((task_dir / "script.json").read_text(encoding="utf-8"))
    written = nasa.write_credits_file(str(task_dir), script["material_sources"])
    expected = Path(written).read_text(encoding="utf-8")
    assert "NASA/moon-2" in expected
    assert task_materials.credits_text(task_dir) == expected
    assert task_materials.credits_text(storage["root"] / "missing") is None


def test_store_file_hard_links_on_the_same_drive(tmp_path):
    source = tmp_path / "cache.mp4"
    source.write_bytes(b"material")
    target = tmp_path / "task" / "01.mp4"
    target.parent.mkdir()
    assert task_materials.store_file(source, target) == "hardlink"
    assert os.path.samefile(source, target)
    assert os.stat(source).st_nlink == 2
    # Storing again keeps the link.
    assert task_materials.store_file(source, target) == "hardlink"


def test_store_file_copies_when_linking_fails(tmp_path, monkeypatch):
    source = tmp_path / "cache.mp4"
    source.write_bytes(b"material")
    target = tmp_path / "01.mp4"

    def no_link(*args):
        raise OSError(18, "Invalid cross-device link")

    monkeypatch.setattr(task_materials.os, "link", no_link)
    assert task_materials.store_file(source, target) == "copy"
    assert target.read_bytes() == b"material"
    assert not os.path.samefile(source, target)


def test_copy_fallback_is_recorded_in_the_manifest(storage, monkeypatch):
    monkeypatch.setattr(task_materials.os, "link", lambda *args: (_ for _ in ()).throw(OSError("no links")))
    task_id = str(uuid4())
    with task_materials.task_scope(task_id, _params()):
        _download(task_id, storage, monkeypatch)
    manifest = _manifest(task_id)
    entries = [entry for m in manifest["materials"] for entry in m["files"].values()]
    assert entries and {entry["stored_as"] for entry in entries} == {"copy"}
    for entry in entries:
        assert entry["sha256"] == _sha(task_materials.folder(task_id) / entry["path"])


def test_cache_cleanup_leaves_the_task_materials_readable(storage, monkeypatch):
    task_id = str(uuid4())
    with task_materials.task_scope(task_id, _params()):
        _download(task_id, storage, monkeypatch)
    manifest = _manifest(task_id)
    cache_files = [p for p in storage["cache"].iterdir() if p.is_file()]
    old = time.time() - 400 * 86400
    for path in cache_files:
        os.utime(path, (old, old))
    monkeypatch.setattr(cache_manager, "video_cache_dir", lambda: str(storage["cache"]))
    cache_manager.clean_video_cache(max_age_days=None)
    linked_sources = {entry["source"] for m in manifest["materials"] for entry in m["files"].values()}
    removed = [s for s in linked_sources if not (storage["root"] / s).exists()]
    assert removed, "the cleanup should have removed cache files the task links to"
    for m in manifest["materials"]:
        for entry in m["files"].values():
            assert _sha(task_materials.folder(task_id) / entry["path"]) == entry["sha256"]


def test_deleting_the_task_removes_materials_but_keeps_the_cache(storage, monkeypatch):
    task_id = str(uuid4())
    with task_materials.task_scope(task_id, _params()):
        _download(task_id, storage, monkeypatch)
    manifest = _manifest(task_id)
    sources = [storage["root"] / e["source"] for m in manifest["materials"] for e in m["files"].values()]
    state = sm.MemoryState()
    state.update_task(task_id, state=const.TASK_STATE_COMPLETE)
    with patch.dict(config.app, {"api_key": ""}), patch.object(video_controller.sm, "state", state):
        response = TestClient(asgi.get_application()).delete(f"/api/v1/tasks/{task_id}")
    assert response.status_code == 200
    assert not Path(utils.task_dir()).joinpath(task_id).exists()
    for source in sources:
        assert source.is_file()
        assert os.stat(source).st_nlink == 1


def test_a_failed_task_keeps_the_materials_it_downloaded(storage, monkeypatch):
    task_id = str(uuid4())

    def pipeline(task_id, params, **kwargs):
        _download(task_id, storage, monkeypatch)
        raise RuntimeError("render failed")

    monkeypatch.setattr(task, "_run_pipeline", pipeline)
    try:
        task.start(task_id, _params())
        assert sm.state.get_task(task_id)["state"] == const.TASK_STATE_FAILED
    finally:
        sm.state.delete_task(task_id)
    manifest = _manifest(task_id)
    assert (manifest["status"], manifest["stage"]) == ("failed", "materials")
    assert [m["asset_id"] for m in manifest["materials"]] == ["moon-2", "px-1"]
    assert manifest["scenes"] == []
    for m in manifest["materials"]:
        for entry in m["files"].values():
            assert (task_materials.folder(task_id) / entry["path"]).is_file()


def test_switched_off_keeps_the_old_behaviour(storage, monkeypatch):
    monkeypatch.setitem(config.app, "save_task_materials", "false")
    task_id = str(uuid4())
    with task_materials.task_scope(task_id, _params()) as recorder:
        assert recorder is None
        assert not task_materials.recording()
        paths = _download(task_id, storage, monkeypatch)
    assert len(paths) == 2
    assert not task_materials.folder(task_id).exists()
    script = json.loads((Path(utils.task_dir(task_id)) / "script.json").read_text(encoding="utf-8"))
    assert [record["asset_id"] for record in script["material_sources"]] == ["moon-2", "px-1"]
    assert storage["preview_downloads"] == []


def test_outside_a_task_nothing_is_recorded(storage, monkeypatch):
    task_id = str(uuid4())
    _download(task_id, storage, monkeypatch)
    assert not task_materials.folder(task_id).exists()


def test_a_cached_nasa_preview_is_reused(storage, monkeypatch):
    item = _nasa("moon-9", "full moon", ["moon"])
    url = nasa._preview_url(item)
    cached = storage["cache"] / f"nasa-{utils.md5(url)}.jpg"
    cached.write_bytes(_jpeg((640, 400)))
    target = storage["root"] / "preview.jpg"
    task_materials._download_preview(url, target)
    assert storage["preview_downloads"] == []
    with Image.open(target) as image:
        assert image.size == (320, 200)


def test_combine_videos_records_where_each_clip_starts(storage, tmp_path, monkeypatch):
    binary = video.utils.get_ffmpeg_binary()
    audio = tmp_path / "audio.wav"
    with wave.open(str(audio), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(8000)
        writer.writeframes(b"\0\0" * 8000 * 4)
    sources = []
    for name, color, seconds in (("a", "red", 3), ("b", "blue", 1.5)):
        source = tmp_path / f"{name}.mp4"
        subprocess.run(
            [binary, "-y", "-f", "lavfi", "-i", f"color=c={color}:s=64x64:r=10", "-t", str(seconds),
             "-c:v", "libx264", "-pix_fmt", "yuv420p", str(source)],
            check=True, capture_output=True,
        )
        sources.append(str(source))
    monkeypatch.setattr(VideoAspect, "to_resolution", lambda self: (64, 64))
    monkeypatch.setattr(video, "_get_configured_video_codec", lambda: "libx264")
    monkeypatch.setattr(video, "_get_effective_video_codec", lambda *args: "libx264")
    task_id = str(uuid4())
    output = Path(utils.task_dir(task_id)) / "combined-1.mp4"
    with task_materials.task_scope(task_id, _params()):
        video.combine_videos(
            str(output), sources, str(audio), video_concat_mode=VideoConcatMode.random,
            max_clip_duration=2, threads=1,
        )
    scenes = _manifest(task_id)["scenes"]
    starts = sorted((scene["source_file"], scene["start"]) for scene in scenes)
    assert starts == [("a.mp4", 0.0), ("a.mp4", 2.0), ("b.mp4", 0.0)]
    assert sum(scene["duration"] for scene in scenes) == pytest.approx(4.0, abs=0.01)
    assert [scene["id"] for scene in scenes] == ["v1-s01", "v1-s02", "v1-s03"]
