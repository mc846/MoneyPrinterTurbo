"""片段的 ffmpeg 归一化（只需缩放/裁剪时）与码流复制拼接：取帧规则、画面适配与 MoviePy 等价；
满足条件时复制拼接并精确截断，不满足或失败时回到原来的重新编码。"""

import re
import subprocess
import wave
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest
from loguru import logger
from moviepy import VideoFileClip

from app.models.schema import VideoAspect, VideoConcatMode, VideoFitMode, VideoTransitionMode
from app.services import video

FF = video.utils.get_ffmpeg_binary()
TARGET = (180, 320)


def _run(*args, cwd=None):
    return subprocess.run([FF, "-hide_banner", *args], capture_output=True, text=True, errors="replace", cwd=cwd)


def _source(path, size, rate, seconds=6, pattern="testsrc2"):
    source = f"{pattern}=s={size}:r={rate}" if pattern == "testsrc2" else (
        f"nullsrc=s={size}:r={rate},geq=lum='16+6*mod(N\\,36)':cb=128:cr=128"
    )
    _run("-y", "-f", "lavfi", "-i", source, "-t", str(seconds), "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path))
    return path


def _audio(path, seconds):
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(24000)
        writer.writeframes(b"\0\0" * int(24000 * seconds))
    return path


@pytest.fixture
def small_canvas(monkeypatch):
    monkeypatch.setattr(VideoAspect, "to_resolution", lambda self: TARGET)
    monkeypatch.setattr(video, "_get_configured_video_codec", lambda: "libx264")
    monkeypatch.setattr(video, "_get_effective_video_codec", lambda *args: "libx264")


def _combine(tmp_path, name, sources, seconds, force_moviepy=False, **options):
    target = tmp_path / f"{name}-{'moviepy' if force_moviepy else 'ffmpeg'}.mp4"
    with patch.object(video, "_normalize_clip_with_ffmpeg", wraps=video._normalize_clip_with_ffmpeg) as spy:
        if force_moviepy:
            spy.side_effect = RuntimeError("use MoviePy")
        video.combine_videos(str(target), [str(s) for s in sources], str(_audio(tmp_path / "audio.wav", seconds)),
                             max_clip_duration=options.pop("max_clip_duration", 2), threads=1, **options)
    return target, spy


def _frame_ssim(first, second, work_dir):
    stats = Path(work_dir) / "ssim.log"
    _run("-i", str(first), "-i", str(second), "-lavfi", f"[0:v][1:v]ssim=stats_file={stats.name}", "-f", "null", "-",
         cwd=work_dir)
    return [float(value) for value in re.findall(r"All:([\d.]+)", stats.read_text())]


def _centre_levels(path):
    raw = subprocess.run([FF, "-v", "error", "-i", str(path), "-vf", "crop=8:8,format=gray", "-f", "rawvideo", "-"],
                         capture_output=True).stdout
    return [int(block.mean()) for block in np.frombuffer(raw, dtype=np.uint8).reshape(-1, 8, 8)]


# ---------------------------------------------------------------- 取帧与画面适配

@pytest.mark.parametrize("rate", ["25", "30000/1001", "30", "60"])
@pytest.mark.parametrize("mode", [VideoConcatMode.sequential, VideoConcatMode.random])
def test_ffmpeg_picks_the_same_source_frames_as_moviepy(tmp_path, small_canvas, rate, mode):
    # 每帧亮度编码帧号；random 模式会用到从 2、4 秒开始的片段。
    source = _source(tmp_path / "numbered.mp4", "320x180", rate, pattern="numbered")
    with patch.object(video.random, "shuffle", lambda items: items.reverse()):
        moviepy_output, _ = _combine(tmp_path, "frames", [source], 3.4, True, video_concat_mode=mode)
        ffmpeg_output, spy = _combine(tmp_path, "frames", [source], 3.4, video_concat_mode=mode)
    assert spy.called
    expected, actual = _centre_levels(moviepy_output), _centre_levels(ffmpeg_output)
    assert len(expected) == len(actual)
    # 相邻帧亮度相差 6 级（全量程约 7）；编码误差远小于此。
    assert max(abs(a - b) for a, b in zip(expected, actual)) <= 3


FIT_CASES = [
    ("landscape-cover", "640x360", VideoFitMode.cover),
    ("landscape-contain", "640x360", VideoFitMode.contain),
    ("portrait-larger", "360x640", VideoFitMode.cover),
    ("square-contain", "400x400", VideoFitMode.contain),
    ("square-cover", "400x400", VideoFitMode.cover),
    ("odd-size-cover", "641x359", VideoFitMode.cover),
]


@pytest.mark.parametrize("name, size, fit", FIT_CASES, ids=[case[0] for case in FIT_CASES])
def test_ffmpeg_normalization_matches_the_moviepy_fit(tmp_path, small_canvas, name, size, fit):
    source = _source(tmp_path / f"{name}.mp4", size, 30)
    moviepy_output, _ = _combine(tmp_path, name, [source], 3.4, True, video_fit_mode=fit,
                                 video_concat_mode=VideoConcatMode.sequential)
    ffmpeg_output, spy = _combine(tmp_path, name, [source], 3.4, video_fit_mode=fit,
                                  video_concat_mode=VideoConcatMode.sequential)
    assert spy.called
    scores = _frame_ssim(moviepy_output, ffmpeg_output, tmp_path)
    assert min(scores) >= 0.98, (name, min(scores))


def test_cross_orientation_clips_are_cropped_even_in_contain_mode(tmp_path, small_canvas):
    source = _source(tmp_path / "landscape.mp4", "640x360", 30)
    _, spy = _combine(tmp_path, "cross", [source], 1.5, video_fit_mode=VideoFitMode.contain,
                      video_concat_mode=VideoConcatMode.sequential, crop_cross_orientation_clips=True)
    assert spy.call_args.args[2] == VideoFitMode.cover


@pytest.mark.parametrize("options", [
    {"video_transition_mode": VideoTransitionMode.fade_in},
    {"video_transition_mode": VideoTransitionMode.shuffle},
    {"clip_speed": 1.5},
])
def test_transitions_and_speed_changes_keep_the_moviepy_path(tmp_path, small_canvas, options):
    source = _source(tmp_path / "landscape.mp4", "640x360", 30)
    output, spy = _combine(tmp_path, "moviepy-only", [source], 1.5, video_concat_mode=VideoConcatMode.sequential,
                           **options)
    spy.assert_not_called()
    assert output.stat().st_size > 0


def test_clips_at_the_video_size_keep_the_moviepy_path(tmp_path, small_canvas):
    source = _source(tmp_path / "exact.mp4", "180x320", 30)
    _, spy = _combine(tmp_path, "exact", [source], 1.5, video_concat_mode=VideoConcatMode.sequential)
    spy.assert_not_called()


def test_failed_ffmpeg_normalization_falls_back_to_moviepy(tmp_path, small_canvas):
    source = _source(tmp_path / "landscape.mp4", "640x360", 30)
    messages = []
    handler = logger.add(lambda message: messages.append(str(message)), level="WARNING")
    real_run = subprocess.run

    def failing(command, *args, **kwargs):
        if "-bf" in command and "-vf" in command and "temp-clip-" in str(command[-1]):
            raise OSError("simulated ffmpeg crash")
        return real_run(command, *args, **kwargs)

    try:
        with patch.object(video.subprocess, "run", side_effect=failing):
            output, _ = _combine(tmp_path, "fallback", [source], 1.5, video_concat_mode=VideoConcatMode.sequential)
    finally:
        logger.remove(handler)
    assert output.stat().st_size > 0
    assert any("ffmpeg clip normalization failed, using MoviePy" in m for m in messages)
    assert not list(tmp_path.glob("temp-clip-*"))


# ---------------------------------------------------------------- 码流复制拼接

def _encode(path, size="180x320", rate=30, seconds=1.0, extra=("-bf", "0")):
    _run("-y", "-f", "lavfi", "-i", f"testsrc2=s={size}:r={rate}", "-t", str(seconds), "-vf", video._BT709_VIDEO_FILTER,
         "-c:v", "libx264", "-preset", "medium", *extra, "-pix_fmt", "yuv420p", str(path))
    return str(path)


def _video_packets(path):
    text = _run("-i", str(path), "-map", "0:v", "-c", "copy", "-f", "framemd5", "-").stdout
    return [line.split(",") for line in text.splitlines() if line and not line.startswith("#")]


def test_uniform_clips_are_stream_copied_and_cut_exactly(tmp_path):
    clips = [_encode(tmp_path / f"{index}.mp4") for index in range(3)]
    output = tmp_path / "copied.mp4"
    assert video.concat_video_clips_with_ffmpeg([*clips, clips[0]], str(output), None, str(tmp_path), 2.345) == "copy"
    packets = _video_packets(output)
    pts = [int(fields[2]) for fields in packets]
    # 时长 2.345 秒：保留 t < 2.345 的 71 帧，显示顺序与解码顺序一致，完整可解码。
    assert len(packets) == 71 and pts == sorted(pts)
    assert _run("-v", "error", "-i", str(output), "-f", "null", "-").stderr.strip() == ""
    reencoded = tmp_path / "reencoded.mp4"
    with patch.object(video, "_clips_allow_stream_copy", return_value=False):
        video.concat_video_clips_with_ffmpeg([*clips, clips[0]], str(reencoded), None, str(tmp_path), 2.345)
    assert len(_video_packets(reencoded)) == len(packets)
    assert not list(tmp_path.glob(".ffmpeg-concat-*")) and not list(tmp_path.glob("ffmpeg-concat-*"))


@pytest.mark.parametrize("name, other", [
    ("size", {"size": "176x320"}),
    ("frame-rate", {"rate": 25}),
    ("b-frames", {"extra": ()}),
    ("rate-control", {"extra": ("-bf", "0", "-crf", "30")}),
])
def test_clips_that_differ_are_re_encoded(tmp_path, name, other):
    clips = [_encode(tmp_path / "a.mp4"), _encode(tmp_path / "b.mp4", **other)]
    with patch.object(video, "_get_effective_video_codec", return_value="libx264"):
        assert video.concat_video_clips_with_ffmpeg(clips, str(tmp_path / "out.mp4"), None, str(tmp_path), 1.5) == "libx264"


def test_empty_or_unreadable_clips_prevent_copying(tmp_path):
    good = _encode(tmp_path / "good.mp4")
    empty = tmp_path / "empty.mp4"
    empty.write_bytes(b"\0" * 261)
    assert video._clips_allow_stream_copy([good, good]) is True
    assert video._clips_allow_stream_copy([good, str(empty)]) is False
    assert video._clips_allow_stream_copy([good, str(tmp_path / "missing.mp4")]) is False


def test_failed_stream_copy_falls_back_to_re_encoding(tmp_path):
    clips = [_encode(tmp_path / "a.mp4"), _encode(tmp_path / "b.mp4")]
    real = video._run_concat_with_heartbeat

    def run(command, output_file):
        if "copy" in command:
            return subprocess.CompletedProcess(command, 1, "", "simulated copy failure")
        return real(command, output_file)

    with (
        patch.object(video, "_run_concat_with_heartbeat", side_effect=run),
        patch.object(video, "_get_effective_video_codec", return_value="libx264"),
    ):
        assert video.concat_video_clips_with_ffmpeg(clips, str(tmp_path / "out.mp4"), None, str(tmp_path), 1.5) == "libx264"
    assert (tmp_path / "out.mp4").stat().st_size > 0


def test_combined_video_from_mixed_paths_is_stream_copied(tmp_path, small_canvas):
    """MoviePy（原尺寸片段）和 ffmpeg（需要缩放的片段）写出的临时片段 SPS/PPS 相同，可以直接复制拼接。"""
    exact = _source(tmp_path / "exact.mp4", "180x320", 30, seconds=2)
    wide = _source(tmp_path / "wide.mp4", "640x360", 25, seconds=2)
    messages = []
    handler = logger.add(lambda message: messages.append(str(message)), level="INFO")
    try:
        output, spy = _combine(tmp_path, "mixed", [exact, wide], 3.5, video_concat_mode=VideoConcatMode.sequential)
    finally:
        logger.remove(handler)
    assert spy.call_count == 1
    assert any("concatenated 2 clips by stream copy" in m for m in messages)
    with VideoFileClip(str(output), audio=False) as clip:
        assert list(clip.size) == list(TARGET)
        assert clip.duration == pytest.approx(3.5, abs=0.05)
