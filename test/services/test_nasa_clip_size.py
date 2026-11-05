"""NASA 图片片段直接按成片尺寸渲染：放大余量留在处理后的图片里，合成阶段不再缩放；
已渲染的片段在后续任务里复用。不访问网络。"""

import math
import wave
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest
from moviepy import VideoFileClip
from PIL import Image

from app.models.schema import ImageMotion, MaterialInfo, VideoAspect
from app.services import nasa, video

_SMALL_RESOLUTIONS = {
    VideoAspect.portrait: (72, 128),
    VideoAspect.landscape: (128, 72),
    VideoAspect.square: (96, 96),
}


@pytest.fixture
def small_video(monkeypatch):
    monkeypatch.setattr(VideoAspect, "to_resolution", lambda self: _SMALL_RESOLUTIONS[VideoAspect(self)])
    monkeypatch.setattr(video, "_get_configured_video_codec", lambda: "libx264")
    monkeypatch.setattr(video, "_get_effective_video_codec", lambda *args: "libx264")


def _original(path, size=(800, 600)):
    width, height = size
    x = np.linspace(0, 255, width)[np.newaxis, :]
    y = np.linspace(0, 255, height)[:, np.newaxis]
    pixels = np.dstack([np.repeat(x, height, 0), np.repeat(y, width, 1), np.full((height, width), 80)])
    Image.fromarray(pixels.astype("uint8")).save(path, "JPEG", quality=92)
    return str(path)


def _item():
    return MaterialInfo(provider="nasa", url="https://images-assets.nasa.gov/image/x/x~orig.jpg", duration=5)


def _size(path):
    with VideoFileClip(str(path), audio=False) as clip:
        return list(clip.size)


def test_zoom_rate_matches_the_renderer():
    assert nasa.ZOOM_RATE_PER_SECOND == video._IMAGE_ZOOM_RATE_PER_SECOND


@pytest.mark.parametrize("aspect", list(_SMALL_RESOLUTIONS))
def test_clip_is_rendered_at_the_video_size_from_the_prepared_margin(tmp_path, small_video, aspect):
    target = _SMALL_RESOLUTIONS[aspect]
    with patch.object(nasa, "_download_image", return_value=_original(tmp_path / "nasa-x.jpg")):
        clip = nasa.save_image_clip(_item(), str(tmp_path), video_aspect=aspect, clip_duration=2)
    (prepared,) = tmp_path.glob(f"nasa-x-{aspect.name}-*.jpg")
    with Image.open(prepared) as image:
        prepared_size = image.size
    # The prepared crop keeps the zoom margin; the clip itself is the video size.
    assert prepared_size[0] == math.ceil(target[0] * nasa.zoom_margin(2))
    assert _size(clip) == list(target)
    region, band = video._target_size_layout(*prepared_size, *target, video.VideoFitMode.cover)
    final_zoom = video._image_zoom_factor(2, ImageMotion.zoom)
    # At the final zoom the visible source region still covers the output; prepare_image
    # floors the height, so tiny test sizes can fall short by under one pixel.
    assert band == target
    assert region[2] / final_zoom >= target[0] - 1 and region[3] / final_zoom >= target[1] - 1


def test_combine_videos_does_not_resize_the_clip(tmp_path, small_video):
    with patch.object(nasa, "_download_image", return_value=_original(tmp_path / "nasa-x.jpg")):
        clip = nasa.save_image_clip(_item(), str(tmp_path), video_aspect=VideoAspect.portrait, clip_duration=1)
    audio = tmp_path / "audio.wav"
    with wave.open(str(audio), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(24000)
        writer.writeframes(b"\0\0" * 19200)
    with patch.object(video, "_fit_clip_to_canvas", wraps=video._fit_clip_to_canvas) as fit:
        combined = video.combine_videos(str(tmp_path / "combined.mp4"), [clip], str(audio),
                                        video_aspect=VideoAspect.portrait, max_clip_duration=1, threads=1)
    fit.assert_not_called()
    assert _size(combined) == [72, 128]


def test_rendered_clip_is_reused_by_the_next_task(tmp_path, small_video):
    with patch.object(nasa, "_download_image", return_value=_original(tmp_path / "nasa-x.jpg")):
        first = nasa.save_image_clip(_item(), str(tmp_path), video_aspect=VideoAspect.portrait, clip_duration=1)
        published = Path(first).read_bytes()

        def no_render(*_args, **_kwargs):
            raise AssertionError("the clip was rendered again")

        with patch.object(video.VideoClip, "write_videofile", no_render):
            again = nasa.save_image_clip(_item(), str(tmp_path), video_aspect=VideoAspect.portrait, clip_duration=1)
    assert again == first and Path(first).read_bytes() == published
