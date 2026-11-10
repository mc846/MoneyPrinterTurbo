"""blur 画面适配：完整画面居中，四周用同一画面的模糊背景填满（不出现黑边）。

覆盖 ffmpeg 归一化、MoviePy 适配、两条路径的等价性、与画布同比例的素材不受影响、
crop_materials_to_aspect 下的另一方向素材、图片片段，以及 CLI / 预设 / 模型 / WebUI 的取值。
素材全部由 ffmpeg lavfi 和 Pillow 现场生成，不访问网络。
"""

import subprocess
import wave
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest
from moviepy import VideoFileClip
from PIL import Image, ImageDraw

import cli
from app.config import config
from app.models.schema import VideoConcatMode, VideoFitMode, VideoParams
from app.services import batch_production, task_revisions, video
from app.services import revision_timing as timing

FF = video.utils.get_ffmpeg_binary()
PORTRAIT = (1080, 1920)
# 1280x720 的源缩放到 1080 宽：完整画面高 607 像素，居中放在 y = 656。
BAND_TOP, BAND_BOTTOM = 656, 656 + 607
# 源画面左边 40 像素宽的白条，缩放后约 34 像素。
BAR_COLUMNS = slice(3, 28)


def _run(*args, cwd=None):
    return subprocess.run([FF, "-hide_banner", *args], capture_output=True, text=True, errors="replace", cwd=cwd)


def _bar_source(path, size="1280x720", seconds=2):
    """灰色底、深色网格（局部方差大）、左边缘一条白条的测试素材，带一点暖色调。"""
    width, height = (int(value) for value in size.split("x"))
    graph = (
        f"color=c=0xb09070:s={size}:r=30,drawgrid=w=16:h=16:t=4:c=0x303030,"
        f"drawbox=x=0:y=0:w=40:h={height}:c=white:t=fill"
    )
    _run("-y", "-f", "lavfi", "-i", graph, "-t", str(seconds), "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path))
    assert path.stat().st_size > 0, (width, height)
    return path


def _audio(path, seconds):
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(24000)
        writer.writeframes(b"\0\0" * int(24000 * seconds))
    return path


def _frame(path, index=15, size=PORTRAIT):
    """解码第 index 帧为 RGB 数组（高 × 宽 × 3）。"""
    raw = subprocess.run(
        [FF, "-v", "error", "-i", str(path), "-vf", f"select=eq(n\\,{index})", "-frames:v", "1",
         "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        capture_output=True,
    ).stdout
    width, height = size
    assert len(raw) == width * height * 3, f"unexpected frame size: {len(raw)} bytes"
    return np.frombuffer(raw, dtype=np.uint8).reshape(height, width, 3)


def _luma(frame):
    return frame.astype(float) @ np.array([0.2126, 0.7152, 0.0722])


def _local_std(region, block=16):
    """16×16 块内亮度标准差的平均值：网格这样的细节很大，模糊后的背景很小。"""
    luma = _luma(region)
    height, width = (dimension - dimension % block for dimension in luma.shape)
    blocks = luma[:height, :width].reshape(height // block, block, width // block, block)
    return float(blocks.std(axis=(1, 3)).mean())


def _normalize(tmp_path, source, fit, name, size=(1280, 720), seconds=1.0):
    item = video.SubClippedVideoClip(
        file_path=str(source), start_time=0, end_time=seconds, width=size[0], height=size[1]
    )
    output = tmp_path / f"{name}.mp4"
    video._normalize_clip_with_ffmpeg(item, PORTRAIT, fit, str(output))
    return output


@pytest.fixture
def libx264(monkeypatch):
    monkeypatch.setattr(video, "_get_configured_video_codec", lambda: "libx264")
    monkeypatch.setattr(video, "_get_effective_video_codec", lambda *args: "libx264")


def _assert_blur_frame(frame, whole_frame=True):
    """完整画面（含左边缘白条）居中；上下的背景不是黑边，而是平滑的模糊画面。"""
    assert frame.shape == (PORTRAIT[1], PORTRAIT[0], 3)
    band = frame[BAND_TOP + 10:BAND_BOTTOM - 10]
    if whole_frame:
        assert _luma(band[:, BAR_COLUMNS]).mean() > 200
    for region in (frame[20:BAND_TOP - 20], frame[BAND_BOTTOM + 20:-20]):
        assert _luma(region).mean() > 60
        assert _local_std(region) < 3
    # 画面本身保持清晰：网格的局部方差远大于背景。
    assert _local_std(band[:, 100:-100]) > 20


# ---------------------------------------------------------------- ffmpeg 与 MoviePy 两条路径

def test_ffmpeg_blur_shows_the_whole_frame_over_a_blurred_background(tmp_path, libx264):
    source = _bar_source(tmp_path / "landscape.mp4")
    clips = {fit: _normalize(tmp_path, source, fit, fit.value) for fit in VideoFitMode}
    _assert_blur_frame(_frame(clips[VideoFitMode.blur]))
    # cover 把左右两边裁掉：白条不在画面里，上下是清晰的网格。
    covered = _frame(clips[VideoFitMode.cover])
    assert _luma(covered[BAND_TOP + 10:BAND_BOTTOM - 10, BAR_COLUMNS]).mean() < 200
    assert _local_std(covered[20:BAND_TOP - 20]) > 20
    # contain 的上下是黑边。
    contained = _frame(clips[VideoFitMode.contain])
    assert _luma(contained[20:BAND_TOP - 20]).max() < 20
    # 编码参数与其它临时片段相同，拼接时仍然直接复制码流。
    assert video._clips_allow_stream_copy([str(clip) for clip in clips.values()])


def test_moviepy_blur_shows_the_whole_frame_over_a_blurred_background(tmp_path):
    source = _bar_source(tmp_path / "landscape.mp4")
    with VideoFileClip(str(source), audio=False) as clip:
        fitted = video._fit_clip_to_canvas(
            clip, target_width=PORTRAIT[0], target_height=PORTRAIT[1], fit_mode=VideoFitMode.blur
        )
        assert tuple(fitted.size) == PORTRAIT
        frame = np.asarray(fitted.get_frame(0.5)).astype(np.uint8)
    _assert_blur_frame(frame)


def _combine(tmp_path, name, source, force_moviepy, **options):
    target = tmp_path / f"{name}-{'moviepy' if force_moviepy else 'ffmpeg'}.mp4"
    with patch.object(video, "_normalize_clip_with_ffmpeg", wraps=video._normalize_clip_with_ffmpeg) as spy:
        if force_moviepy:
            spy.side_effect = RuntimeError("use MoviePy")
        video.combine_videos(
            str(target), [str(source)], str(_audio(tmp_path / "audio.wav", 1.0)),
            max_clip_duration=2, threads=1, video_concat_mode=VideoConcatMode.sequential,
            video_fit_mode=VideoFitMode.blur, **options,
        )
    assert spy.called
    return target


REGIONS = {
    "frame": (slice(BAND_TOP + 40, BAND_BOTTOM - 40), slice(100, 980)),
    "top background": (slice(150, BAND_TOP - 150), slice(150, 930)),
    "bottom background": (slice(BAND_BOTTOM + 150, 1920 - 150), slice(150, 930)),
}


def test_ffmpeg_and_moviepy_blur_look_the_same(tmp_path, libx264):
    """
    combine_videos 的两条路径（ffmpeg 归一化与 MoviePy 适配）写出的同一帧：画面区域和上下
    背景区域的平均颜色每个通道相差不超过 2（0–255），整帧 SSIM 不低于 0.98。
    """
    source = _bar_source(tmp_path / "landscape.mp4")
    ffmpeg_output = _combine(tmp_path, "blur", source, False)
    moviepy_output = _combine(tmp_path, "blur", source, True)
    ffmpeg_frame, moviepy_frame = _frame(ffmpeg_output), _frame(moviepy_output)
    for name, (rows, columns) in REGIONS.items():
        expected = moviepy_frame[rows, columns].reshape(-1, 3).mean(axis=0)
        actual = ffmpeg_frame[rows, columns].reshape(-1, 3).mean(axis=0)
        assert np.abs(expected - actual).max() <= 2, (name, expected, actual)
    stats = tmp_path / "ssim.log"
    _run("-i", str(moviepy_output), "-i", str(ffmpeg_output), "-lavfi", f"ssim=stats_file={stats.name}",
         "-f", "null", "-", cwd=tmp_path)
    scores = [float(line.split("All:")[1].split()[0]) for line in stats.read_text().splitlines() if "All:" in line]
    assert scores and min(scores) >= 0.98, min(scores)


# ---------------------------------------------------------------- 与画布同比例的素材

def test_clip_with_the_canvas_aspect_ratio_is_fitted_like_cover_and_contain():
    for source_size in ((1080, 1920), (540, 960), (720, 1280)):
        filters = {fit: video._fit_filter(*source_size, *PORTRAIT, fit) for fit in VideoFitMode}
        assert filters[VideoFitMode.blur] == filters[VideoFitMode.cover] == filters[VideoFitMode.contain]
        assert "boxblur" not in filters[VideoFitMode.blur]
    assert "boxblur" in video._fit_filter(1280, 720, *PORTRAIT, VideoFitMode.blur)


def test_portrait_clip_is_normalized_with_the_same_ffmpeg_command(tmp_path, libx264):
    source = _bar_source(tmp_path / "portrait.mp4", size="540x960")
    commands = []
    real_run = video.subprocess.run

    def recording(command, *args, **kwargs):
        commands.append(command)
        return real_run(command, *args, **kwargs)

    with patch.object(video.subprocess, "run", side_effect=recording):
        for fit in (VideoFitMode.cover, VideoFitMode.blur):
            _normalize(tmp_path, source, fit, fit.value, size=(540, 960))
    cover, blur = (command[command.index("-vf") + 1] for command in commands if "-vf" in command)
    assert cover == blur and "split" not in blur


def test_portrait_clip_is_resized_without_a_background_in_moviepy(tmp_path):
    source = _bar_source(tmp_path / "portrait.mp4", size="540x960")
    with VideoFileClip(str(source), audio=False) as clip:
        frames = [
            np.asarray(
                video._fit_clip_to_canvas(clip, target_width=PORTRAIT[0], target_height=PORTRAIT[1], fit_mode=fit)
                .get_frame(0.5)
            )
            for fit in (VideoFitMode.cover, VideoFitMode.blur)
        ]
        assert video._fit_clip_to_canvas(clip, target_width=540, target_height=960, fit_mode=VideoFitMode.blur) is clip
    assert np.array_equal(*frames)


# ---------------------------------------------------------------- crop_materials_to_aspect

@pytest.mark.parametrize(("clip_size", "target_size", "fit_mode", "expected"), [
    # Blur when the crop would enlarge; a crop that still covers the canvas is used as is.
    ((1920, 1080), (1080, 1920), VideoFitMode.blur, VideoFitMode.blur),
    ((1280, 720), (1080, 1920), VideoFitMode.blur, VideoFitMode.blur),
    ((1080, 1920), (1920, 1080), VideoFitMode.blur, VideoFitMode.blur),
    ((3840, 2160), (1080, 1920), VideoFitMode.blur, VideoFitMode.cover),
    ((4000, 3000), (1080, 1920), VideoFitMode.blur, VideoFitMode.cover),
    ((2160, 3840), (1920, 1080), VideoFitMode.blur, VideoFitMode.cover),
    ((1440, 1920), (1080, 1920), VideoFitMode.blur, VideoFitMode.blur),
    ((3840, 2160), (1080, 1920), VideoFitMode.contain, VideoFitMode.cover),
    ((2160, 3840), (1920, 1080), VideoFitMode.contain, VideoFitMode.cover),
])
def test_blur_is_kept_for_cross_orientation_clips(clip_size, target_size, fit_mode, expected):
    assert video._clip_fit_mode(*clip_size, *target_size, fit_mode, True) == expected


def test_cross_orientation_clips_are_blurred_not_cropped_in_combine(tmp_path, libx264):
    source = _bar_source(tmp_path / "landscape.mp4", seconds=1)
    with patch.object(video, "_normalize_clip_with_ffmpeg", wraps=video._normalize_clip_with_ffmpeg) as spy:
        video.combine_videos(
            str(tmp_path / "combined.mp4"), [str(source)], str(_audio(tmp_path / "audio.wav", 0.5)),
            max_clip_duration=2, threads=1, video_concat_mode=VideoConcatMode.sequential,
            video_fit_mode=VideoFitMode.blur, crop_cross_orientation_clips=True,
        )
    assert spy.call_args.args[2] == VideoFitMode.blur


# ---------------------------------------------------------------- 图片片段

def _bar_image(path, size=(1280, 720)):
    image = Image.new("RGB", size, (176, 144, 112))
    draw = ImageDraw.Draw(image)
    for x in range(0, size[0], 16):
        draw.rectangle((x, 0, x + 3, size[1]), fill=(48, 48, 48))
    for y in range(0, size[1], 16):
        draw.rectangle((0, y, size[0], y + 3), fill=(48, 48, 48))
    draw.rectangle((0, 0, 39, size[1]), fill=(255, 255, 255))
    image.save(path)
    return path


@pytest.mark.parametrize("motion", ["zoom", "static"])
def test_image_rendered_with_blur_has_a_blurred_background(tmp_path, motion):
    image = _bar_image(tmp_path / "landscape.png")
    output = video.render_image_zoom_video(
        str(image), 1, image_motion=motion, output_size=PORTRAIT, fit_mode=VideoFitMode.blur
    )
    frame = _frame(output, index=0)
    _assert_blur_frame(frame)
    covered = video.render_image_zoom_video(
        str(image), 1, image_motion=motion, output_size=PORTRAIT, fit_mode=VideoFitMode.cover
    )
    assert covered != output
    assert _local_std(_frame(covered, index=0)[20:BAND_TOP - 20]) > 20
    # 放大模式的最后一帧（画面已放大，白条移出左边缘）仍然是平滑的模糊背景。
    _assert_blur_frame(_frame(output, index=29), whole_frame=motion == "static")


def test_image_with_the_canvas_aspect_ratio_reuses_the_cover_clip(tmp_path):
    image = _bar_image(tmp_path / "portrait.png", size=(540, 960))
    cover = video.render_image_zoom_video(str(image), 1, output_size=PORTRAIT, fit_mode=VideoFitMode.cover)
    with patch.object(video, "_target_size_image_clip", side_effect=AssertionError("rendered again")):
        assert video.render_image_zoom_video(str(image), 1, output_size=PORTRAIT, fit_mode=VideoFitMode.blur) == cover


def test_blur_image_clips_are_named_by_the_background_settings():
    identity = video._image_clip_settings_identity(5, video.ImageMotion.zoom, PORTRAIT, VideoFitMode.blur)
    assert identity != video._image_clip_settings_identity(5, video.ImageMotion.zoom, PORTRAIT, VideoFitMode.contain)
    with patch.object(video, "_BLUR_BACKGROUND_BRIGHTNESS", 1.0):
        assert video._image_clip_settings_identity(5, video.ImageMotion.zoom, PORTRAIT, VideoFitMode.blur) != identity
        # cover/contain 的命名与背景参数无关，已有片段继续复用。
        assert video._image_clip_settings_identity(
            5, video.ImageMotion.zoom, PORTRAIT, VideoFitMode.cover
        ) == video._image_clip_settings_identity(5, video.ImageMotion.zoom, PORTRAIT, "cover")


def test_revision_renders_image_scenes_with_the_task_fit_mode(tmp_path):
    params = VideoParams(video_subject="x", video_fit_mode="blur")
    scene = timing.Scene(id="v1-s01", material="m1", kind=timing.IMAGE)
    with (
        patch.object(task_revisions, "_image_source", return_value=tmp_path / "image.png"),
        patch.object(video, "render_image_zoom_video", return_value=str(tmp_path / "zoom.mp4")) as render,
        patch.object(video, "_normalize_clip_with_ffmpeg") as normalize,
    ):
        task_revisions._render_clip("task", {"files": {}}, scene, 2.0, params, tmp_path, 1)
    assert render.call_args.kwargs["fit_mode"] == VideoFitMode.blur
    assert render.call_args.kwargs["output_size"] == PORTRAIT
    # 片段已是成片尺寸，归一化只截取时长。
    assert normalize.call_args.args[2] == VideoFitMode.cover


# ---------------------------------------------------------------- 取值：模型、CLI、预设、WebUI

def test_schema_accepts_blur():
    assert VideoParams(video_subject="x", video_fit_mode="blur").video_fit_mode == VideoFitMode.blur


def test_cli_accepts_blur():
    with patch.dict(config.ui, {}, clear=True):
        params = cli.build_video_params(cli.parse_args(["--video-subject", "x", "--video-fit-mode", "blur"]))
        assert params.video_fit_mode == VideoFitMode.blur
        # WebUI 保存的 blur 也是命令行的默认值。
        config.ui["video_fit_mode"] = "blur"
        assert cli.build_video_params(cli.parse_args(["--video-subject", "x"])).video_fit_mode == VideoFitMode.blur
    with pytest.raises(SystemExit):
        cli.parse_args(["--video-subject", "x", "--video-fit-mode", "stretch"])


def test_batch_preset_passes_blur_to_the_cli(tmp_path):
    preset_file = tmp_path / "blur.toml"
    preset_file.write_text('[video]\nvideo_fit_mode = "blur"\n', encoding="utf-8")
    with patch.dict(config.app, {}), patch.dict(config.ui, {}, clear=True):
        preset = batch_production.load_preset(preset_file)
        argv = batch_production.video_option_argv(preset.video)
        assert argv == ["--video-fit-mode", "blur"]
        params = cli.build_video_params(cli.parse_args(["--video-subject", "x", *argv]))
    assert params.video_fit_mode == VideoFitMode.blur


def test_webui_offers_and_saves_blur():
    from streamlit.testing.v1 import AppTest

    from app.services import voice

    test_ui_config = dict(
        config.ui, language="en", voice_mode="tts", tts_server="azure-tts-v1",
        voice_name="en-US-JennyNeural-Female",
    )
    with (
        patch.object(config, "app", dict(config.app, video_source="pexels", match_materials_to_script=False)),
        patch.object(config, "ui", test_ui_config),
        patch.object(config, "try_save_config", return_value=True),
        patch.object(voice, "get_all_azure_voices", return_value=["en-US-JennyNeural-Female"]),
    ):
        app = AppTest.from_file(str(Path(__file__).parents[2] / "webui" / "Main.py"), default_timeout=60)
        app.session_state["ui_language"] = "en"
        app.run()
        assert [str(item.value) for item in app.exception] == []
        select = next(item for item in app.selectbox if str(item.key).startswith("video_fit_mode_select"))
        assert select.options == ["Fill (center crop)", "Fit (black bars)", "Fit (blurred background)"]
        select.set_value("blur").run()
        assert [str(item.value) for item in app.exception] == []
    assert test_ui_config["video_fit_mode"] == "blur"
