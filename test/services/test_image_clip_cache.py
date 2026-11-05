"""Image clips: reuse of rendered clips and direct rendering at the output size."""

import math
import subprocess
import wave
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest
from moviepy import VideoFileClip
from PIL import Image, ImageDraw

from app.models.schema import ImageMotion, MaterialInfo, VideoAspect, VideoFitMode, VideoParams
from app.services import task, video
from app.utils import utils

_SMALL_RESOLUTIONS = {
    VideoAspect.portrait: (72, 128),
    VideoAspect.landscape: (128, 72),
    VideoAspect.square: (96, 96),
}


def _scene(path, size):
    """A smooth gradient with a few shapes: geometry is visible, encoding loss stays small."""
    width, height = size
    x = np.linspace(0, 255, width)[np.newaxis, :]
    y = np.linspace(0, 255, height)[:, np.newaxis]
    pixels = np.dstack([np.repeat(x, height, 0), np.repeat(y, width, 1), np.full((height, width), 96)])
    image = Image.fromarray(pixels.astype("uint8"))
    draw = ImageDraw.Draw(image)
    draw.rectangle((width * 0.4, height * 0.4, width * 0.6, height * 0.6), fill="white")
    draw.ellipse((width * 0.1, height * 0.1, width * 0.25, height * 0.25), fill="black")
    image.save(path)
    return path


def _frame(path, time_seconds):
    with VideoFileClip(str(path), audio=False) as clip:
        return clip.get_frame(time_seconds).astype(float), list(clip.size)


def _no_render(*_args, **_kwargs):
    raise AssertionError("the clip was rendered again")


# ---------------------------------------------------------------- cache key

def test_every_render_setting_changes_the_clip_name():
    base = dict(clip_duration=5, motion=ImageMotion.zoom, output_size=(1080, 1920), fit_mode=VideoFitMode.cover)
    reference = video._image_clip_settings_identity(**base)
    changed = [
        {**base, "clip_duration": 6},
        {**base, "motion": ImageMotion.static},
        {**base, "output_size": (1920, 1080)},
        {**base, "output_size": None},
        {**base, "fit_mode": VideoFitMode.contain},
    ]
    identities = {video._image_clip_settings_identity(**options) for options in changed}
    assert reference not in identities and len(identities) == len(changed)
    for name, value in (
        ("_IMAGE_CLIP_RENDER_VERSION", video._IMAGE_CLIP_RENDER_VERSION + 1),
        ("_IMAGE_ZOOM_RATE_PER_SECOND", 0.04),
        ("_IMAGE_CLIP_FPS", 25),
        ("_IMAGE_CLIP_ENCODING", {**video._IMAGE_CLIP_ENCODING, "preset": "veryfast"}),
        ("_IMAGE_CLIP_ENCODING", {**video._IMAGE_CLIP_ENCODING, "codec": "libx265"}),
        ("_IMAGE_CLIP_ENCODING", {**video._IMAGE_CLIP_ENCODING, "ffmpeg_params": ["-vf", "null"]}),
    ):
        with patch.object(video, name, value):
            assert video._image_clip_settings_identity(**base) != reference, name
    # Without an output size the fit mode is not used and does not split the cache.
    native = {**base, "output_size": None}
    assert video._image_clip_settings_identity(**native) == video._image_clip_settings_identity(
        **{**native, "fit_mode": VideoFitMode.contain}
    )


def test_source_pixels_and_settings_name_the_clip_in_the_image_directory(tmp_path):
    source = _scene(tmp_path / "scene.png", (64, 64))
    native = video.render_image_zoom_video(str(source), 0.2)
    sized = video.render_image_zoom_video(str(source), 0.2, output_size=(32, 48))
    assert Path(native).parent == Path(sized).parent == tmp_path
    assert native != sized
    assert Path(native).name.split(".")[0] == Path(sized).name.split(".")[0]
    Image.new("RGB", (64, 64), "blue").save(source)
    assert video.render_image_zoom_video(str(source), 0.2, output_size=(32, 48)) != sized


def test_clips_are_encoded_without_b_frames(tmp_path):
    """Same encoder settings as combine_videos' temporary clips, so they can be stream-copied."""
    source = _scene(tmp_path / "scene.png", (64, 64))
    output = video.render_image_zoom_video(str(source), 0.5, output_size=(32, 48))
    stream = subprocess.run(
        [utils.get_ffmpeg_binary(), "-i", output, "-c:v", "copy", "-bsf:v", "h264_mp4toannexb",
         "-frames:v", "1", "-f", "h264", "-"],
        capture_output=True,
    ).stdout
    assert b"x264 - core" in stream and b" bframes=0 " in stream
    assert video._IMAGE_CLIP_RENDER_VERSION == 3


# ---------------------------------------------------------------- reuse and partial files

def test_published_clip_is_reused_without_encoding(tmp_path):
    source = _scene(tmp_path / "scene.png", (64, 64))
    first = video.render_image_zoom_video(str(source), 0.2, output_size=(32, 48))
    published = Path(first).read_bytes()
    with (
        patch.object(video.VideoClip, "write_videofile", _no_render),
        patch.object(video.CompositeVideoClip, "write_videofile", _no_render),
    ):
        assert video.render_image_zoom_video(str(source), 0.2, output_size=(32, 48)) == first
    assert Path(first).read_bytes() == published


def test_unpublished_temporary_or_empty_files_are_not_reused(tmp_path):
    source = _scene(tmp_path / "scene.png", (64, 64))
    output = Path(video.render_image_zoom_video(str(source), 0.2, output_size=(32, 48)))
    complete = output.read_bytes()
    # A forced exit can leave an unpublished temporary file and, through an
    # interrupted copy, an empty final file. Neither counts as a rendered clip.
    output.unlink()
    leftover = tmp_path / ".image-zoom-interrupted.mp4"
    leftover.write_bytes(complete[: len(complete) // 2])
    rendered = Path(video.render_image_zoom_video(str(source), 0.2, output_size=(32, 48)))
    assert rendered == output and rendered.stat().st_size > 0
    output.write_bytes(b"")
    assert Path(video.render_image_zoom_video(str(source), 0.2, output_size=(32, 48))).stat().st_size > 0
    _, size = _frame(output, 0)
    assert size == [32, 48]


def test_failed_target_size_render_publishes_nothing(tmp_path):
    source = _scene(tmp_path / "scene.png", (64, 64))

    def interrupted_write(_clip, filename, **_kwargs):
        Path(filename).write_bytes(b"partial video")
        raise RuntimeError("encoder interrupted")

    with patch.object(video.VideoClip, "write_videofile", interrupted_write):
        with pytest.raises(RuntimeError, match="encoder interrupted"):
            video.render_image_zoom_video(str(source), 0.2, output_size=(32, 48))
    assert not list(tmp_path.glob("*.mp4")) and not list(tmp_path.glob(".image-zoom-*"))


def test_clip_published_by_another_task_is_used_when_windows_refuses_the_replace(tmp_path):
    source = _scene(tmp_path / "scene.png", (64, 64))
    output = Path(video.render_image_zoom_video(str(source), 0.2, output_size=(32, 48)))
    published = output.read_bytes()
    real_exists = video._rendered_clip_exists
    checks = []

    def first_check_misses(path):
        # The first check runs before the other task publishes the clip.
        checks.append(path)
        return len(checks) > 1 and real_exists(path)

    with (
        patch.object(video, "_rendered_clip_exists", first_check_misses),
        patch.object(video.os, "replace", side_effect=PermissionError("in use")),
    ):
        assert video.render_image_zoom_video(str(source), 0.2, output_size=(32, 48)) == str(output)
    assert output.read_bytes() == published
    assert not list(tmp_path.glob(".image-zoom-*"))


# ---------------------------------------------------------------- output size

def test_prepared_image_renders_at_output_size_without_upscaling(tmp_path):
    # NASA prepares the crop with the zoom margin: target × (1 + 0.03 × duration).
    duration, target = 5, (100, 180)
    max_zoom = 1 + video._IMAGE_ZOOM_RATE_PER_SECOND * duration
    prepared = (round(target[0] * max_zoom), round(target[1] * max_zoom))
    source = _scene(tmp_path / "prepared.png", prepared)
    region, band = video._target_size_layout(*prepared, *target, VideoFitMode.cover)
    assert band == target and region == (0.0, 0.0, float(prepared[0]), float(prepared[1]))
    for time_seconds in np.linspace(0, duration, 11):
        zoom = video._image_zoom_factor(time_seconds, ImageMotion.zoom)
        # Output pixels per source pixel never exceeds 1: the margin is the zoom.
        assert band[0] / (region[2] / zoom) <= 1 + 1e-9
        assert band[1] / (region[3] / zoom) <= 1 + 1e-9
    output = video.render_image_zoom_video(str(source), duration, output_size=target)
    first, size = _frame(output, 0)
    last, _ = _frame(output, duration - 1 / 30)
    assert size == list(target)
    with Image.open(source) as image:
        whole = np.asarray(image.convert("RGB").resize(target, Image.LANCZOS), dtype=float)
        left, top = (prepared[0] - target[0]) / 2, (prepared[1] - target[1]) / 2
        center = np.asarray(
            image.convert("RGB").crop((round(left), round(top), round(left) + target[0], round(top) + target[1])),
            dtype=float,
        )
    # The first frame shows the whole prepared crop, the last its 1:1 center.
    assert np.abs(first - whole).mean() < 6
    assert np.abs(last - center).mean() < 8


def _legacy_frames(source, duration, target, fit_mode, times):
    native = video.render_image_zoom_video(str(source), duration)
    with VideoFileClip(native, audio=False) as clip:
        fitted = video._fit_clip_to_canvas(
            clip, target_width=target[0], target_height=target[1], fit_mode=fit_mode
        )
        return [fitted.get_frame(t).astype(float) for t in times]


@pytest.mark.parametrize("aspect", list(_SMALL_RESOLUTIONS))
@pytest.mark.parametrize("fit_mode", [VideoFitMode.cover, VideoFitMode.contain])
@pytest.mark.parametrize("source_size", [(240, 160), (160, 240)])
def test_output_size_matches_the_native_render_fitted_by_combine(tmp_path, aspect, fit_mode, source_size):
    target = _SMALL_RESOLUTIONS[aspect]
    duration, times = 1, (0, 0.5, 29 / 30)
    source = _scene(tmp_path / "scene.png", source_size)
    expected = _legacy_frames(source, duration, target, fit_mode, times)
    direct = video.render_image_zoom_video(str(source), duration, output_size=target, fit_mode=fit_mode)
    for time_seconds, legacy in zip(times, expected):
        frame, size = _frame(direct, time_seconds)
        assert size == list(target)
        error = np.abs(frame - legacy).mean()
        # Measured 1.0–2.1 (resampling and one encode); a wrong fit or zoom is far larger.
        assert error < 4, (time_seconds, error)


def test_static_target_size_clip_keeps_alpha_composition(tmp_path):
    source = tmp_path / "alpha.png"
    Image.new("RGBA", (96, 96), (240, 0, 0, 128)).save(source)
    output = video.render_image_zoom_video(str(source), 0.2, image_motion="static", output_size=(48, 48))
    frame, size = _frame(output, 0)
    assert size == [48, 48]
    assert 110 <= frame[24, 24, 0] <= 130 and max(frame[24, 24, 1:]) < 5


def test_large_source_is_reduced_once_before_the_zoom(tmp_path):
    source = _scene(tmp_path / "large.png", (900, 1600))
    resizes = []
    real_resize = Image.Image.resize

    def counting_resize(image, size, *args, **kwargs):
        resizes.append((image.size, size))
        return real_resize(image, size, *args, **kwargs)

    with patch.object(Image.Image, "resize", counting_resize):
        video.render_image_zoom_video(str(source), 1, output_size=(72, 128))
    needed = (math.ceil(72 * 1.03), math.ceil(128 * 1.03))
    assert resizes[0] == ((900, 1600), needed)
    assert all(source_size == needed for source_size, _ in resizes[1:])


# ---------------------------------------------------------------- local materials

def _silent_audio(path, seconds):
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(24000)
        writer.writeframes(b"\0\0" * int(24000 * seconds))
    return path


@pytest.mark.parametrize("aspect", [VideoAspect.portrait, VideoAspect.landscape, VideoAspect.square])
def test_local_image_is_rendered_at_the_video_size_and_not_resized_again(tmp_path, monkeypatch, aspect):
    monkeypatch.setattr(VideoAspect, "to_resolution", lambda self: _SMALL_RESOLUTIONS[VideoAspect(self)])
    monkeypatch.setattr(video, "_get_configured_video_codec", lambda: "libx264")
    monkeypatch.setattr(video, "_get_effective_video_codec", lambda *args: "libx264")
    local_dir = Path(utils.storage_dir("local_videos", create=True))
    image = _scene(local_dir / f"local-{aspect.name}.png", (640, 480))
    materials = video.preprocess_video(
        [MaterialInfo(provider="local", url=image.name)],
        clip_duration=1,
        video_aspect=aspect,
        video_fit_mode=VideoFitMode.contain,
    )
    clip_path = materials[0].url
    _, size = _frame(clip_path, 0)
    assert size == list(_SMALL_RESOLUTIONS[aspect])
    with patch.object(video, "_fit_clip_to_canvas", wraps=video._fit_clip_to_canvas) as fit:
        combined = video.combine_videos(
            str(tmp_path / "combined.mp4"),
            [clip_path],
            str(_silent_audio(tmp_path / "audio.wav", 0.8)),
            video_aspect=aspect,
            video_fit_mode=VideoFitMode.contain,
            max_clip_duration=1,
            threads=1,
        )
    fit.assert_not_called()
    _, size = _frame(combined, 0)
    assert size == list(_SMALL_RESOLUTIONS[aspect])


def test_local_images_without_an_aspect_keep_the_native_render(tmp_path):
    local_dir = Path(utils.storage_dir("local_videos", create=True))
    image = _scene(local_dir / "native.png", (640, 480))
    with patch.object(video, "render_image_zoom_video", return_value="clip.mp4") as render:
        video.preprocess_video([MaterialInfo(provider="local", url=image.name)], clip_duration=2)
    assert render.call_args.kwargs == {"image_motion": ImageMotion.zoom}


def test_task_passes_the_video_aspect_and_fit_mode_to_local_images():
    params = VideoParams(
        video_subject="test", video_source="local", video_aspect=VideoAspect.landscape,
        video_fit_mode=VideoFitMode.contain,
    )
    with patch.object(video, "preprocess_video", return_value=[MaterialInfo(url="local.mp4")]) as preprocess:
        assert task.get_video_materials("aspect-test", params, [], 2) == ["local.mp4"]
    assert preprocess.call_args.kwargs["video_aspect"] == VideoAspect.landscape
    assert preprocess.call_args.kwargs["video_fit_mode"] == VideoFitMode.contain
