"""ffmpeg 成片路径（final_render_engine = "ffmpeg"）与 MoviePy 路径的等价性：同一组短的合成输入
分别用两条路径渲染，比较逐帧 SSIM、帧数、音频包和音画起点；另测回退、不支持内容和设置。

默认运行一组覆盖每个维度的核心组合；完整的风格矩阵（位置 × 背景 × 描边 × 字体 × 三种比例）
较慢，设置 MPT_RUN_SLOW_TESTS=1 才运行。"""

import itertools
import os
import re
import subprocess
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest
from loguru import logger
from moviepy import CompositeVideoClip, ImageClip

from app.config import config
from app.models.schema import VideoAspect, VideoParams
from app.services import video

RUN_SLOW_TESTS = os.environ.get("MPT_RUN_SLOW_TESTS", "").lower() in {"1", "true", "yes"}
FF = video.utils.get_ffmpeg_binary()
SIZES = {VideoAspect.portrait: (180, 320), VideoAspect.landscape: (320, 180), VideoAspect.square: (240, 240)}
FONTS = ("STHeitiMedium.ttc", "MicrosoftYaHeiBold.ttc")
POSITIONS = ("bottom", "top", "center", "custom")
SSIM_THRESHOLD = 0.98


def _run(*args, cwd=None):
    return subprocess.run([FF, "-hide_banner", *args], capture_output=True, text=True, errors="replace", cwd=cwd)


@pytest.fixture(scope="module")
def inputs(tmp_path_factory):
    root = tmp_path_factory.mktemp("final-render")
    bases = {}
    for aspect, (width, height) in SIZES.items():
        base = root / f"base-{width}x{height}.mp4"
        _run("-y", "-f", "lavfi", "-i", f"testsrc2=s={width}x{height}:r=30", "-t", "2",
             "-vf", video._BT709_VIDEO_FILTER, "-c:v", "libx264", "-pix_fmt", "yuv420p", str(base))
        bases[aspect] = base
    voice, bgm, long_voice = root / "voice.wav", root / "bgm.wav", root / "long-voice.wav"
    _run("-y", "-f", "lavfi", "-i", "sine=f=440:d=1.9", "-ar", "44100", str(voice))
    # 比视频长：MoviePy 写出完整音频（成片里音频比视频长），ffmpeg 路径不能截掉音频的结尾。
    _run("-y", "-f", "lavfi", "-i", "sine=f=330:d=2.3", "-ar", "44100", str(long_voice))
    # 比成片短，随机/自定义背景音乐会被循环铺满，再淡出。
    _run("-y", "-f", "lavfi", "-i", "sine=f=220:d=0.7", "-ar", "44100", str(bgm))
    subtitles = root / "subtitles.srt"
    subtitles.write_text(
        "1\n00:00:00,000 --> 00:00:00,900\nHello world test\n\n"
        "2\n00:00:01,000 --> 00:00:01,950\nİkinci satır çok uzun bir altyazı metni, iki satıra bölünür\n\n",
        encoding="utf-8",
    )
    # 最后一条字幕比原视频长：原视频结束后 MoviePy 的画面是黑底加字幕。
    overrun = root / "overrun.srt"
    overrun.write_text("1\n00:00:01,500 --> 00:00:02,400\nAfter the end\n\n", encoding="utf-8")
    return {"root": root, "bases": bases, "voice": voice, "long_voice": long_voice, "bgm": bgm,
            "subtitles": subtitles, "overrun": overrun}


def _render(inputs, engine, name, aspect=VideoAspect.portrait, subtitles="subtitles", bgm=True, voice="voice",
            **overrides):
    options = dict(video_subject="test", video_aspect=aspect.value, font_size=18, stroke_width=1,
                   bgm_type="random" if bgm else "", bgm_volume=0.3)
    options.update(overrides)
    target = inputs["root"] / f"{name}-{engine}.mp4"
    with (
        patch.dict(config.app, {"final_render_engine": engine}),
        patch.object(VideoAspect, "to_resolution", lambda self: SIZES[VideoAspect(self)]),
        patch.object(video, "get_bgm_file", return_value=str(inputs["bgm"])),
    ):
        video.generate_video(str(inputs["bases"][aspect]), str(inputs[voice]), str(inputs[subtitles]),
                             str(target), VideoParams(**options))
    return target


def _frame_ssim(first, second, work_dir):
    stats = Path(work_dir) / "ssim.log"
    _run("-i", str(first), "-i", str(second), "-lavfi", f"[0:v][1:v]ssim=stats_file={stats.name}", "-f", "null", "-",
         cwd=work_dir)
    return [float(value) for value in re.findall(r"All:([\d.]+)", stats.read_text())]


def _packets(path):
    """每个流的时间基和数据包（dts、pts、时长、大小、md5）。"""
    text = _run("-i", str(path), "-map", "0", "-c", "copy", "-f", "framemd5", "-").stdout
    time_bases = dict(re.findall(r"^#tb (\d+): (\S+)", text, re.M))
    packets = {}
    for line in text.splitlines():
        if line and not line.startswith("#"):
            fields = [field.strip() for field in line.split(",")]
            packets.setdefault(fields[0], []).append(fields[1:])
    return time_bases, packets


def _start_seconds(time_base, packets):
    numerator, denominator = (int(part) for part in time_base.split("/"))
    return int(packets[0][1]) * numerator / denominator


def _pcm(path):
    raw = subprocess.run([FF, "-i", str(path), "-vn", "-f", "s16le", "-ac", "1", "-"], capture_output=True).stdout
    return np.frombuffer(raw, dtype=np.int16).astype(float)


def _assert_equivalent(inputs, name, **options):
    moviepy_output = _render(inputs, "moviepy", name, **options)
    with patch.object(video, "_run_ffmpeg_final_command", wraps=video._run_ffmpeg_final_command) as command:
        ffmpeg_output = _render(inputs, "ffmpeg", name, **options)
    assert command.called, "the ffmpeg path was not used"
    scores = _frame_ssim(moviepy_output, ffmpeg_output, inputs["root"])
    moviepy_bases, moviepy_packets = _packets(moviepy_output)
    ffmpeg_bases, ffmpeg_packets = _packets(ffmpeg_output)
    # 视频帧数相同（时长差不超过一帧，这里要求完全相同），逐帧 SSIM 不低于阈值。
    assert len(moviepy_packets["0"]) == len(ffmpeg_packets["0"]) == len(scores)
    assert min(scores) >= SSIM_THRESHOLD, (name, min(scores))
    # 音频流逐包相同（同一次 MoviePy 混音），音画起点相同。
    assert moviepy_packets.get("1") == ffmpeg_packets.get("1")
    for stream in moviepy_packets:
        assert _start_seconds(moviepy_bases[stream], moviepy_packets[stream]) == pytest.approx(
            _start_seconds(ffmpeg_bases[stream], ffmpeg_packets[stream]), abs=1e-6
        )
    return min(scores)


CORE_CASES = [
    ("bottom-plain-thin", {"subtitle_position": "bottom"}),
    ("top-box-stroke3", {"subtitle_position": "top", "text_background_color": "#000000", "stroke_width": 3}),
    ("center-yahei", {"subtitle_position": "center", "font_name": "MicrosoftYaHeiBold.ttc", "stroke_width": 0}),
    ("custom-rounded", {"subtitle_position": "custom", "custom_position": 30.0, "text_background_color": "#203080",
                        "rounded_subtitle_background": True, "text_fore_color": "#FFD000"}),
    ("two-thirds", {"subtitle_position": "two_thirds_bottom", "text_background_color": True}),
    ("no-subtitles", {"subtitle_enabled": False}),
    ("no-bgm", {"bgm": False}),
    ("landscape", {"aspect": VideoAspect.landscape, "text_background_color": "#000000"}),
    ("square", {"aspect": VideoAspect.square, "stroke_width": 3}),
    ("subtitle-after-video-end", {"subtitles": "overrun"}),
    ("voice-longer-than-video", {"voice": "long_voice"}),
    ("voice-longer-no-bgm", {"voice": "long_voice", "bgm": False}),
]


@pytest.mark.parametrize("name, options", CORE_CASES, ids=[case[0] for case in CORE_CASES])
def test_ffmpeg_render_matches_moviepy(inputs, name, options):
    _assert_equivalent(inputs, name, **options)


FULL_MATRIX = list(itertools.product(SIZES, POSITIONS, (False, True), (0, 3), FONTS))


@pytest.mark.skipif(not RUN_SLOW_TESTS, reason="full style matrix: set MPT_RUN_SLOW_TESTS=1")
@pytest.mark.parametrize(
    "aspect, position, box, stroke, font", FULL_MATRIX,
    ids=[f"{a.name}-{p}-{'box' if b else 'nobox'}-s{s}-{f.split('.')[0]}" for a, p, b, s, f in FULL_MATRIX],
)
def test_full_style_matrix(inputs, aspect, position, box, stroke, font):
    options = dict(aspect=aspect, subtitle_position=position, stroke_width=stroke, font_name=font,
                   text_background_color="#000000" if box else False)
    if position == "custom":
        options["custom_position"] = 70.0
    _assert_equivalent(inputs, f"m-{aspect.name}-{position}-{box}-{stroke}-{font[:5]}", **options)


# ---------------------------------------------------------------- 叠加层钩子

def test_layers_composed_after_the_subtitles_are_rendered_too(inputs, tmp_path):
    """署名这类在字幕之后再叠一层的合成（嵌套 CompositeVideoClip）同样走 ffmpeg 路径。"""
    base_path = inputs["bases"][VideoAspect.portrait]
    badge = np.zeros((40, 120, 4), dtype=np.uint8)
    badge[..., :3], badge[..., 3] = (250, 250, 250), 150
    outputs = {}
    for engine in ("moviepy", "ffmpeg"):
        with video.VideoFileClip(str(base_path)) as base:
            text = ImageClip(np.full((30, 100, 3), 200, dtype=np.uint8)).with_start(0.2).with_end(1.4).with_position(("center", 40))
            inner = CompositeVideoClip([base, text])
            credit = ImageClip(badge).with_start(1.0).with_duration(1.0).with_position(("center", 250))
            final = CompositeVideoClip([inner, credit])
            target = tmp_path / f"nested-{engine}.mp4"
            with patch.dict(config.app, {"final_render_engine": engine}):
                rendered = video._render_final_video_with_ffmpeg(
                    final, base, str(base_path), str(target), VideoParams(video_subject="x"), 44100, str(tmp_path)
                )
            if engine == "moviepy":
                assert rendered is False
                video._write_videofile_with_codec_fallback(final, str(target), codec="libx264", logger=None, fps=30)
            else:
                assert rendered is True
            outputs[engine] = target
    assert min(_frame_ssim(outputs["moviepy"], outputs["ffmpeg"], tmp_path)) >= SSIM_THRESHOLD


def test_identical_overlays_are_written_once(inputs, tmp_path):
    subtitles = tmp_path / "repeat.srt"
    subtitles.write_text("1\n00:00:00,000 --> 00:00:00,500\nSame\n\n2\n00:00:01,000 --> 00:00:01,500\nSame\n\n",
                         encoding="utf-8")
    seen = []
    real = video._run_ffmpeg_final_command

    def run(command, cwd):
        seen.append((command, sorted(os.listdir(cwd))))
        return real(command, cwd)

    with patch.object(video, "_run_ffmpeg_final_command", side_effect=run):
        _render({**inputs, "repeat": subtitles}, "ffmpeg", "repeat", subtitles="repeat")
    command, files = seen[0]
    overlay_inputs = [command[i + 1] for i, value in enumerate(command) if value == "-i"][1:3]
    assert overlay_inputs == ["0000.png", "0000.png"]
    assert [name for name in files if name.endswith(".png")] == ["0000.png"]


# ---------------------------------------------------------------- 回退与不支持的内容

def _messages():
    messages = []
    handler = logger.add(lambda message: messages.append(str(message)), level="INFO")
    return messages, handler


def test_ffmpeg_failure_falls_back_to_moviepy_and_logs_it(inputs):
    messages, handler = _messages()
    try:
        with (
            patch.object(video, "_run_ffmpeg_final_command", side_effect=RuntimeError("simulated encoder crash")),
            patch.object(video, "_write_videofile_with_codec_fallback",
                         wraps=video._write_videofile_with_codec_fallback) as writer,
        ):
            output = _render(inputs, "ffmpeg", "fallback")
    finally:
        logger.remove(handler)
    assert writer.called and output.exists() and output.stat().st_size > 0
    assert any("ffmpeg final render failed, falling back to MoviePy" in m and "simulated encoder crash" in m
               for m in messages)
    assert not list(output.parent.glob(f".{output.name}.*"))


def test_animated_subtitles_use_moviepy_with_the_reason_logged(inputs):
    messages, handler = _messages()
    try:
        with patch.object(video, "_run_ffmpeg_final_command") as command:
            output = _render(inputs, "ffmpeg", "animated", subtitle_animation="pop_spring")
    finally:
        logger.remove(handler)
    command.assert_not_called()
    assert output.exists()
    assert any("final video render uses MoviePy: subtitle animation pop_spring" in m for m in messages)


def test_overlay_that_changes_over_time_is_not_supported(inputs, tmp_path):
    with video.VideoFileClip(str(inputs["bases"][VideoAspect.portrait])) as base:
        moving = ImageClip(np.full((20, 20, 3), 255, dtype=np.uint8)).with_duration(1).with_position(lambda t: (int(10 + 50 * t), 10))
        final = CompositeVideoClip([base, moving])
        with (
            patch.dict(config.app, {"final_render_engine": "ffmpeg"}),
            patch.object(video, "_run_ffmpeg_final_command") as command,
        ):
            assert video._render_final_video_with_ffmpeg(
                final, base, "base.mp4", str(tmp_path / "out.mp4"), VideoParams(video_subject="x"), 44100, str(tmp_path)
            ) is False
        command.assert_not_called()


def test_hardware_encoder_failure_retries_with_libx264(inputs):
    calls = []
    real = video._run_ffmpeg_final_command

    def run(command, cwd):
        codec = command[command.index("-c:v") + 1]
        calls.append(codec)
        if codec != "libx264":
            raise RuntimeError("encoder unavailable")
        return real(command, cwd)

    with (
        patch.object(video, "_get_effective_video_codec", return_value="h264_qsv"),
        patch.object(video, "_run_ffmpeg_final_command", side_effect=run),
        patch.object(video, "_disable_runtime_video_codec") as disable,
    ):
        _render(inputs, "ffmpeg", "qsv-fallback")
    assert calls == ["h264_qsv", "libx264"]
    disable.assert_called_once()


@pytest.mark.parametrize("value, expected", [(None, "moviepy"), ("", "moviepy"), ("FFmpeg", "ffmpeg"),
                                             ("ffmpeg", "ffmpeg"), ("gpu", "moviepy")])
def test_engine_setting(value, expected):
    with patch.dict(config.app, {"final_render_engine": value}):
        assert video._get_final_render_engine() == expected


def test_default_engine_is_moviepy(inputs):
    with (
        patch.dict(config.app, clear=False),
        patch.object(video, "_run_ffmpeg_final_command") as command,
    ):
        config.app.pop("final_render_engine", None)
        with (
            patch.object(VideoAspect, "to_resolution", lambda self: SIZES[VideoAspect(self)]),
            patch.object(video, "get_bgm_file", return_value=str(inputs["bgm"])),
        ):
            video.generate_video(str(inputs["bases"][VideoAspect.portrait]), str(inputs["voice"]),
                                 str(inputs["subtitles"]), str(inputs["root"] / "default.mp4"),
                                 VideoParams(video_subject="x", font_size=18))
    command.assert_not_called()


def test_cli_flag_overrides_the_setting_for_this_process_only():
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    import cli

    with patch.dict(config.app, {"final_render_engine": "moviepy"}), patch.object(config, "save_config") as save:
        cli.apply_render_overrides(cli.parse_args(["--video-subject", "x", "--final-render-engine", "ffmpeg"]))
        assert config.app["final_render_engine"] == "ffmpeg"
        cli.apply_render_overrides(cli.parse_args(["--video-subject", "x"]))
        assert config.app["final_render_engine"] == "ffmpeg"
    save.assert_not_called()
    with pytest.raises(SystemExit):
        cli.parse_args(["--video-subject", "x", "--final-render-engine", "gpu"])


def test_example_config_documents_the_setting():
    text = (Path(__file__).resolve().parents[2] / "config.example.toml").read_text(encoding="utf-8")
    assert '# final_render_engine = "moviepy"' in text
