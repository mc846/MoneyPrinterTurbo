"""Kurgu kanalında anlatıma göre sahne süreleri (fiction.py): planlamada cümle sınırlarından ≤ 7,5 sn gruplar ve
uyarılar, altyazıdan anlatım aralıkları, sahne sınırları (duraklamanın ortası), en çok 1,2× yavaşlatma ve taşma,
eşit süreli karşılaştırma, klip hazırlama (gerçek ffmpeg), görevin klipleri ve kurgu görevinin revizyonu.
Ağ yok; TTS ve denetim sahte; config.toml'a yazılmaz."""

import json
from pathlib import Path
from uuid import uuid4

import pytest

from app.models.schema import VideoAspect, VideoParams
from app.services import fiction, task_materials, task_revisions
from test.services.test_fiction import make_clip
from test.services.test_task_revisions import _audio, _sha, _video, storage  # noqa: F401 -- fikstür

LONG = ("Güneş rüzgârı milyarlarca yıldır Ay yüzeyine helyum-3 bırakıyor; Apollo örnekleri bunu doğruladı, ama "
        "ortalama bir ton toprakta yalnızca birkaç miligram var.")


def _srt(path: Path, cues) -> Path:
    def stamp(seconds):
        ms = round(seconds * 1000)
        return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"

    path.write_text("\n".join(f"{i}\n{stamp(a)} --> {stamp(b)}\n{text}\n" for i, (a, b, text) in enumerate(cues, 1)),
                    encoding="utf-8")
    return path


# ---------------------------------------------------------------- planlama


def test_scenes_are_split_at_sentences_within_the_narration_limit():
    script = "Ya Ay'da maden olsaydı? Kısa bir cümle. Bir tane daha. " + LONG + " Peki buna değer miydi?"
    groups = fiction.narration_groups(script)
    assert all(group.endswith(("?", ".")) for group in groups)  # never inside a sentence
    assert LONG in groups  # the long sentence is a scene of its own
    for group in groups:
        if group != LONG:
            assert fiction.estimate_narration(group) <= fiction.MAX_NARRATION_SECONDS
    assert " ".join(groups) == script
    # The fewest scenes for the limit: three short sentences fit in one.
    assert fiction.narration_groups("Bir. İki. Üç.") == ["Bir. İki. Üç."]
    assert fiction.estimate_narration(LONG) == pytest.approx(0.066 * len(LONG.replace(" ", "")), abs=0.01)


def test_timing_warnings_name_the_long_scene():
    plan = {"clip_seconds": 8, "scenes": [{"index": 1, "narration": "Kısa."}, {"index": 2, "narration": LONG}]}
    warnings = fiction.timing_warnings(plan)
    assert len(warnings) == 1 and warnings[0].startswith("sahne 02: anlatım ~")
    assert "tek cümle" in warnings[0] and "8 sn × 1.2 = 9.6 sn" in warnings[0] and "bölebilirsiniz" in warnings[0]
    medium = " ".join(["kelime"] * 19) + "."  # 115 harf: ~7,6 sn, ekranda ~8,9 sn < 9,6
    assert 7.5 < fiction.estimate_narration(medium) < 8.3
    note = fiction.timing_warnings({"clip_seconds": 8, "scenes": [{"index": 1, "narration": medium}]})
    assert note and "yavaşlatılır" in note[0]


def test_a_new_plan_uses_the_narration_groups():
    from test.services.test_fiction import SCRIPT, plan_answer

    plan = fiction.build_plan("konu", SCRIPT, 40.0, fiction.Settings(), ask=plan_answer)
    assert plan["timing"] == fiction.TIMING_NARRATION and plan["version"] == 2
    assert [scene["narration"] for scene in plan["scenes"]] == fiction.narration_groups(SCRIPT)
    assert all(scene["estimated_seconds"] == fiction.estimate_narration(scene["narration"]) for scene in plan["scenes"])


# ---------------------------------------------------------------- render: aralıklar ve sınırlar

CUES = [(0.1, 3.0, "Bir iki üç dört."), (3.6, 6.0, "Beş altı yedi,"), (6.0, 8.4, "sekiz dokuz on."),
        (9.0, 11.5, "On bir on iki.")]
NARRATIONS = ["Bir iki üç dört.", "Beş altı yedi, sekiz dokuz on.", "On bir on iki."]


def test_narration_spans_follow_the_subtitle_words():
    spans = fiction.narration_spans(NARRATIONS, CUES)
    assert spans == [pytest.approx((0.1, 3.0)), pytest.approx((3.6, 8.4)), pytest.approx((9.0, 11.5))]
    # A boundary inside a cue: the cue's time is shared by its words (letters).
    inside = fiction.narration_spans(["Bir iki", "üç dört."], [(0.0, 2.0, "Bir iki üç dört.")])
    assert inside[0][1] == pytest.approx(inside[1][0]) and 0.5 < inside[0][1] < 1.5
    with pytest.raises(fiction.FictionError, match="eşleşmedi"):
        fiction.narration_spans(["tamamen başka bir metin burada"], CUES)
    with pytest.raises(fiction.FictionError, match="boş"):
        fiction.narration_spans(NARRATIONS, [])


def test_scene_boundaries_sit_in_the_pauses_and_long_scenes_are_slowed():
    timing = fiction.narration_timing(NARRATIONS, CUES, 12.0, [8.0, 8.0, 8.0])
    assert [s["start"] for s in timing["scenes"]] == [0.0, 3.3, 8.7]
    assert [s["duration"] for s in timing["scenes"]] == [3.3, 5.4, 3.3]
    assert [s["speed"] for s in timing["scenes"]] == [1.0, 1.0, 1.0]  # every clip is longer: cut
    assert timing["max_outside"] == 0 and timing["warnings"] == []
    slowed = fiction.narration_timing(NARRATIONS, CUES, 12.0, [4.0, 5.0, 4.0])
    assert slowed["scenes"][1]["speed"] == pytest.approx(1.08) and slowed["warnings"] == []


def test_a_scene_beyond_1_2x_hands_its_rest_to_the_next_one():
    timing = fiction.narration_timing(NARRATIONS, CUES, 12.0, [4.0, 4.0, 4.0])
    middle, last = timing["scenes"][1], timing["scenes"][2]
    assert middle["duration"] == pytest.approx(4.8) and middle["speed"] == pytest.approx(1.2)
    assert last["start"] == pytest.approx(8.1) and middle["outside"] == pytest.approx(0.3)
    assert timing["max_outside"] == pytest.approx(0.3)
    assert timing["warnings"] == ["sahne 02: ekranda 5.4 sn kalmalı, klip 4.0 sn × 1.2 = 4.8 sn; sahne 03 0.6 sn "
                                  "erken başlıyor (metni bölebilirsiniz)"]
    # The last scene too long: the one before keeps its rest.
    tail = fiction.narration_timing(NARRATIONS, CUES, 16.0, [8.0, 8.0, 4.0])
    assert tail["scenes"][2]["duration"] == pytest.approx(4.8) and tail["scenes"][1]["end"] == pytest.approx(11.2)
    assert tail["warnings"][0].startswith("sahne 03: klip 4.0 sn × 1.2 = 4.8 sn'ye sığmıyor; sahne 02")
    with pytest.raises(fiction.FictionError, match="sığmıyor"):
        fiction.narration_timing(NARRATIONS, CUES, 20.0, [3.0, 3.0, 3.0])


def test_equal_scenes_for_the_comparison():
    timing = fiction.equal_timing(NARRATIONS, CUES, 11.95)
    assert [s["start"] for s in timing["scenes"]] == [0.0, 4.0, 8.0]
    assert timing["scenes"][1]["outside"] == pytest.approx(0.4) and timing["max_outside"] == pytest.approx(0.4)


# ---------------------------------------------------------------- klip hazırlama ve görev


def test_a_scene_clip_is_cut_or_slowed_without_sound(tmp_path):
    clip = make_clip(tmp_path / "c.mp4", seconds=8, audio=True)
    cut = fiction.prepare_scene_clip(clip, 5.0, 1.0, tmp_path / "cut.mp4")
    info = fiction.probe(cut)
    assert info["duration"] == pytest.approx(5.0, abs=0.1) and not info["audio"]
    slow = fiction.prepare_scene_clip(clip, 9.2, 1.15, tmp_path / "slow.mp4")
    assert fiction.probe(slow)["duration"] == pytest.approx(9.2, abs=0.1)


def test_task_clips_follow_the_narration(tmp_path, monkeypatch):
    task = tmp_path / "task"
    task.mkdir()
    monkeypatch.setattr(fiction.utils, "task_dir", lambda task_id: str(task))
    from app.services import material

    persisted = {}
    monkeypatch.setattr(material, "_persist_material_sources", lambda task_id, records: persisted.update(r=records))
    clips = []
    for index in range(1, 4):
        path = make_clip(tmp_path / f"c{index}.mp4", seconds=4, color=("blue", "red", "green")[index - 1])
        clips.append({"index": index, "file": str(path), "sha256": fiction._sha256(path), "uploaded_at": "",
                      "prompt": f"p{index}", "narration": NARRATIONS[index - 1]})
    plan = tmp_path / "render-plani.json"
    plan.write_text(json.dumps({"tool": fiction.TOOL, "style_version": "abc", "clip_seconds": 4,
                                "timing": fiction.TIMING_NARRATION, "scenes": clips}), encoding="utf-8")
    _srt(task / "subtitle.srt", CUES)
    paths = fiction.task_clips("t1", plan, audio_duration=11.9)
    assert [Path(p).name for p in paths] == ["sahne-01-sahne.mp4", "sahne-02-sahne.mp4", "sahne-03-sahne.mp4"]
    timing = json.loads((task / "ai_clips" / fiction.TIMING_FILE).read_text(encoding="utf-8"))
    assert timing["mode"] == fiction.TIMING_NARRATION and timing["scenes"][1]["speed"] == pytest.approx(1.2)
    durations = [fiction.probe(p)["duration"] for p in paths]
    assert durations[0] == pytest.approx(3.3, abs=0.1) and durations[1] == pytest.approx(4.8, abs=0.1)
    # The last clip: its scene plus a little margin, never past the 4 s clip (it is not slowed).
    assert durations[2] == pytest.approx(min(timing["scenes"][2]["duration"] + 0.3, 4.0), abs=0.1)
    assert fiction.scene_clip_limit("t1", 11.9, 3) == pytest.approx(5.8)
    assert persisted["r"][1]["sha256"] == clips[1]["sha256"]  # the uploaded clip's identity
    # No subtitles: equal scenes as before.
    (task / "subtitle.srt").unlink()
    paths = fiction.task_clips("t1", plan, audio_duration=11.9)
    assert Path(paths[0]).name == "sahne-01.mp4"
    assert fiction.scene_clip_limit("t1", 11.9, 3) == pytest.approx(fiction.scene_seconds(11.9, 3))


# ---------------------------------------------------------------- kurgu görevinin revizyonu


def _fiction_task(root, cues, audio_seconds=12.0, clip_seconds=4.0):
    task_id = str(uuid4())
    task = root / "tasks" / task_id
    folder = task / "materials"
    folder.mkdir(parents=True)
    materials, scenes = [], []
    for number, narration in enumerate(NARRATIONS, start=1):
        target = folder / f"{number:02d}-ai_manual-video.mp4"
        _video(target, clip_seconds, ("navy", "red", "olive")[number - 1])
        materials.append({
            "id": f"m{number:02d}", "provider": "ai_manual", "asset_id": f"sahne-{number:02d}", "source_page": None,
            "credit": fiction.TOOL, "creator": None, "search_term": narration, "term_index": None,
            "duration": clip_seconds, "relevance": None, "filters": [],
            "files": {"video": {"path": target.name, "sha256": _sha(target), "size": target.stat().st_size,
                                "stored_as": "copy", "source": target.name}},
            "source": {"provider": "ai_manual", "asset_id": f"sahne-{number:02d}", "local_file": target.name,
                       "search_term": narration, "prompt": f"p{number}", "scene_label": fiction.SCENE_LABEL},
        })
        scenes.append({"id": f"v1-s{number:02d}", "video": 1, "index": number, "material": f"m{number:02d}",
                       "start": 0.0, "duration": audio_seconds / 3})
    (folder / "materials.json").write_text(json.dumps({
        "schema_version": 1, "task_id": task_id, "status": "complete", "stage": "video", "terms": [],
        "materials": materials, "scenes": scenes, "anchors": []}), encoding="utf-8")
    params = VideoParams(video_subject="Ya olsaydı", video_source="ai_manual", video_aspect="9:16",
                         video_clip_duration=int(clip_seconds), subtitle_enabled=True, bgm_type="",
                         voice_name="tr-TR-EmelNeural-Female").model_dump(mode="json")
    (task / "script.json").write_text(json.dumps({
        "script": " ".join(NARRATIONS), "search_terms": [], "params": params,
        "material_sources": [m["source"] for m in materials]}), encoding="utf-8")
    _audio(task / "audio.mp3", audio_seconds)
    _srt(task / "subtitle.srt", cues)
    _video(task / "final-1.mp4", audio_seconds, "black")
    return task_id


def test_a_revision_aligns_an_equal_fiction_video_to_its_narration(storage, monkeypatch):  # noqa: F811
    monkeypatch.setattr(VideoAspect, "to_resolution", lambda self: (108, 192))
    task_id = _fiction_task(storage, CUES)
    record = task_revisions.create(task_id)  # nothing else changes: the timing is the change
    assert record["changes"] == {"scene_timing": {"from": "equal", "to": "narration"}}
    assert record["scene_timing"] == "narration" and record["network"]["tts"] is False
    detail = record["scene_timing_detail"]
    assert [s["duration"] for s in detail["scenes"]] == pytest.approx([3.3, 4.8, 3.9])
    assert [round(s["duration"], 2) for s in record["scenes"]] == [3.3, 4.8, 3.9]
    assert any("sahne 02: ekranda 5.4 sn kalmalı" in warning for warning in record["warnings"])
    video = task_revisions.file_path(task_id, record, "video")
    assert fiction.probe(video)["duration"] == pytest.approx(12.0, abs=0.2)
    # Already aligned: the next revision needs a real change.
    with pytest.raises(task_revisions.RevisionError, match="nothing changed"):
        task_revisions.create(task_id)
    assert task_materials.manifest_path(task_id).is_file()
