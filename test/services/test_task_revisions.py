"""Task revisions: the revision folder, duration fitting, voice reuse, audit, CLI and package refresh."""

import hashlib
import json
import os
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest

import cli
from app.config import config
from app.models.schema import VideoAspect, VideoParams
from app.services import batch_review, material, nasa, task_materials, task_revisions, video
from app.services import revision_timing as timing
from app.utils import utils

FF = utils.get_ffmpeg_binary()
CLIP = 3.0
SIZE = (64, 112)


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _ffmpeg(*args):
    subprocess.run([FF, "-y", "-loglevel", "error", *args], check=True, capture_output=True)


def _video(path, seconds, color="gray"):
    _ffmpeg("-f", "lavfi", "-i", f"color=c={color}:s=64x64:r=30", "-t", str(seconds),
            "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path))
    return path


def _audio(path, seconds):
    _ffmpeg("-f", "lavfi", "-i", f"sine=frequency=330:duration={seconds}", "-c:a", "libmp3lame", str(path))
    return path


def _image(path, color):
    from PIL import Image

    Image.new("RGB", (360, 640), color).save(path)
    return path


# ---------------------------------------------------------------- duration rules (pure)


def _scene(name, kind, term, order, start=0.0, source=None):
    return timing.Scene(id=name, material=f"m-{name}", kind=kind, term_index=term, order=order,
                        start=start, source_duration=source)


SCENES = [
    _scene("s1", timing.IMAGE, 1, 1), _scene("s2", timing.VIDEO, 2, 2, start=1.0, source=7.0),
    _scene("s3", timing.IMAGE, 3, 3), _scene("s4", timing.VIDEO, 4, 4, source=2.5),
    _scene("s5", timing.IMAGE, 5, 5),
]


def test_shorter_voiceover_drops_scenes_by_even_spread_not_from_the_end():
    plan = timing.plan(SCENES, 7.0, CLIP)
    # ceil(7 / 3) = 3 scenes; even spread keeps the first, the middle and the last.
    assert [p.scene.id for p in plan.scenes] == ["s1", "s3", "s5"]
    assert [s.id for s in plan.dropped] == ["s2", "s4"]
    assert set(plan.dropped_reasons.values()) == {timing.EVEN_SPREAD}
    assert [p.duration for p in plan.scenes] == pytest.approx([7 / 3] * 3, abs=0.002)
    assert sum(p.duration for p in plan.scenes) == pytest.approx(7.0)


def test_even_spread_takes_more_scenes_when_a_short_video_cannot_cover():
    scenes = [_scene("a", timing.VIDEO, 1, 1, source=1.0), _scene("b", timing.IMAGE, 2, 2),
              _scene("c", timing.VIDEO, 3, 3, source=1.0)]
    plan = timing.plan(scenes, 4.0, CLIP)
    # Two scenes (first and last) hold only 2 s; all three are needed.
    assert [p.scene.id for p in plan.scenes] == ["a", "b", "c"]
    assert [p.duration for p in plan.scenes] == pytest.approx([1.0, 2.0, 1.0])


def test_longer_voiceover_spreads_evenly_within_clip_and_source_limits():
    plan = timing.plan(SCENES, 14.0, CLIP)
    durations = {p.scene.id: p.duration for p in plan.scenes}
    # s4's source has 2.5 s; the others share the rest equally (each at most 3 s).
    assert durations["s4"] == pytest.approx(2.5)
    assert [durations[name] for name in ("s1", "s2", "s3", "s5")] == pytest.approx([2.875] * 4, abs=0.002)
    assert not plan.dropped and not plan.added


def test_video_scene_stays_inside_its_source_from_its_start():
    scene = _scene("v", timing.VIDEO, 1, 1, start=5.5, source=7.0)
    assert scene.capacity(CLIP) == pytest.approx(1.5)
    assert timing.plan([scene, _scene("i", timing.IMAGE, 2, 2)], 4.0, CLIP).scenes[0].duration == pytest.approx(1.5)


def test_candidates_are_added_round_robin_after_their_terms_last_scene():
    calls = []

    def fetch(candidate):
        calls.append(candidate.key)
        return timing.Scene(id=f"add-{candidate.key}", material=f"m-{candidate.key}", kind=timing.IMAGE)

    candidates = [timing.Candidate("t1-a", 1), timing.Candidate("t1-b", 1), timing.Candidate("t3-a", 3)]
    plan = timing.plan(SCENES, 19.0, CLIP, candidates=candidates, fetch=fetch)
    # 14.5 s available; one candidate per term per round, in term order, until it is enough.
    assert calls == ["t1-a", "t3-a"]
    assert [p.scene.id for p in plan.scenes] == ["s1", "add-t1-a", "s2", "s3", "add-t3-a", "s4", "s5"]
    assert {p.scene.id: p.status for p in plan.scenes}["add-t1-a"] == "added"
    assert sum(p.duration for p in plan.scenes) == pytest.approx(19.0)
    assert max(p.duration for p in plan.scenes) <= CLIP + 1e-6


def test_a_candidate_that_cannot_be_used_is_skipped():
    def fetch(candidate):
        return None if candidate.key == "bad" else timing.Scene(id="ok", material="m", kind=timing.IMAGE)

    plan = timing.plan(SCENES, 16.0, CLIP, candidates=[timing.Candidate("bad", 1), timing.Candidate("good", 1)],
                       fetch=fetch)
    assert [c.key for c in plan.skipped_candidates] == ["bad"]
    assert [c.key for c, _scene in plan.added] == ["good"]


def test_dropped_scenes_return_before_candidates():
    reserve = [_scene("s9", timing.IMAGE, 9, 2.5)]
    plan = timing.plan(SCENES, 16.0, CLIP, reserve=reserve,
                       candidates=[timing.Candidate("never", 1)], fetch=lambda c: pytest.fail("not needed"))
    assert [p.scene.id for p in plan.scenes] == ["s1", "s2", "s9", "s3", "s4", "s5"]
    assert [s.id for s in plan.returned] == ["s9"]


def test_last_image_scene_is_extended_when_nothing_else_is_left():
    plan = timing.plan(SCENES, 20.0, CLIP)
    durations = {p.scene.id: p.duration for p in plan.scenes}
    assert plan.extended == "s5"
    assert durations["s5"] == pytest.approx(20.0 - 3 - 3 - 3 - 2.5)
    assert {p.scene.id: p.status for p in plan.scenes}["s5"] == "extended"


def test_without_material_or_an_image_scene_it_is_an_error():
    scenes = [_scene("v", timing.VIDEO, 1, 1, source=2.0)]
    with pytest.raises(timing.RevisionTimingError, match="no image scene"):
        timing.plan(scenes, 10.0, CLIP)


def test_a_video_scene_without_room_in_its_source_is_dropped():
    scenes = [_scene("v", timing.VIDEO, 1, 1, start=6.9, source=7.0), _scene("i", timing.IMAGE, 2, 2)]
    plan = timing.plan(scenes, 2.0, CLIP)
    assert plan.dropped_reasons == {"v": timing.NO_ROOM}


def test_clip_frames_add_up_to_the_voiceover():
    durations = [4.962] * 8 + [4.964]
    frames = task_revisions._frame_counts(durations, 30)
    # Rounding each clip down would lose ~0.16 s over nine clips; the running total keeps them.
    assert sum(frames) == round(sum(durations) * 30) == 1340
    assert set(frames) <= {148, 149}


def test_term_matching_warning_only_for_a_changed_text_in_script_order():
    assert timing.term_matching_warning(True, True)
    assert not timing.term_matching_warning(True, False)
    assert not timing.term_matching_warning(False, True)


# ---------------------------------------------------------------- a small finished task


@pytest.fixture
def storage(tmp_path, monkeypatch):
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
    (root / "cache_videos").mkdir(parents=True)
    monkeypatch.setattr(VideoAspect, "to_resolution", lambda self: SIZE)
    monkeypatch.setattr(video, "_get_configured_video_codec", lambda: "libx264")
    monkeypatch.setattr(video, "_get_effective_video_codec", lambda *args: "libx264")
    monkeypatch.setitem(config.app, "final_render_engine", "ffmpeg")
    monkeypatch.setattr(nasa, "text_filter_active", lambda: False)
    monkeypatch.setattr(nasa, "face_filter_enabled", lambda: False)
    return root


def _candidate(provider, asset, term, status, rank, url=None):
    entry = {"rank": rank, "provider": provider, "asset_id": asset, "source_page": f"https://example.org/{asset}",
             "duration": 5, "status": status, "filters": [], "preview": None}
    if url:
        entry["item"] = {"provider": provider, "url": url, "duration": 9,
                         "source_info": {"provider": provider, "asset_id": asset, "credit": f"credit {asset}"}}
    return entry


def _make_task(root, *, audio_seconds=11.0):
    """Four scenes (image, video, image, video) over four terms, clip duration 3 s, 11 s voiceover."""
    task_id = str(uuid4())
    task = root / "tasks" / task_id
    folder = task / "materials"
    folder.mkdir(parents=True)
    cache = root / "cache_videos"
    files = {
        "m01": {"prepared": _image(cache / "nasa-a-portrait.jpg", "navy"), "original": _image(cache / "nasa-a.jpg", "navy")},
        "m02": {"video": _video(cache / "vid-b.mp4", 7, "red")},
        "m03": {"prepared": _image(cache / "nasa-c-portrait.jpg", "olive"), "original": _image(cache / "nasa-c.jpg", "olive")},
        "m04": {"video": _video(cache / "vid-d.mp4", 2.5, "blue")},
    }
    materials = []
    for number, (material_id, roles) in enumerate(files.items(), start=1):
        provider = "nasa" if "prepared" in roles else "pixabay"
        entries = {}
        for role, path in roles.items():
            target = folder / f"{number:02d}-{provider}-{role}{path.suffix}"
            task_materials.store_file(path, target)
            entries[role] = {"path": target.name, "sha256": _sha(target), "size": target.stat().st_size,
                             "stored_as": "hardlink", "source": f"cache_videos/{path.name}"}
        materials.append({
            "id": material_id, "provider": provider, "asset_id": f"A{number}", "source_page": None,
            "credit": f"NASA/{number}" if provider == "nasa" else None, "creator": None,
            "search_term": f"term {number}", "term_index": number, "duration": 5, "relevance": None,
            "filters": [], "files": entries,
            "source": {"provider": provider, "local_file": next(iter(roles.values())).name, "duration": 5,
                       "asset_id": f"A{number}", **({"credit": f"NASA/{number}"} if provider == "nasa" else {})},
        })
    terms = [
        {"index": 1, "term": "term 1", "provider": "nasa", "candidates": [
            _candidate("nasa", "A1", 1, "selected", 1),
            _candidate("nasa", "C1", 1, "not_selected", 2, "https://images-assets.nasa.gov/image/C1/C1~orig.jpg")]},
        {"index": 2, "term": "term 2", "provider": "pixabay", "candidates": [
            _candidate("pixabay", "A2", 2, "selected", 1),
            _candidate("pixabay", "R2", 2, "rejected", 2, "https://cdn.pixabay.com/video/r2.mp4"),
            _candidate("pixabay", "C2", 2, "not_selected", 3, "https://cdn.pixabay.com/video/c2.mp4")]},
        {"index": 3, "term": "term 3", "provider": "nasa", "candidates": [_candidate("nasa", "A3", 3, "selected", 1)]},
        {"index": 4, "term": "term 4", "provider": "pixabay", "candidates": [_candidate("pixabay", "A4", 4, "selected", 1)]},
    ]
    scenes = [
        {"id": "v1-s01", "video": 1, "index": 1, "material": "m01", "start": 0.0, "duration": 3.0},
        {"id": "v1-s02", "video": 1, "index": 2, "material": "m02", "start": 1.0, "duration": 3.0},
        {"id": "v1-s03", "video": 1, "index": 3, "material": "m03", "start": 0.0, "duration": 3.0},
        {"id": "v1-s04", "video": 1, "index": 4, "material": "m04", "start": 0.0, "duration": 2.0},
    ]
    (folder / "materials.json").write_text(json.dumps({
        "schema_version": 1, "task_id": task_id, "status": "complete", "stage": "video", "terms": terms,
        "materials": materials, "scenes": scenes, "anchors": [],
    }), encoding="utf-8")
    params = VideoParams(video_subject="Deneme konusu", video_source="nasa", video_aspect="9:16",
                         video_clip_duration=int(CLIP), subtitle_enabled=False, bgm_type="",
                         voice_name="tr-TR-EmelNeural-Female", match_materials_to_script=True,
                         nasa_credit_overlay=False).model_dump(mode="json")
    (task / "script.json").write_text(json.dumps({
        "script": "bir iki üç dört beş altı yedi sekiz dokuz on", "search_terms": [t["term"] for t in terms],
        "params": params, "material_sources": [m["source"] for m in materials],
    }), encoding="utf-8")
    _audio(task / "audio.mp3", audio_seconds)
    _video(task / "final-1.mp4", audio_seconds, "black")
    return task_id


@pytest.fixture
def voice_seconds(monkeypatch):
    """Fake TTS: the voiceover lasts as many seconds as the test sets; calls are recorded."""
    state = {"seconds": 11.0, "calls": []}

    def synthesize(params, script, audio_file):
        state["calls"].append((params.voice_name, script))
        _audio(audio_file, state["seconds"])
        return object()

    monkeypatch.setattr(task_revisions, "_synthesize", synthesize)
    return state


@pytest.fixture
def no_downloads(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("no material may be downloaded")

    monkeypatch.setattr(material, "save_video", refuse)
    monkeypatch.setattr(nasa, "_prepared_image", refuse)


@pytest.fixture
def audits(monkeypatch):
    calls = []

    def audit(subject, script):
        calls.append(script)
        return {"durum": "tamam", "iddialar": [{"claim": "x", "rating": "Y", "reason": "r"}],
                "sayim": {"D": 0, "B": 0, "Y": 1, "X": 0}, "isaretli": [{"claim": "x", "rating": "Y", "reason": "r"}]}

    monkeypatch.setattr(task_revisions, "_audit", audit)
    return calls


def test_the_task_output_is_recorded_as_r001_without_moving_it(storage):
    task_id = _make_task(storage)
    task = storage / "tasks" / task_id
    before = {name: _sha(task / name) for name in ("final-1.mp4", "audio.mp3", "script.json")}
    record = task_revisions.ensure_first(task_id)
    assert sorted(p.name for p in (task / "revisions" / "r001").iterdir()) == ["revision.json"]
    assert record["video"]["file"] == "../../final-1.mp4"
    assert record["video"]["sha256"] == before["final-1.mp4"]
    assert [s["id"] for s in record["scenes"]] == ["v1-s01", "v1-s02", "v1-s03", "v1-s04"]
    assert [s["kind"] for s in record["scenes"]] == ["image", "video", "image", "video"]
    assert record["scenes"][1]["source_duration"] == pytest.approx(7.0, abs=0.05)
    assert task_revisions.verify(task_id, "r001") == []
    assert {name: _sha(task / name) for name in before} == before
    assert task_revisions.ensure_first(task_id) == record


def test_subtitle_only_change_reuses_the_voiceover_and_downloads_nothing(storage, voice_seconds, no_downloads, audits):
    task_id = _make_task(storage)
    voice_seconds["seconds"] = None  # TTS must not run
    record = task_revisions.create(task_id, settings={"text_fore_color": "#FFFF00", "font_size": 72})
    folder = storage / "tasks" / task_id / "revisions" / "r002"
    assert record["revision"] == "r002" and record["parent"] == "r001"
    assert set(record["changes"]) == {"text_fore_color", "font_size"}
    assert record["network"] == {"tts": False, "audit": False, "downloads": 0}
    assert voice_seconds["calls"] == [] and audits == []
    assert record["audio"]["reused_from"] == "r001"
    assert record["audio"]["sha256"] == _sha(storage / "tasks" / task_id / "audio.mp3")
    # credits.txt: the revision's own NASA credits, from the scenes it shows.
    assert sorted(p.name for p in folder.iterdir()) == [
        "audio.mp3", "credits.txt", "final.mp4", "revision.json", "script.txt"]
    assert [s["status"] for s in record["scenes"]] == ["kept"] * 4
    assert record["params"]["text_fore_color"] == "#FFFF00"
    assert record["video"]["duration"] == pytest.approx(11.0, abs=0.15)
    assert task_revisions.verify(task_id, "r002") == []


def test_music_is_cut_to_the_video_and_a_random_choice_is_kept(storage, voice_seconds, no_downloads, audits,
                                                                  monkeypatch):
    task_id = _make_task(storage)
    songs = storage / "songs"
    songs.mkdir()
    long_song = _audio(songs / "output019.mp3", 30)
    calls = []

    def get_bgm_file(bgm_type="random", bgm_file=""):
        calls.append((bgm_type, bgm_file))
        return str(long_song)

    monkeypatch.setattr(video, "get_bgm_file", get_bgm_file)
    record = task_revisions.create(task_id, settings={"bgm_type": "random", "bgm_volume": 0.3})
    # A 30 s song under an 11 s video: looped and cut by generate_video, not stretching the video.
    assert record["video"]["duration"] == pytest.approx(11.0, abs=0.15)
    assert record["bgm"] == {"type": "random", "file": "output019.mp3", "volume": 0.3}
    assert ("custom", str(long_song)) in calls  # the chosen file went in as a custom file
    calls.clear()
    third = task_revisions.create(task_id, settings={"font_size": 70})
    # The music did not change: the random choice of r002 is kept.
    assert calls[0] == ("custom", "output019.mp3") and third["bgm"]["file"] == "output019.mp3"


def test_a_voice_change_runs_tts_but_downloads_nothing(storage, voice_seconds, no_downloads, audits):
    task_id = _make_task(storage)
    record = task_revisions.create(task_id, settings={"voice_name": "tr-TR-AhmetNeural-Male", "voice_rate": 1.1})
    assert voice_seconds["calls"] == [("tr-TR-AhmetNeural-Male", "bir iki üç dört beş altı yedi sekiz dokuz on")]
    assert record["network"] == {"tts": True, "audit": False, "downloads": 0}
    assert record["audit"] is None and not record["check_required"]


def test_a_shorter_text_drops_scenes_and_is_audited(storage, voice_seconds, no_downloads, audits):
    task_id = _make_task(storage)
    voice_seconds["seconds"] = 5.0
    record = task_revisions.create(task_id, script="bir iki üç dört")
    assert [s["id"] for s in record["scenes"]] == ["v1-s01", "v1-s04"]
    assert [d["id"] for d in record["dropped"]] == ["v1-s02", "v1-s03"]
    assert {d["reason"] for d in record["dropped"]} == {"even spread"}
    assert sum(s["duration"] for s in record["scenes"]) == pytest.approx(5.0, abs=0.01)
    assert record["changes"]["script"] == {"from_words": 10, "to_words": 4}
    assert audits == ["bir iki üç dört"]
    assert record["check_required"] and record["check_reasons"]
    assert record["warnings"]  # script order: terms may no longer match their sentences
    folder = storage / "tasks" / task_id / "revisions" / "r002"
    assert (folder / "script.txt").read_text(encoding="utf-8") == "bir iki üç dört\n"
    assert record["video"]["duration"] == pytest.approx(5.0, abs=0.15)


def test_a_longer_text_adds_candidates_of_the_same_terms(storage, voice_seconds, audits, monkeypatch):
    task_id = _make_task(storage)
    cache = storage / "cache_videos"
    downloaded = []

    def prepared_image(item, save_dir, aspect, clip_duration, motion):
        downloaded.append(item.url)
        return str(_image(cache / "nasa-c1.jpg", "teal")), str(_image(cache / "nasa-c1-portrait.jpg", "teal"))

    def save_video(video_url, save_dir=""):
        downloaded.append(video_url)
        return str(_video(cache / "vid-c2.mp4", 9, "purple"))

    monkeypatch.setattr(nasa, "_prepared_image", prepared_image)
    monkeypatch.setattr(material, "save_video", save_video)
    voice_seconds["seconds"] = 16.0
    record = task_revisions.create(task_id, script="bir iki üç dört beş altı yedi sekiz dokuz on on bir on iki on üç")
    # 11.5 s of scenes (s04's source is 2.5 s); C1 (term 1) and C2 (term 2, the rejected R2 skipped) follow.
    assert downloaded == ["https://images-assets.nasa.gov/image/C1/C1~orig.jpg", "https://cdn.pixabay.com/video/c2.mp4"]
    assert [s["id"] for s in record["scenes"]] == ["v1-s01", "r002-a01", "v1-s02", "r002-a02", "v1-s03", "v1-s04"]
    assert [a["candidate"] for a in record["added"]] == ["nasa:C1", "pixabay:C2"]
    assert record["network"]["downloads"] == 2
    assert sum(s["duration"] for s in record["scenes"]) == pytest.approx(16.0, abs=0.01)
    manifest = json.loads((storage / "tasks" / task_id / "materials" / "materials.json").read_text(encoding="utf-8"))
    added = [m for m in manifest["materials"] if m.get("added_by") == "r002"]
    assert [m["asset_id"] for m in added] == ["C1", "C2"]
    assert set(added[0]["files"]) == {"original", "prepared"} and set(added[1]["files"]) == {"video"}
    # The original task's records (credits) stay without the added materials.
    assert len(task_materials.material_sources(manifest)) == 4
    assert [s["asset_id"] for s in record["material_sources"]] == ["A1", "C1", "A2", "C2", "A3", "A4"]


def test_an_older_manifest_nasa_candidate_is_rebuilt_and_falls_back_to_large(storage, monkeypatch):
    task_id = _make_task(storage)
    cache = storage / "cache_videos"
    tried = []

    def prepared_image(item, save_dir, aspect, clip_duration, motion):
        tried.append(item.url)
        if "~orig." in item.url:
            return "", ""
        return str(_image(cache / "nasa-old.jpg", "teal")), str(_image(cache / "nasa-old-portrait.jpg", "teal"))

    monkeypatch.setattr(nasa, "_prepared_image", prepared_image)
    entry = _candidate("nasa", "OLD7", 1, "not_selected", 2)  # no "item": recorded before addresses were kept
    params = VideoParams(video_subject="x", video_aspect="9:16", video_clip_duration=3)
    fetcher = task_revisions._Fetcher(task_id, "r002", params, {"nasa:OLD7": (entry, {"index": 1, "term": "term 1"})})
    scene = fetcher(timing.Candidate("nasa:OLD7", 1))
    assert tried == ["https://images-assets.nasa.gov/image/OLD7/OLD7~orig.jpg",
                     "https://images-assets.nasa.gov/image/OLD7/OLD7~large.jpg"]
    assert scene.kind == timing.IMAGE and scene.id == "r002-a01"
    # A Pixabay candidate without an address cannot be rebuilt and is skipped with a reason.
    pixabay = _candidate("pixabay", "P9", 2, "not_selected", 2)
    fetcher.entries["pixabay:P9"] = (pixabay, {"index": 2, "term": "term 2"})
    assert fetcher(timing.Candidate("pixabay:P9", 2)) is None
    assert fetcher.skipped == [{"candidate": "pixabay:P9", "reason": "no download address recorded"}]


def test_the_last_image_scene_is_extended_when_no_candidate_is_left(storage, voice_seconds, audits, monkeypatch):
    task_id = _make_task(storage)
    monkeypatch.setattr(nasa, "_prepared_image", lambda *a: ("", ""))
    monkeypatch.setattr(material, "save_video", lambda video_url, save_dir="": "")
    voice_seconds["seconds"] = 14.0
    record = task_revisions.create(task_id, script="uzun bir metin")
    assert record["extended"] == "v1-s03"
    durations = {s["id"]: s["duration"] for s in record["scenes"]}
    assert durations["v1-s03"] == pytest.approx(14.0 - 3 - 3 - 2.5, abs=0.01)
    assert [s["reason"] for s in record["skipped_candidates"]] == [
        "image could not be downloaded or prepared", "download failed"]
    assert record["video"]["duration"] == pytest.approx(14.0, abs=0.15)


def test_revisions_are_never_rewritten(storage, voice_seconds, no_downloads, audits):
    task_id = _make_task(storage)
    second = task_revisions.create(task_id, settings={"text_fore_color": "#FFFF00"})
    folder = storage / "tasks" / task_id / "revisions"
    snapshot = {p.name: _sha(p) for p in (folder / "r002").iterdir()}
    third = task_revisions.create(task_id, settings={"font_size": 80})
    assert (second["revision"], third["revision"], third["parent"]) == ("r002", "r003", "r002")
    assert {p.name: _sha(p) for p in (folder / "r002").iterdir()} == snapshot
    with pytest.raises(task_revisions.RevisionError, match="already exists"):
        task_revisions._publish(folder / "r003", folder / "r002")
    with pytest.raises(task_revisions.RevisionError, match="nothing changed"):
        task_revisions.create(task_id, settings={"font_size": 80})
    (folder / "r002" / "final.mp4").write_bytes(b"changed")
    assert task_revisions.verify(task_id, "r002") == ["video"]
    assert not [p for p in folder.iterdir() if p.name.endswith(".tmp")]


def test_a_derived_revision_brings_dropped_scenes_back_first(storage, voice_seconds, no_downloads, audits):
    task_id = _make_task(storage)
    voice_seconds["seconds"] = 5.0
    task_revisions.create(task_id, script="kısa metin")
    voice_seconds["seconds"] = 11.0
    record = task_revisions.create(task_id, script="yeniden uzun bir metin")
    assert record["parent"] == "r002"
    assert sorted(record["returned"]) == ["v1-s02", "v1-s03"]
    assert [s["id"] for s in record["scenes"]] == ["v1-s01", "v1-s02", "v1-s03", "v1-s04"]


def test_a_failed_revision_leaves_no_folder(storage, voice_seconds, audits, monkeypatch):
    task_id = _make_task(storage)
    monkeypatch.setattr(video, "generate_video", lambda **kwargs: (_ for _ in ()).throw(RuntimeError("render")))
    with pytest.raises(RuntimeError):
        task_revisions.create(task_id, settings={"font_size": 90})
    assert task_revisions.names(task_id) == ["r001"]
    assert not [p for p in (storage / "tasks" / task_id / "revisions").iterdir() if p.name.startswith(".r0")]


# ---------------------------------------------------------------- command line


def test_cli_flags_become_revision_settings(storage, monkeypatch, capsys):
    task_id = _make_task(storage)
    seen = {}

    def create(task, *, script=None, settings=None, base=None, scene_ops=None, performance_settings=None):
        seen.update(task=task, script=script, settings=settings, base=base)
        seen["ops"], seen["performance"] = scene_ops, performance_settings
        return {"revision": "r002", "check_required": True,
                "video": {"file": "final.mp4"}}

    monkeypatch.setattr(task_revisions, "create", create)
    text = storage / "metin.txt"
    text.write_text("yeni metin", encoding="utf-8")
    code = cli.run_cli(["revizyon", task_id, f"--metin-dosyasi={text}", "--ses=tr-TR-AhmetNeural-Male",
                        "--ses-hizi=1.2", "--altyazi=acik", "--yazi-rengi=#FFFF00", "--kontur-kalinligi=2",
                        "--altyazi-konumu=top", "--muzik=dosya", "--muzik-dosyasi=output001.mp3",
                        "--muzik-duzeyi=0.3", "--nasa-atif=kapali", "--temel-revizyon=r001"])
    assert code == 0
    assert seen == {"task": task_id, "script": "yeni metin", "base": "r001", "settings": {
        "voice_name": "tr-TR-AhmetNeural-Male", "voice_rate": 1.2, "subtitle_enabled": True,
        "text_fore_color": "#FFFF00", "stroke_width": 2.0, "subtitle_position": "top", "bgm_file": "output001.mp3",
        "bgm_volume": 0.3, "bgm_type": "custom", "nasa_credit_overlay": False}, "ops": [], "performance": {}}
    output = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert output["revision"] == "r002" and output["check_required"]
    assert batch_review.busy(storage / "tasks" / task_id / "revisions") is None  # the lock is released


def test_settings_and_cli_flags_round_trip():
    settings = {"voice_name": "tr-TR-AhmetNeural-Male", "voice_rate": 0.9, "subtitle_enabled": False,
                "font_name": "a.ttf", "font_size": 50, "text_fore_color": "#00FF00", "stroke_color": "#111111",
                "stroke_width": 1.5, "subtitle_position": "custom", "custom_position": 40.0, "bgm_type": "",
                "bgm_volume": 0.1, "nasa_credit_overlay": True}
    args = task_revisions.parser().parse_args(["t", *task_revisions.settings_arguments(settings)])
    assert task_revisions.build_settings(args) == settings
    with pytest.raises(task_revisions.RevisionError):
        task_revisions.settings_arguments({"video_source": "pexels"})


def test_cli_reports_a_busy_task(storage, monkeypatch):
    task_id = _make_task(storage)
    folder = storage / "tasks" / task_id / "revisions"
    folder.mkdir()
    monkeypatch.setattr(batch_review, "acquire_busy",
                        lambda *a: (_ for _ in ()).throw(batch_review.ReviewBusy({"action": "revizyon", "pid": 1})))
    assert cli.run_cli(["revizyon", task_id, "--yazi-boyutu=70"]) == 3


def test_cli_rejects_an_unknown_task(storage):
    assert cli.run_cli(["revizyon", "no-such-task", "--yazi-boyutu=70"]) == 2


def test_the_old_cli_usage_is_unchanged(monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "parse_args", lambda argv: calls.append(argv) or (_ for _ in ()).throw(SystemExit(0)))
    with pytest.raises(SystemExit):
        cli.run_cli(["--video-subject", "Konu"])
    assert calls == [["--video-subject", "Konu"]]


# ---------------------------------------------------------------- batch package


def test_package_refresh_writes_next_to_the_original(storage, voice_seconds, no_downloads, audits, monkeypatch):
    from app.services import batch_package
    from app.services import batch_production as bp

    task_id = _make_task(storage)
    task_revisions.create(task_id, settings={"text_fore_color": "#FFFF00"})
    batch_dir = storage / "batches" / "b1"
    original = batch_dir / "01-deneme"
    original.mkdir(parents=True)
    (original / "kalite.json").write_text(json.dumps({"dogruluk": {"durum": "tamam", "sayim": {"D": 2}}}),
                                          encoding="utf-8")
    (original / "video.mp4").write_bytes(b"published")
    state = bp.BatchState(input="konular.csv", input_sha256="", preset="presets/uzay.toml", preset_sha256="",
                          created="2026-11-10T00:00:00", items=[
        bp.ItemState(index=1, subject="Deneme konusu", status=bp.DONE, task_id=task_id, folder="01-deneme")])
    monkeypatch.setattr(bp.BatchState, "load", classmethod(lambda cls, folder: state))
    seen = {}

    def build_package(item, result, preset, folder, log_lines, ask=None, review=None, revision=None):
        seen.update(result=result, revision=revision, folder=folder)
        (folder / "kalite.json").write_text("{}", encoding="utf-8")

    monkeypatch.setattr(batch_package, "build_package", build_package)
    target = task_revisions.refresh_package(batch_dir, 1)
    assert target == original / "revizyonlar" / "r002"
    assert (target / "kalite.json").is_file()
    assert (original / "video.mp4").read_bytes() == b"published"
    assert seen["result"]["videos"][0].endswith(os.path.join("r002", "final.mp4"))
    assert seen["revision"]["name"] == "r002"
    # The text did not change: the original package's audit is reused, no new LLM call.
    assert seen["revision"]["audit"] == {"durum": "tamam", "sayim": {"D": 2}}
    assert [s["asset_id"] for s in seen["revision"]["material_sources"]] == ["A1", "A2", "A3", "A4"]
    with pytest.raises(task_revisions.RevisionError):
        task_revisions.refresh_package(batch_dir, 1, "r001")


# ---------------------------------------------------------------- WebUI


def _revision_page(task_id, **params):
    from streamlit.testing.v1 import AppTest

    app = AppTest.from_file(str(Path(__file__).resolve().parents[2] / "webui" / "Main.py"), default_timeout=90)
    app.query_params["view"] = "revision"
    app.query_params["task"] = task_id
    for name, value in params.items():
        app.query_params[name] = value
    return app


def _config_file_hash():
    return _sha(Path(config.config_file))


def test_panel_starts_from_the_task_and_never_saves_settings(storage, monkeypatch):
    task_id = _make_task(storage)
    started = []
    monkeypatch.setattr(task_revisions, "start_background", lambda task, arguments: started.append((task, arguments)) or 1)
    saves = []
    monkeypatch.setattr(config, "save_config", lambda *a, **k: saves.append("save"))
    monkeypatch.setattr(config, "try_save_config", lambda *a, **k: saves.append("try") or True)
    before = _config_file_hash()
    app = _revision_page(task_id).run()
    assert not app.exception
    key = f"revision_{task_id}_r001"
    assert app.text_area(key=f"{key}_script").value == "bir iki üç dört beş altı yedi sekiz dokuz on"
    assert app.selectbox(key=f"{key}_voice").value == "tr-TR-EmelNeural-Female"
    assert app.selectbox(key=f"revision_base_{task_id}").options == ["r001"]
    assert app.button(key="revision_generate").disabled  # nothing changed yet
    # A card per scene (scene_edits); its title is the scene ID and term.
    assert sum(m.value.startswith("**v1-s0") for m in app.markdown) == 4
    app.color_picker(key=f"{key}_text_color").set_value("#FFFF00").run()
    app.number_input(key=f"{key}_font_size").set_value(72).run()
    assert not app.button(key="revision_generate").disabled
    app.button(key="revision_generate").click().run()
    assert not app.exception
    assert started == [(task_id, ["--temel-revizyon=r001", "--yazi-boyutu=72", "--yazi-rengi=#FFFF00"])] or \
        started == [(task_id, ["--temel-revizyon=r001", "--yazi-rengi=#FFFF00", "--yazi-boyutu=72"])]
    assert saves == [] and _config_file_hash() == before


def test_an_edited_text_is_handed_to_the_background_process(storage, monkeypatch):
    task_id = _make_task(storage)
    started = []
    monkeypatch.setattr(task_revisions, "start_background", lambda task, arguments: started.append(arguments) or 1)
    app = _revision_page(task_id).run()
    app.text_area(key=f"revision_{task_id}_r001_script").set_value("yepyeni bir metin").run()
    app.button(key="revision_generate").click().run()
    (arguments,) = started
    text_flag = next(a for a in arguments if a.startswith("--metin-dosyasi="))
    assert Path(text_flag.split("=", 1)[1]).read_text(encoding="utf-8") == "yepyeni bir metin"


def test_history_shows_revisions_and_refreshes_a_batch_package(storage, voice_seconds, no_downloads, audits, monkeypatch):
    task_id = _make_task(storage)
    task_revisions.create(task_id, settings={"text_fore_color": "#FFFF00"})
    refreshed = []
    monkeypatch.setattr(task_revisions, "start_package_refresh",
                        lambda batch, index, task, name: refreshed.append((batch, index, task, name)) or 1)
    app = _revision_page(task_id, batch="C:/batches/b1", item="2").run()
    assert not app.exception
    assert app.selectbox(key=f"revision_base_{task_id}").options == ["r001", "r002"]
    assert app.selectbox(key=f"revision_base_{task_id}").value == "r002"
    assert len(app.get("video")) == 2
    app.button(key="revision_package_r002").click().run()
    assert refreshed == [("C:/batches/b1", 2, task_id, "r002")]


def test_task_manager_opens_the_panel(storage, monkeypatch):
    from streamlit.testing.v1 import AppTest

    task_id = _make_task(storage)
    monkeypatch.setattr(config, "try_save_config", lambda *a, **k: True)
    app = AppTest.from_file(str(Path(__file__).resolve().parents[2] / "webui" / "Main.py"), default_timeout=90).run()
    buttons = [b for b in app.button if b.key and b.key.startswith("revise_task_") and b.key.endswith(task_id)]
    assert buttons and not buttons[0].disabled
    buttons[0].click().run()
    assert app.query_params["view"] == ["revision"] or app.query_params["view"] == "revision"
    assert task_id in str(app.query_params["task"])
