"""Scene labels: scene times from the combined clips on whole frames, the label layers (corner,
credit avoidance, one at a time), both final render engines alike (SSIM), the setting, the
language, the records and revisions. Synthetic media, no network."""

import json
import re
import subprocess
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import numpy as np
import pytest
from moviepy import VideoFileClip

from app.config import config
from app.models.schema import MaterialInfo, VideoAspect, VideoConcatMode, VideoParams
from app.services import material, task, task_materials, task_revisions, video
from app.utils import utils

FF = video.utils.get_ffmpeg_binary()
LABEL = "NASA SVS · LRO verisinden görselleştirme"


def _clip(path, frames, color="red", size="64x64"):
    subprocess.run([FF, "-y", "-f", "lavfi", "-i", f"color=c={color}:s={size}:r=30", "-frames:v", str(frames),
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-g", "30", str(path)], check=True, capture_output=True)
    return str(path)


def _params(**kwargs):
    return VideoParams(video_subject="x", video_aspect="9:16", font_size=18, bgm_type="", **kwargs)


# ---------------------------------------------------------------- scene times


def test_frames_are_counted_and_the_timeline_is_cumulative(tmp_path):
    clips = [_clip(tmp_path / f"{n}.mp4", frames, color) for n, frames, color in
             (("a", 37, "red"), ("b", 52, "blue"), ("c", 45, "green"))]
    assert [video.video_frame_count(clip) for clip in clips] == [37, 52, 45]
    combined = tmp_path / "combined.mp4"
    video.concat_video_clips_with_ffmpeg(clip_files=clips, output_file=str(combined), threads=1,
                                         output_dir=str(tmp_path), max_duration=None)
    timeline = video.scene_timeline_from_clips([(clip, f"src-{i}") for i, clip in enumerate(clips)], str(combined))
    assert [(s["start_frame"], s["end_frame"]) for s in timeline] == [(0, 37), (37, 89), (89, 134)]
    assert timeline[1]["start"] == pytest.approx(37 / 30, abs=1e-4) and timeline[2]["source"] == "src-2"


def test_the_timeline_stops_where_the_combined_video_ends(tmp_path):
    clips = [_clip(tmp_path / f"{n}.mp4", 60, color) for n, color in (("a", "red"), ("b", "blue"), ("c", "green"))]
    combined = tmp_path / "combined.mp4"
    video.concat_video_clips_with_ffmpeg(clip_files=clips, output_file=str(combined), threads=1,
                                         output_dir=str(tmp_path), max_duration=2.5)
    total = video.video_frame_count(str(combined))
    timeline = video.scene_timeline_from_clips([(clip, clip) for clip in clips], str(combined))
    assert timeline[-1]["end_frame"] == total and len(timeline) == 2 and total < 120


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
    return root


def test_combine_videos_gives_the_scene_times_and_records_them(storage, tmp_path, monkeypatch):
    audio = tmp_path / "audio.wav"
    with wave.open(str(audio), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(8000)
        writer.writeframes(b"\0\0" * 8000 * 4)
    sources = [_clip(tmp_path / "a.mp4", 90, "red"), _clip(tmp_path / "b.mp4", 45, "blue")]
    monkeypatch.setattr(VideoAspect, "to_resolution", lambda self: (64, 64))
    monkeypatch.setattr(video, "_get_configured_video_codec", lambda: "libx264")
    monkeypatch.setattr(video, "_get_effective_video_codec", lambda *args: "libx264")
    task_id = str(uuid4())
    output = Path(utils.task_dir(task_id)) / "combined-1.mp4"
    timeline = []
    with task_materials.task_scope(task_id, _params(video_source="local")):
        video.combine_videos(str(output), sources, str(audio), video_concat_mode=VideoConcatMode.sequential,
                             max_clip_duration=2, threads=1, scene_timeline=timeline)
    total = video.video_frame_count(str(output))
    assert timeline[0]["start_frame"] == 0 and timeline[-1]["end_frame"] == total
    assert all(a["end_frame"] == b["start_frame"] for a, b in zip(timeline, timeline[1:]))
    assert [s["end_frame"] - s["start_frame"] for s in timeline[:2]] == [60, 45]  # 2 s of a, then all of b
    scenes = json.loads((task_materials.folder(task_id) / task_materials.MANIFEST).read_text("utf-8"))["scenes"]
    assert [(s["video_start"], s["video_end"]) for s in scenes] == [(s["start"], s["end"]) for s in timeline]
    assert all(scene["label"] is None for scene in scenes)


# ---------------------------------------------------------------- layers


def test_a_label_layer_covers_exactly_its_scene_frames():
    params = _params()
    labels = [{"start": 37 / 30, "end": 89 / 30, "text": LABEL}]
    font = video.file_security.resolve_path_within_directory(utils.font_dir(), "STHeitiMedium.ttc")
    [layer] = video.scene_label_clips(labels, font, 1080, 1920, params)
    assert video._layer_frame_range(layer, 30, 300) == (37, 88)


def _boxes(layout, sizes):
    return [(p["start"], p["end"], p["x"], p["y"], p["x"] + sizes[p["text"]][0], p["y"] + sizes[p["text"]][1])
            for p in layout]


def test_labels_merge_take_over_and_never_overlap_in_time():
    sizes = {"A": (200, 40), "B": (180, 40)}
    labels = [{"start": 0, "end": 2, "text": "A"}, {"start": 2, "end": 4, "text": "A"},
              {"start": 3.5, "end": 6, "text": "B"}, {"start": 7, "end": 8, "text": ""}]
    layout = video.scene_label_layout(labels, sizes, 1080, 1920, _params())
    assert [(p["text"], p["start"], p["end"]) for p in layout] == [("A", 0, 3.5), ("B", 3.5, 6)]
    assert all(a["end"] <= b["start"] for a, b in zip(layout, layout[1:]))


def test_the_corner_is_away_from_the_subtitles():
    sizes = {"A": (200, 40)}
    top = video.scene_label_layout([{"start": 0, "end": 1, "text": "A"}], sizes, 1080, 1920,
                                   _params(subtitle_position="bottom"))[0]
    bottom = video.scene_label_layout([{"start": 0, "end": 1, "text": "A"}], sizes, 1080, 1920,
                                      _params(subtitle_position="top"))[0]
    assert (top["x"], top["y"]) == (43, 77)  # top left, 4% margins
    assert bottom["y"] + 40 == round(1920 * 0.96)


@pytest.mark.parametrize("position", ["bottom", "top"])
def test_the_label_moves_next_to_the_credit_while_it_shows(position):
    params = _params(subtitle_position=position)
    sizes = {"A": (300, 40)}
    credit_y = round(1920 * 0.04) if position == "bottom" else round(1920 * 0.96) - 60
    layout = video.scene_label_layout([{"start": 0, "end": 10, "text": "A"}], sizes, 1080, 1920, params,
                                      credit_box=(8.0, 10.0, credit_y, 60))
    assert [(p["start"], p["end"]) for p in layout] == [(0, 8.0), (8.0, 10)]
    before, during = layout
    credit = (credit_y, credit_y + 60)
    label = (during["y"], during["y"] + 40)
    assert label[1] <= credit[0] or label[0] >= credit[1]  # beside the credit, not on it
    assert before["y"] != during["y"]


# ---------------------------------------------------------------- both engines


SIZE = (180, 320)


@pytest.fixture(scope="module")
def inputs(tmp_path_factory):
    root = tmp_path_factory.mktemp("labels")
    subprocess.run([FF, "-y", "-f", "lavfi", "-i", f"testsrc2=s={SIZE[0]}x{SIZE[1]}:r=30", "-t", "3",
                    "-vf", video._BT709_VIDEO_FILTER, "-c:v", "libx264", "-pix_fmt", "yuv420p", str(root / "base.mp4")],
                   check=True, capture_output=True)
    subprocess.run([FF, "-y", "-f", "lavfi", "-i", "sine=f=440:d=2.9", "-ar", "44100", str(root / "voice.wav")],
                   check=True, capture_output=True)
    (root / "subtitles.srt").write_text("1\n00:00:00,200 --> 00:00:02,800\nAy'ın uzak yüzü\n\n", encoding="utf-8")
    return root


def _render(root, engine, name, labels, enabled=True):
    target = root / f"{name}-{engine}.mp4"
    with (
        patch.dict(config.app, {"final_render_engine": engine, "scene_labels_enabled": enabled}),
        patch.object(VideoAspect, "to_resolution", lambda self: SIZE),
    ):
        video.generate_video(str(root / "base.mp4"), str(root / "voice.wav"), str(root / "subtitles.srt"),
                             str(target), _params(), credit_text="Görüntüler: NASA", scene_labels=labels)
    return target


def _frame(path, second):
    with VideoFileClip(str(path), audio=False) as clip:
        return clip.get_frame(second).astype(float)


def test_both_engines_draw_the_labels_alike(inputs):
    labels = [{"start": 0.5, "end": 1.5, "text": LABEL}, {"start": 1.5, "end": 2.9, "text": "Canlandırma · yapay zekâ"}]
    moviepy_output = _render(inputs, "moviepy", "labels", labels)
    ffmpeg_output = _render(inputs, "ffmpeg", "labels", labels)
    stats = inputs / "ssim.log"
    subprocess.run([FF, "-hide_banner", "-i", str(moviepy_output), "-i", str(ffmpeg_output), "-lavfi",
                    f"[0:v][1:v]ssim=stats_file={stats.name}", "-f", "null", "-"], cwd=inputs, capture_output=True)
    scores = [float(value) for value in re.findall(r"All:([\d.]+)", stats.read_text())]
    assert len(scores) == 90 and min(scores) >= 0.98
    plain = _render(inputs, "ffmpeg", "plain", [])
    corner = (slice(0, 40), slice(0, 120))  # top left
    assert np.abs(_frame(ffmpeg_output, 1.0)[corner] - _frame(plain, 1.0)[corner]).mean() > 2  # label shows
    assert np.abs(_frame(ffmpeg_output, 0.2)[corner] - _frame(plain, 0.2)[corner]).mean() < 1  # not before


def test_the_separator_dot_sits_in_a_narrow_slot():
    # STHeiti gives "·" a full-em advance (a wide hole after it); the label draws its own dot.
    font = str(Path(utils.font_dir()) / "STHeitiMedium.ttc")
    dotted = video._label_image(LABEL, font, 1080)
    spaced = video._label_image(LABEL.replace("·", " "), font, 1080)
    white = [int(((np.asarray(im)[..., 0] > 200) & (np.asarray(im)[..., 3] == 255)).sum()) for im in (dotted, spaced)]
    assert dotted.height == spaced.height
    assert 0 < dotted.width - spaced.width <= round(1080 * 0.024) * 0.4
    assert white[0] - white[1] >= 4


def test_switched_off_draws_nothing(inputs):
    off = _render(inputs, "ffmpeg", "off", [{"start": 0.5, "end": 2.5, "text": LABEL}], enabled=False)
    plain = _render(inputs, "ffmpeg", "plain2", [])
    assert np.abs(_frame(off, 1.0) - _frame(plain, 1.0)).mean() < 0.5


# ---------------------------------------------------------------- texts, records, revisions


def test_the_label_text_follows_the_video_language():
    label = {"tr": LABEL, "en": "NASA SVS · LRO data visualization"}
    assert video.scene_label_text(label, "tr-TR") == LABEL
    assert video.scene_label_text(label, "en-US") == "NASA SVS · LRO data visualization"
    assert video.scene_label_text(label, "") == "NASA SVS · LRO data visualization"
    assert video.scene_label_text("Canlandırma · yapay zekâ", "en") == "Canlandırma · yapay zekâ"
    assert video.scene_label_text(None) == ""


def test_the_source_record_keeps_the_label():
    item = MaterialInfo(provider="nasa_svs", url="https://svs.gsfc.nasa.gov/x.mp4", duration=5,
                        source_info={"provider": "nasa_svs", "asset_id": "5587",
                                     "scene_label": {"tr": LABEL, "en": "x" * 200, "de": "nein"}})
    with patch.object(material.task_materials, "downloaded"):
        record = material._material_source_record(item, "C:/x/svs-5587.mp4")
    assert record["scene_label"] == {"tr": LABEL, "en": "x" * 80}


def test_the_task_maps_scenes_to_their_material_labels():
    labelled = {"svs-5587.mp4": {"tr": LABEL, "en": "NASA SVS · LRO data visualization"}}
    timeline = [{"source": "C:/cache/nasa-1.mp4", "start": 0.0, "end": 2.0},
                {"source": "C:/cache/svs-5587.mp4", "start": 2.0, "end": 4.5}]
    assert task.scene_labels_for(timeline, labelled, "tr-TR") == [{"start": 2.0, "end": 4.5, "text": LABEL}]
    assert task.scene_labels_for(timeline, {}, "tr-TR") == []


def test_a_revision_keeps_the_labels_of_its_materials():
    materials = {"m01": {"source": {"provider": "nasa"}},
                 "m02": {"source": {"provider": "nasa_svs", "scene_label": {"tr": LABEL, "en": "data"}}}}
    scenes = [SimpleNamespace(material="m01"), SimpleNamespace(material="m02"), SimpleNamespace(material="m02")]
    timeline = [{"source": "m01", "start": 0.0, "end": 2.0}, {"source": "m02", "start": 2.0, "end": 4.0},
                {"source": "m02", "start": 4.0, "end": 5.0}]
    with patch.object(video, "scene_timeline_from_clips", return_value=timeline) as built:
        labels = task_revisions._revision_scene_labels(["a.mp4", "b.mp4", "c.mp4"], scenes, "combined.mp4",
                                                        materials, "tr-TR")
    assert built.call_args.args[0] == [("a.mp4", "m01"), ("b.mp4", "m02"), ("c.mp4", "m02")]
    assert labels == [{"start": 2.0, "end": 4.0, "text": LABEL}, {"start": 4.0, "end": 5.0, "text": LABEL}]
    with patch.dict(config.app, {"scene_labels_enabled": False}):
        assert task_revisions._revision_scene_labels(["a.mp4"], scenes[:1], "c.mp4", materials, "tr") == []
    plain = {"m01": {"source": {"provider": "nasa"}}}
    with patch.object(video, "scene_timeline_from_clips") as built:
        assert task_revisions._revision_scene_labels(["a.mp4"], scenes[:1], "c.mp4", plain, "tr") == []
    built.assert_not_called()  # no labelled material, no frame counting
