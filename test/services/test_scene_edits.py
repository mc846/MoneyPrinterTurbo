"""Scene operations of a revision (replace, remove, crop) and its speed settings."""
# The task fixtures come from test_task_revisions; tests name them as arguments.
# ruff: noqa: F811

import json
import subprocess
from pathlib import Path

import pytest
from PIL import Image

import cli
from app.config import config
from app.models.schema import MaterialInfo, VideoAspect, VideoParams
from app.services import material, material_upload, nasa, scene_edits, task_materials, task_revisions, video
from app.services import revision_timing as timing
from app.services.scene_edits import CROP, REMOVE, REPLACE, SceneEditError, SceneOp
from test.services.test_task_revisions import (  # noqa: F401  (fixtures)
    FF,
    _config_file_hash,
    _make_task,
    _revision_page,
    _sha,
    _video,
    audits,
    no_downloads,
    storage,
    voice_seconds,
)


def _manifest(storage, task_id):
    return json.loads((storage / "tasks" / task_id / "materials" / "materials.json").read_text(encoding="utf-8"))


def _write_manifest(storage, task_id, manifest):
    (storage / "tasks" / task_id / "materials" / "materials.json").write_text(json.dumps(manifest), encoding="utf-8")


def _two_colour_image(path, size=(640, 320), left="red", right="blue"):
    image = Image.new("RGB", size, left)
    image.paste(Image.new("RGB", (size[0] // 2, size[1]), right), (size[0] // 2, 0))
    image.save(path)
    return path


def _use_original(storage, task_id, material_id, image):
    """Give a NASA material another original image (a wide one, a tiny one...)."""
    folder = storage / "tasks" / task_id / "materials"
    target = folder / f"{material_id}-original{Path(image).suffix}"
    task_materials.store_file(image, target)
    manifest = _manifest(storage, task_id)
    entry = next(m for m in manifest["materials"] if m["id"] == material_id)
    entry["files"]["original"] = {"path": target.name, "sha256": _sha(target), "size": target.stat().st_size,
                                  "stored_as": "hardlink", "source": f"cache_videos/{Path(image).name}"}
    _write_manifest(storage, task_id, manifest)


def _frame_colour(video_file, seconds):
    """The centre pixel of a frame, as (r, g, b)."""
    frame = Path(video_file).with_suffix(f".{seconds}.png")
    subprocess.run([FF, "-y", "-loglevel", "error", "-ss", str(seconds), "-i", str(video_file), "-frames:v", "1",
                    str(frame)], check=True, capture_output=True)
    with Image.open(frame) as image:
        image = image.convert("RGB")
        return image.getpixel((image.width // 2, image.height // 2))


@pytest.fixture
def nasa_downloads(storage, monkeypatch):
    """Fake NASA downloads: each asset becomes a 9:16 image in the cache."""
    cache = storage / "cache_videos"
    calls = []

    def prepared_image(item, save_dir, aspect, clip_duration, motion):
        calls.append(item.url)
        name = item.url.rstrip("/").split("/")[-1].split("~")[0]
        original = cache / f"nasa-{name}.jpg"
        Image.new("RGB", (360, 640), "teal").save(original)
        prepared = cache / f"nasa-{name}-portrait.jpg"
        Image.new("RGB", (360, 640), "teal").save(prepared)
        return str(original), str(prepared)

    monkeypatch.setattr(nasa, "_prepared_image", prepared_image)
    return calls


# ---------------------------------------------------------------- speed settings


def _write_preset(storage, task_id, text):
    preset = storage / "preset.toml"
    preset.write_text(text, encoding="utf-8")
    batch = storage / "batches" / "b1"
    batch.mkdir(parents=True)
    (batch / "state.json").write_text(json.dumps({"preset": str(preset), "items": [{"index": 1, "task_id": task_id}]}),
                                      encoding="utf-8")


def test_speed_settings_come_from_explicit_task_preset_then_defaults_never_config(storage, monkeypatch):
    task_id = _make_task(storage)
    monkeypatch.setitem(config.app, "final_render_engine", "moviepy")
    monkeypatch.setitem(config.app, "material_concurrency", 1)
    monkeypatch.setitem(config.app, "video_clip_concurrency", 1)
    monkeypatch.setitem(config.app, "video_codec", "h264_nvenc")
    # An older task (no record, not in a batch): the fast defaults, not config.toml's values.
    resolved = task_revisions.performance(task_id)
    assert {name: value["value"] for name, value in resolved.items()} == task_revisions.PERFORMANCE_DEFAULTS
    assert {value["source"] for value in resolved.values()} == {"default"}
    # A batch task without a record: its preset.
    _write_preset(storage, task_id, '[video]\nfinal_render_engine = "moviepy"\n[app]\nmaterial_concurrency = 2\n')
    resolved = task_revisions.performance(task_id)
    assert resolved["final_render_engine"] == {"value": "moviepy", "source": "preset"}
    assert resolved["material_concurrency"] == {"value": 2, "source": "preset"}
    assert resolved["video_clip_concurrency"] == {"value": 3, "source": "default"}
    # The task's own record wins over the preset; explicit values over both.
    manifest = _manifest(storage, task_id)
    manifest["settings"] = {"performance": {"final_render_engine": "ffmpeg", "material_concurrency": 4,
                                            "video_clip_concurrency": 2, "video_codec": "libx264", "n_threads": 6}}
    _write_manifest(storage, task_id, manifest)
    resolved = task_revisions.performance(task_id, {"video_clip_concurrency": 5, "video_codec": ""})
    assert {name: (value["value"], value["source"]) for name, value in resolved.items()} == {
        "final_render_engine": ("ffmpeg", "task"), "material_concurrency": (4, "task"),
        "video_clip_concurrency": (5, "explicit"), "video_codec": ("libx264", "task"), "n_threads": (6, "task")}


def test_a_task_records_its_speed_settings(monkeypatch):
    monkeypatch.setitem(config.app, "final_render_engine", "ffmpeg")
    monkeypatch.setitem(config.app, "material_concurrency", 3)
    monkeypatch.setitem(config.app, "video_clip_concurrency", 2)
    monkeypatch.setitem(config.app, "video_codec", "libx264")
    params = VideoParams(video_subject="x", n_threads=4)
    assert task_materials._settings(params)["performance"] == {
        "final_render_engine": "ffmpeg", "material_concurrency": 3, "video_clip_concurrency": 2,
        "video_codec": "libx264", "n_threads": 4}


def test_speed_settings_apply_only_while_the_revision_renders(storage, voice_seconds, no_downloads, audits,
                                                             monkeypatch):
    task_id = _make_task(storage)
    monkeypatch.setitem(config.app, "final_render_engine", "moviepy")
    monkeypatch.setitem(config.app, "material_concurrency", 1)
    config.app.pop("video_codec", None)
    seen = {}
    original = video.generate_video

    def generate_video(**kwargs):
        seen.update({name: config.app.get(name) for name in ("final_render_engine", "material_concurrency",
                                                             "video_clip_concurrency", "video_codec")})
        seen["n_threads"] = kwargs["params"].n_threads
        return original(**kwargs)

    monkeypatch.setattr(video, "generate_video", generate_video)
    before = _config_file_hash()
    record = task_revisions.create(task_id, settings={"font_size": 70},
                                   performance_settings={"material_concurrency": 2, "n_threads": 2})
    assert seen == {"final_render_engine": "ffmpeg", "material_concurrency": 2, "video_clip_concurrency": 3,
                    "video_codec": None, "n_threads": 2}
    assert record["performance"]["material_concurrency"] == {"value": 2, "source": "explicit"}
    assert record["performance"]["final_render_engine"] == {"value": "ffmpeg", "source": "default"}
    # The process's settings are back and config.toml was not written.
    assert config.app["final_render_engine"] == "moviepy" and config.app["material_concurrency"] == 1
    assert "video_codec" not in config.app
    assert _config_file_hash() == before


# ---------------------------------------------------------------- replace


def test_replace_with_a_saved_candidate_keeps_the_scene_id_and_updates_credits(
        storage, voice_seconds, audits, nasa_downloads, monkeypatch):
    task_id = _make_task(storage)
    overlay = []
    monkeypatch.setattr(nasa, "overlay_credit_text",
                        lambda sources, language="": overlay.append([s.get("asset_id") for s in sources]) or "atıf")
    voice_seconds["seconds"] = None  # no TTS: text and voice unchanged
    record = task_revisions.create(task_id, settings={"nasa_credit_overlay": True},
                                   scene_ops=[SceneOp(REPLACE, "v1-s01", candidate="nasa:C1")])
    assert nasa_downloads == ["https://images-assets.nasa.gov/image/C1/C1~orig.jpg"]
    assert voice_seconds["calls"] == [] and record["network"]["tts"] is False
    assert [s["id"] for s in record["scenes"]] == ["v1-s01", "v1-s02", "v1-s03", "v1-s04"]
    first = record["scenes"][0]
    assert first["status"] == "replaced" and first["kind"] == "image"
    (change,) = record["scene_changes"]
    assert change["op"] == "replace" and change["candidate"] == "nasa:C1"
    assert (change["from"]["asset_id"], change["to"]["asset_id"]) == ("A1", "C1")
    assert record["changes"]["scenes"] == ["v1-s01: replace with nasa:C1"]
    manifest = _manifest(storage, task_id)
    added = [m for m in manifest["materials"] if m.get("added_by") == "r002"]
    assert [m["asset_id"] for m in added] == ["C1"] and first["material"] == added[0]["id"]
    assert record["blocked_terms"] == []  # a candidate of the scene's own term keeps the term
    # Credits come from the scenes the revision shows: C1 instead of A1, on screen and in credits.txt.
    assert [s["asset_id"] for s in record["material_sources"]] == ["C1", "A2", "A3", "A4"]
    assert overlay == [["C1", "A2", "A3", "A4"]]
    credits = (storage / "tasks" / task_id / "revisions" / "r002" / "credits.txt").read_text(encoding="utf-8")
    assert "C1" in credits and "NASA/3" in credits and "NASA/1" not in credits
    assert record["credits"]["sha256"]
    assert task_revisions.verify(task_id, "r002") == []
    # The task's own records stay as they were.
    assert len(task_materials.material_sources(manifest)) == 4


def test_filtered_and_unusable_candidates_cannot_be_chosen(storage, voice_seconds, no_downloads, audits):
    task_id = _make_task(storage)
    base = task_revisions.ensure_first(task_id)
    manifest, materials = task_revisions._materials(task_id)
    scene = base["scenes"][1]  # v1-s02, term 2
    choices = {c["key"]: c for c in scene_edits.candidate_choices(manifest, materials, scene, base["scenes"])}
    assert set(choices) == {"pixabay:A2", "pixabay:R2", "pixabay:C2"}
    assert (choices["pixabay:R2"]["selectable"], choices["pixabay:R2"]["reason"]) == (False, "rejected")
    assert (choices["pixabay:A2"]["selectable"], choices["pixabay:A2"]["reason"]) == (False, "the scene uses it now")
    assert choices["pixabay:C2"]["selectable"]
    with pytest.raises(SceneEditError, match="cannot be used: rejected"):
        task_revisions.create(task_id, scene_ops=[SceneOp(REPLACE, "v1-s02", candidate="pixabay:R2")])
    with pytest.raises(SceneEditError, match="not a candidate"):
        task_revisions.create(task_id, scene_ops=[SceneOp(REPLACE, "v1-s02", candidate="nasa:C1")])
    with pytest.raises(SceneEditError, match="no scene"):
        task_revisions.create(task_id, scene_ops=[SceneOp(REMOVE, "v1-s09")])
    assert task_revisions.names(task_id) == ["r001"]
    assert not [p for p in (storage / "tasks" / task_id / "revisions").iterdir() if p.name.endswith(".tmp")]


def _nasa_item(asset, title):
    return MaterialInfo(provider="nasa", url=f"https://images-assets.nasa.gov/image/{asset}/{asset}~orig.jpg",
                        duration=5, source_info={"provider": "nasa", "asset_id": asset, "title": title,
                                                 "credit": f"NASA/{asset}",
                                                 "source_page": f"https://images.nasa.gov/details/{asset}"})


def test_a_new_search_adds_candidates_and_replaces_with_the_first_usable(
        storage, voice_seconds, audits, nasa_downloads, monkeypatch):
    task_id = _make_task(storage)
    searched = []

    def stock_source_search(source, **options):
        searched.append((source, options["tag_relevance"]))

        def search(search_term, minimum_duration, video_aspect):
            items = [_nasa_item("TXT1", "earth rise poster"), _nasa_item("ER2", "earth rise"),
                     _nasa_item("ER3", "earth rise over the moon")]
            task_materials.searched("nasa", search_term, items)
            task_materials.rejected(items[0], "text", "2 lines")
            return items

        return search, {"tag_relevance": options["tag_relevance"]}

    monkeypatch.setattr(material, "_stock_source_search", stock_source_search)
    monkeypatch.setattr(task_materials, "_download_preview",
                        lambda url, target: Image.new("RGB", (32, 48), "gray").save(target, "JPEG"))
    record = task_revisions.create(task_id, scene_ops=[SceneOp(REPLACE, "v1-s03", search="earth rise")])
    assert searched == [("nasa", "off")]
    manifest = _manifest(storage, task_id)
    term = manifest["terms"][-1]
    assert (term["index"], term["term"], term["scene"]) == (5, "earth rise", "v1-s03")
    assert [(c["asset_id"], c["status"]) for c in term["candidates"]] == [
        ("TXT1", "rejected"), ("ER2", "available"), ("ER3", "available")]
    assert term["candidates"][0]["reason"] == "filtered: text"
    assert all(c["preview"] for c in term["candidates"])
    # The filtered first result is skipped; ER2 replaces the scene.
    (change,) = record["scene_changes"]
    assert (change["search"], change["candidate"], change["to"]["asset_id"]) == ("earth rise", "nasa:ER2", "ER2")
    assert nasa_downloads == ["https://images-assets.nasa.gov/image/ER2/ER2~orig.jpg"]
    # The search's results are the scene's candidates from now on (the WebUI shows them).
    scene = next(s for s in record["scenes"] if s["id"] == "v1-s03")
    _manifest_now, materials = task_revisions._materials(task_id)
    keys = [c["key"] for c in scene_edits.candidate_choices(manifest, materials, dict(scene, term_index=3))]
    assert keys == ["nasa:A3", "nasa:TXT1", "nasa:ER2", "nasa:ER3"]
    # A search's results are a choice for the user: never added on their own to fill time.
    assert record["blocked_terms"] == [3]
    added_on_their_own = [c.key for c in task_revisions._candidates(manifest, materials)[0]]
    assert "nasa:ER3" not in added_on_their_own and "pixabay:C2" in added_on_their_own


def test_a_term_replaced_from_outside_is_not_refilled_with_its_candidates(
        storage, voice_seconds, audits, nasa_downloads, monkeypatch):
    task_id = _make_task(storage)
    cache = storage / "cache_videos"
    monkeypatch.setattr(material, "save_video", lambda video_url, save_dir="": str(_video(cache / "vid-c2.mp4", 9)))
    picture = storage / "kendi.png"
    Image.new("RGB", (400, 700), "orange").save(picture)
    voice_seconds["seconds"] = 16.0
    record = task_revisions.create(task_id, script="daha uzun bir metin",
                                   scene_ops=[SceneOp(REPLACE, "v1-s01", path=str(picture))])
    # Term 1's scene was replaced by the user's file: its candidate C1 is not added back to
    # fill the longer voiceover (the "rocket part" case); term 2's C2 still is.
    assert record["blocked_terms"] == [1]
    assert [a["candidate"] for a in record["added"]] == ["pixabay:C2"]
    assert nasa_downloads == []
    assert record["extended"] == "v1-s03"
    # Later revisions keep the term turned down.
    later = task_revisions.create(task_id, script="daha da uzun bir metin oldu")
    assert later["blocked_terms"] == [1] and "nasa:C1" not in [a["candidate"] for a in later["added"]]


def test_a_local_file_warns_about_faces_and_text_but_is_used(storage, voice_seconds, no_downloads, audits,
                                                            monkeypatch):
    task_id = _make_task(storage)
    uploads = storage / "local_videos"
    monkeypatch.setattr(nasa, "crop_text_lines", lambda path, aspect: [{"score": 0.9, "height": 0.1}])
    monkeypatch.setattr(nasa, "count_faces", lambda path: 2)
    picture = storage / "benim.png"
    Image.new("RGB", (400, 700), "orange").save(picture)
    clip = _video(storage / "benim.mp4", 6, "green")
    record = task_revisions.create(task_id, scene_ops=[SceneOp(REPLACE, "v1-s03", path=str(picture)),
                                                       SceneOp(REPLACE, "v1-s02", path=str(clip))])
    by_scene = {change["scene"]: change for change in record["scene_changes"]}
    assert by_scene["v1-s03"]["warnings"] == ["text in the crop area (1 line(s))", "face(s) in the image: 2"]
    assert by_scene["v1-s02"]["warnings"] == []
    assert record["warnings"][-2:] == ["v1-s03: text in the crop area (1 line(s))", "v1-s03: face(s) in the image: 2"]
    kinds = {s["id"]: s["kind"] for s in record["scenes"]}
    assert (kinds["v1-s03"], kinds["v1-s02"]) == ("image", "video")
    added = [m for m in _manifest(storage, task_id)["materials"] if m.get("added_by") == "r002"]
    assert {m["provider"] for m in added} == {"local"} and {m["credit"] for m in added} == {"kullanıcı"}
    assert all(Path(m["source"]["local_file"]).parent == Path(".") for m in added)
    assert sorted(set(m["files"]) for m in added) == sorted([{"local", "prepared"}, {"video"}], key=sorted)
    assert {change["to"]["credit"] for change in record["scene_changes"]} == {"kullanıcı"}
    # The user's files were copied into the local materials folder; the originals stay.
    assert picture.is_file() and len(list(uploads.iterdir())) >= 2
    assert material_upload.uploaded_material_dir() == str(uploads)


def test_a_missing_or_wrong_kind_of_local_file_is_refused(storage):
    task_id = _make_task(storage)
    text = storage / "not-media.txt"
    text.write_text("x", encoding="utf-8")
    with pytest.raises(SceneEditError, match="no such file"):
        task_revisions.create(task_id, scene_ops=[SceneOp(REPLACE, "v1-s01", path=str(storage / "missing.jpg"))])
    with pytest.raises(SceneEditError, match="neither an image nor a video"):
        task_revisions.create(task_id, scene_ops=[SceneOp(REPLACE, "v1-s01", path=str(text))])


# ---------------------------------------------------------------- remove


def test_remove_spreads_the_time_and_the_scene_stays_out(storage, voice_seconds, no_downloads, audits):
    task_id = _make_task(storage)
    voice_seconds["seconds"] = None
    task_revisions.create(task_id, settings={"font_size": 70})  # the voiceover stays 11 s
    record = task_revisions.create(task_id, scene_ops=[SceneOp(REMOVE, "v1-s03")])
    assert [s["id"] for s in record["scenes"]] == ["v1-s01", "v1-s02", "v1-s04"]
    assert record["excluded"] == ["v1-s03"]
    assert sum(s["duration"] for s in record["scenes"]) == pytest.approx(11.0, abs=0.01)
    assert record["scene_changes"] == [{"scene": "v1-s03", "op": "remove", "from": {
        "id": "m03", "provider": "nasa", "asset_id": "A3", "credit": "NASA/3", "search_term": "term 3"}}]
    assert "v1-s03" not in record["returned"]
    # A later revision does not bring a removed scene back.
    later = task_revisions.create(task_id, settings={"font_size": 72})
    assert "v1-s03" not in [s["id"] for s in later["scenes"]] and later["excluded"] == ["v1-s03"]


def test_the_last_scene_cannot_be_removed(storage):
    task_id = _make_task(storage)
    every = [SceneOp(REMOVE, scene) for scene in ("v1-s01", "v1-s02", "v1-s03", "v1-s04")]
    with pytest.raises(SceneEditError, match="the last scene cannot be removed"):
        task_revisions.create(task_id, scene_ops=every)
    with pytest.raises(SceneEditError, match="both removed and replaced"):
        task_revisions.create(task_id, scene_ops=[SceneOp(REMOVE, "v1-s01"),
                                                  SceneOp(REPLACE, "v1-s01", candidate="nasa:C1")])


# ---------------------------------------------------------------- crop


def test_crop_position_is_part_of_the_prepared_image_name_and_keeps_the_resolution(storage):
    wide = _two_colour_image(storage / "cache_videos" / "nasa-wide.jpg")
    aspect = VideoAspect.portrait
    centre = nasa.prepare_image(str(wide), aspect, 3)
    assert centre and nasa.prepare_image(str(wide), aspect, 3, crop_position=(0.5, 0.5)) == centre
    left = nasa.prepare_image(str(wide), aspect, 3, crop_position=(0.0, 0.5))
    right = nasa.prepare_image(str(wide), aspect, 3, crop_position=(1.0, 0.5))
    assert left.endswith("-x000y050.jpg") and right.endswith("-x100y050.jpg")
    assert len({centre, left, right}) == 3
    with Image.open(left) as image_left, Image.open(right) as image_right:
        assert image_left.size == image_right.size  # the window keeps its size
        assert image_left.getpixel((image_left.width // 2, 10))[0] > 200  # red half
        assert image_right.getpixel((image_right.width // 2, 10))[2] > 200  # blue half
    tiny = _two_colour_image(storage / "cache_videos" / "nasa-tiny.jpg", size=(90, 60))
    assert nasa.prepare_image(str(tiny), aspect, 3, crop_position=(0.2, 0.5)) == ""
    assert nasa.crop_position_suffix(None) == "" and nasa.crop_position_suffix((0.3, 0.5)) == "-x030y050"


def test_the_clip_cache_key_follows_the_crop_position():
    params = VideoParams(video_subject="x", video_aspect="9:16", video_clip_duration=3)
    material_record = {"files": {"prepared": {"sha256": "p"}, "original": {"sha256": "o"}}}
    scene = timing.Scene(id="s", material="m", kind=timing.IMAGE)
    keys = [task_revisions._clip_key(material_record, scene, 90, crop, params)
            for crop in (None, (0.2, 0.5), (0.8, 0.5), (0.2, 0.5))]
    assert len(set(keys[:3])) == 3 and keys[1] == keys[3]
    assert task_revisions._clip_key(material_record, scene, 91, None, params) != keys[0]


def test_the_crop_cache_name_is_kept_by_the_cache_cleaner():
    from app.services import cache_manager

    names = ["nasa-0123456789abcdef0123456789abcdef-portrait-1188x2112.jpg",
             "nasa-0123456789abcdef0123456789abcdef-portrait-1188x2112-x030y050.jpg"]
    for name in names:
        assert any(pattern.match(name) for pattern in cache_manager._GENERATED_CACHE_FILE_PATTERNS), name
        assert any(pattern.match(f"{name}.part") for pattern in cache_manager._GENERATED_CACHE_TEMP_FILE_PATTERNS)


def test_cropping_moves_the_window_and_renders_only_that_scene(storage, voice_seconds, no_downloads, audits):
    task_id = _make_task(storage)
    _use_original(storage, task_id, "m01", _two_colour_image(storage / "cache_videos" / "nasa-wide-a.jpg"))
    voice_seconds["seconds"] = None
    second = task_revisions.create(task_id, settings={"font_size": 70})
    assert sorted(second["clips"]["rendered"]) == ["v1-s01", "v1-s02", "v1-s03", "v1-s04"]
    left = task_revisions.create(task_id, scene_ops=[SceneOp(CROP, "v1-s01", position=(0.0, 0.5))])
    # Only the cropped scene is rendered; the voiceover is reused, nothing is downloaded.
    assert left["clips"] == {"rendered": ["v1-s01"], "reused": ["v1-s02", "v1-s03", "v1-s04"]}
    assert left["audio"]["reused_from"] == "r002" and left["network"] == {"tts": False, "audit": False, "downloads": 0}
    assert left["scenes"][0]["crop"] == [0.0, 0.5] and left["scenes"][0]["status"] == "cropped"
    assert left["scene_changes"] == [{"scene": "v1-s01", "op": "crop", "from": [0.5, 0.5], "to": [0.0, 0.5]}]
    assert list((storage / "cache_videos").glob("nasa-wide-a-*-x000y050.jpg"))
    final = storage / "tasks" / task_id / "revisions" / "r003" / "final.mp4"
    red, green, blue = _frame_colour(final, 1.0)
    assert red > 150 and blue < 100
    right = task_revisions.create(task_id, scene_ops=[SceneOp(CROP, "v1-s01", position=(1.0, 0.5))])
    assert right["clips"]["rendered"] == ["v1-s01"]
    red, green, blue = _frame_colour(storage / "tasks" / task_id / "revisions" / "r004" / "final.mp4", 1.0)
    assert blue > 150 and red < 100
    # The crop is kept by the next revision and its clip comes from the cache.
    kept = task_revisions.create(task_id, settings={"font_size": 74})
    assert kept["scenes"][0]["crop"] == [1.0, 0.5] and kept["clips"]["rendered"] == []


def test_only_image_scenes_with_enough_resolution_can_be_cropped(storage):
    task_id = _make_task(storage)
    with pytest.raises(SceneEditError, match="only image scenes can be cropped"):
        task_revisions.create(task_id, scene_ops=[SceneOp(CROP, "v1-s02", position=(0.3, 0.5))])
    _use_original(storage, task_id, "m03", _two_colour_image(storage / "cache_videos" / "nasa-tiny.jpg", (90, 60)))
    with pytest.raises(SceneEditError, match="below the output resolution"):
        task_revisions.create(task_id, scene_ops=[SceneOp(CROP, "v1-s03", position=(0.3, 0.5))])
    with pytest.raises(SceneEditError):
        scene_edits.parse_position("1.5")
    assert scene_edits.parse_position("0.3") == (0.3, 0.5) and scene_edits.parse_position("0.2,0.9") == (0.2, 0.9)


# ---------------------------------------------------------------- command line


def test_scene_and_speed_flags_reach_the_revision(storage, monkeypatch):
    task_id = _make_task(storage)
    seen = {}

    def create(task, *, script=None, settings=None, base=None, scene_ops=None, performance_settings=None):
        seen["ops"], seen["performance"] = scene_ops, performance_settings
        return {"revision": "r002", "check_required": False, "video": {"file": "final.mp4"}}

    monkeypatch.setattr(task_revisions, "create", create)
    picture = storage / "benim.jpg"
    Image.new("RGB", (10, 10)).save(picture)
    code = cli.run_cli(["revizyon", task_id, "--sahne-degistir", "v1-s03=nasa:C1", "--sahne-ara",
                        'v1-s01="earth rise"', "--sahne-dosya", f"v1-s02={picture}", "--sahne-kaldir", "v1-s04",
                        "--kirpma", "v1-s01=0.3", "--render-motoru=ffmpeg", "--klip-esezamanliligi=4",
                        "--is-parcacigi=2"])
    assert code == 0
    assert seen["ops"] == [SceneOp(REPLACE, "v1-s03", candidate="nasa:C1"),
                           SceneOp(REPLACE, "v1-s01", search="earth rise"),
                           SceneOp(REPLACE, "v1-s02", path=str(picture)), SceneOp(REMOVE, "v1-s04"),
                           SceneOp(CROP, "v1-s01", position=(0.3, 0.5))]
    assert seen["performance"] == {"final_render_engine": "ffmpeg", "video_clip_concurrency": 4, "n_threads": 2}
    # The WebUI starts the command with the same flags.
    arguments = scene_edits.to_arguments(seen["ops"]) + task_revisions.performance_arguments(seen["performance"])
    args = task_revisions.parser().parse_args([task_id, *arguments])
    assert scene_edits.from_arguments(args) == seen["ops"]
    assert task_revisions.build_performance(args) == seen["performance"]


def test_cli_refuses_a_broken_scene_rule_as_invalid_input(storage):
    task_id = _make_task(storage)
    assert cli.run_cli(["revizyon", task_id, "--sahne-degistir=v1-s02=pixabay:R2"]) == 2
    assert cli.run_cli(["revizyon", task_id, "--kirpma=v1-s01=2"]) == 2
    assert cli.run_cli(["revizyon", task_id, "--sahne-kaldir=v1-s01", "--sahne-kaldir=v1-s02",
                        "--sahne-kaldir=v1-s03", "--sahne-kaldir=v1-s04"]) == 2
    assert task_revisions.names(task_id) == ["r001"]


# ---------------------------------------------------------------- WebUI


@pytest.fixture
def panel(storage, monkeypatch):
    started, saves = [], []
    monkeypatch.setattr(task_revisions, "start_background",
                        lambda task, arguments: started.append(arguments) or 1)
    monkeypatch.setattr(config, "save_config", lambda *a, **k: saves.append("save"))
    monkeypatch.setattr(config, "try_save_config", lambda *a, **k: saves.append("try") or True)
    return {"started": started, "saves": saves, "config": _config_file_hash()}


def _pending(app):
    return [m.value for m in app.markdown if m.value.startswith("- v1-s")]


def test_panel_scene_cards_remove_and_undo(storage, panel):
    task_id = _make_task(storage)
    app = _revision_page(task_id).run()
    assert not app.exception
    for scene in ("v1-s01", "v1-s02", "v1-s03", "v1-s04"):
        assert app.button(key=f"revision_replace_btn_{scene}")
        assert app.button(key=f"revision_remove_btn_{scene}")
    crops = sorted(b.key for b in app.button if b.key and b.key.startswith("revision_crop_btn_"))
    assert crops == ["revision_crop_btn_v1-s01", "revision_crop_btn_v1-s03"]  # image scenes only
    app.button(key="revision_remove_btn_v1-s02").click().run()
    assert _pending(app) == ["- v1-s02: remove"]
    assert not app.button(key="revision_generate").disabled
    undo = next(b for b in app.button if b.key and b.key.startswith("revision_undo_"))
    undo.click().run()
    assert _pending(app) == [] and app.button(key="revision_generate").disabled
    for scene in ("v1-s01", "v1-s02", "v1-s03"):
        app.button(key=f"revision_remove_btn_{scene}").click().run()
    assert app.button(key="revision_remove_btn_v1-s04").disabled  # the last scene stays
    app.button(key="revision_generate").click().run()
    assert panel["started"] == [["--temel-revizyon=r001", "--sahne-kaldir=v1-s01", "--sahne-kaldir=v1-s02",
                                 "--sahne-kaldir=v1-s03"]]
    assert panel["saves"] == [] and _config_file_hash() == panel["config"]


def test_panel_replace_dialog_shows_candidates_and_adds_the_choice(storage, panel):
    task_id = _make_task(storage)
    app = _revision_page(task_id).run()
    app.button(key="revision_replace_btn_v1-s02").click().run()
    assert not app.exception
    key = f"revision_replace_{task_id}_v1-s02"
    choose = {name: app.button(key=f"{key}_choose_{name}") for name in ("pixabay:A2", "pixabay:R2", "pixabay:C2")}
    assert choose["pixabay:R2"].disabled and choose["pixabay:A2"].disabled and not choose["pixabay:C2"].disabled
    assert len([c for c in app.caption if c.value.startswith(":orange[")]) == 2  # the two reasons
    choose["pixabay:C2"].click().run()
    app.button(key=f"{key}_add").click().run()
    assert _pending(app) == ["- v1-s02: replace with pixabay:C2"]
    app.button(key="revision_generate").click().run()
    assert panel["started"] == [["--temel-revizyon=r001", "--sahne-degistir=v1-s02=pixabay:C2"]]
    assert panel["saves"] == [] and _config_file_hash() == panel["config"]


def test_panel_crop_dialog_moves_the_frame_with_the_slider(storage, panel, monkeypatch):
    from webui import revision_page

    task_id = _make_task(storage)
    _use_original(storage, task_id, "m01", _two_colour_image(storage / "cache_videos" / "nasa-wide-b.jpg"))
    framed = []
    original = revision_page.framed

    def spy(path, aspect, position=scene_edits.CENTER, max_side=480):
        framed.append(tuple(position))
        return original(path, aspect, position, max_side)

    monkeypatch.setattr(revision_page, "framed", spy)
    app = _revision_page(task_id).run()
    app.button(key="revision_crop_btn_v1-s01").click().run()
    assert not app.exception
    key = f"revision_crop_{task_id}_v1-s01"
    assert not app.slider(key=f"{key}_x").disabled and app.slider(key=f"{key}_y").disabled  # a wide source
    assert framed[-1] == (0.5, 0.5)
    app.slider(key=f"{key}_x").set_value(0.2).run()
    assert framed[-1] == (0.2, 0.5)
    app.button(key=f"{key}_apply").click().run()
    assert _pending(app) == ["- v1-s01: crop 0.20,0.50"]
    app.button(key="revision_generate").click().run()
    assert panel["started"] == [["--temel-revizyon=r001", "--kirpma=v1-s01=0.200,0.500"]]
    assert panel["saves"] == [] and _config_file_hash() == panel["config"]


def test_reasons_are_shown_in_the_ui_language(storage):
    from webui import revision_page

    task_id = _make_task(storage)
    base = task_revisions.ensure_first(task_id)
    manifest, materials = task_revisions._materials(task_id)
    manifest["terms"][1]["candidates"][1]["filters"] = [{"filter": "text", "result": "rejected", "detail": ""},
                                                        {"filter": "face", "result": "rejected", "detail": ""}]
    choices = {c["key"]: c for c in scene_edits.candidate_choices(manifest, materials, base["scenes"][1],
                                                                  base["scenes"])}
    assert (choices["pixabay:R2"]["reason_code"], choices["pixabay:R2"]["filters_rejected"]) == (
        "filtered", ["face", "text"])
    assert choices["pixabay:A2"]["reason_code"] == "current" and choices["pixabay:C2"]["reason_code"] is None

    def tr(key):
        return {"revision.reason.filtered": "elendi: {filters}", "revision.filter.text": "yazı",
                "revision.filter.face": "yüz", "revision.reason.other_scene": "{scene} kullanıyor"}.get(key, key)

    assert revision_page._reason(tr, choices["pixabay:R2"]) == "elendi: yüz, yazı"
    assert revision_page._reason(tr, choices["pixabay:A2"]) == "revision.reason.current"
    assert revision_page._reason(tr, {"reason_code": "other_scene", "used_by": "v1-s05"}) == "v1-s05 kullanıyor"
    assert revision_page._reason(tr, {"reason_code": "no_address"}) == "revision.reason.no_address"
    # Pending operations are described in the UI language too.
    assert revision_page._describe(lambda key: key + " {scene}", SceneOp(REMOVE, "v1-s02")) == \
        "revision.op.remove v1-s02"
    assert revision_page._describe(lambda key: "{scene} {x} {y}", SceneOp(CROP, "v1-s01", position=(0.2, 0.5))) == \
        "v1-s01 0.20 0.50"
    assert revision_page._describe(lambda key: key + " {words}", SceneOp(REPLACE, "v1-s03", search="earth rise")) == \
        "revision.op.replace_search earth rise"


def test_framed_preview_marks_the_crop_window(storage):
    from webui import revision_page

    wide = _two_colour_image(storage / "wide.png", size=(640, 320))
    left = revision_page.framed(str(wide), VideoAspect.portrait, (0.0, 0.5), max_side=640)
    right = revision_page.framed(str(wide), VideoAspect.portrait, (1.0, 0.5), max_side=640)
    # Inside the window the image is bright; outside it is dimmed.
    assert left.getpixel((40, 160))[0] > 200 and left.getpixel((600, 160))[2] < 150
    assert right.getpixel((600, 160))[2] > 200 and right.getpixel((40, 160))[0] < 150
    assert revision_page.crop_axes(str(wide), VideoAspect.portrait) == (True, False)


def test_panel_speed_settings_are_passed_only_when_changed(storage, panel):
    task_id = _make_task(storage)
    app = _revision_page(task_id).run()
    key = f"revision_performance_{task_id}"
    assert app.selectbox(key=f"{key}_engine").value == "ffmpeg"
    assert app.number_input(key=f"{key}_material").value == 3
    app.number_input(key=f"{key}_clips").set_value(2).run()
    app.button(key="revision_remove_btn_v1-s04").click().run()
    app.button(key="revision_generate").click().run()
    assert panel["started"] == [["--temel-revizyon=r001", "--sahne-kaldir=v1-s04", "--klip-esezamanliligi=2"]]
    assert panel["saves"] == [] and _config_file_hash() == panel["config"]
