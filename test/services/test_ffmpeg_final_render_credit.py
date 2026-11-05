"""ffmpeg 成片路径叠加 NASA 署名（nasa_credit_overlay）：署名层在字幕之后合成，ffmpeg 路径从合成结构里
读出它，不需要单独的署名代码；画面与 MoviePy 路径逐帧一致。"""

import re
import subprocess
from unittest.mock import patch

import numpy as np
import pytest
from moviepy import VideoFileClip

from app.config import config
from app.models.schema import VideoAspect, VideoParams
from app.services import video

FF = video.utils.get_ffmpeg_binary()
SIZES = {VideoAspect.portrait: (180, 320), VideoAspect.landscape: (320, 180), VideoAspect.square: (240, 240)}


@pytest.fixture(scope="module")
def inputs(tmp_path_factory):
    root = tmp_path_factory.mktemp("credit")
    for aspect, (width, height) in SIZES.items():
        subprocess.run([FF, "-y", "-f", "lavfi", "-i", f"testsrc2=s={width}x{height}:r=30", "-t", "3",
                        "-vf", video._BT709_VIDEO_FILTER, "-c:v", "libx264", "-pix_fmt", "yuv420p",
                        str(root / f"base-{aspect.name}.mp4")], check=True, capture_output=True)
    subprocess.run([FF, "-y", "-f", "lavfi", "-i", "sine=f=440:d=2.9", "-ar", "44100", str(root / "voice.wav")],
                   check=True, capture_output=True)
    (root / "subtitles.srt").write_text("1\n00:00:00,200 --> 00:00:02,800\nZombi yıldızlar\n\n", encoding="utf-8")
    return root


def _render(root, engine, aspect, position):
    target = root / f"{aspect.name}-{position}-{engine}.mp4"
    params = VideoParams(video_subject="x", video_aspect=aspect.value, font_size=18, bgm_type="",
                         subtitle_position=position)
    with (
        patch.dict(config.app, {"final_render_engine": engine}),
        patch.object(VideoAspect, "to_resolution", lambda self: SIZES[VideoAspect(self)]),
        patch.object(video, "_run_ffmpeg_final_command", wraps=video._run_ffmpeg_final_command) as command,
    ):
        video.generate_video(str(root / f"base-{aspect.name}.mp4"), str(root / "voice.wav"), str(root / "subtitles.srt"),
                             str(target), params, credit_text="Görüntüler: NASA, JPL ve diğerleri")
    assert command.called is (engine == "ffmpeg")
    return target


@pytest.mark.parametrize("aspect", list(SIZES))
@pytest.mark.parametrize("position", ["bottom", "top"])
def test_credit_overlay_matches_moviepy(inputs, aspect, position):
    moviepy_output = _render(inputs, "moviepy", aspect, position)
    ffmpeg_output = _render(inputs, "ffmpeg", aspect, position)
    stats = inputs / "ssim.log"
    subprocess.run([FF, "-hide_banner", "-i", str(moviepy_output), "-i", str(ffmpeg_output), "-lavfi",
                    f"[0:v][1:v]ssim=stats_file={stats.name}", "-f", "null", "-"], cwd=inputs, capture_output=True)
    scores = [float(value) for value in re.findall(r"All:([\d.]+)", stats.read_text())]
    assert len(scores) == 90 and min(scores) >= 0.98
    # 署名只在最后 2 秒出现：第 0.5 秒与第 2.5 秒在署名区域不同。
    with VideoFileClip(str(ffmpeg_output), audio=False) as clip:
        early, late = clip.get_frame(0.5).astype(float), clip.get_frame(2.5).astype(float)
    with VideoFileClip(str(moviepy_output), audio=False) as clip:
        reference = clip.get_frame(2.5).astype(float)
    assert np.abs(late - early).mean() > 1
    assert np.abs(late - reference).mean() < 2
