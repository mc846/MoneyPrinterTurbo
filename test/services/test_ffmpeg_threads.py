"""FFmpeg thread count: unset by default (FFmpeg decides), an explicit value is used."""

import inspect
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from moviepy import ColorClip

from app.models.schema import TaskVideoRequest, VideoParams
from app.services import video


class _FakeAudio:
    duration = 1

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()

    def close(self):
        pass

    def with_effects(self, _effects):
        return self


def test_thread_count_defaults_to_unset_and_keeps_explicit_values():
    assert VideoParams(video_subject="x").n_threads is None
    assert TaskVideoRequest(video_subject="x").n_threads is None
    assert VideoParams(video_subject="x", n_threads=2).n_threads == 2
    assert TaskVideoRequest(video_subject="x", n_threads=3).n_threads == 3
    assert inspect.signature(video.combine_videos).parameters["threads"].default is None


def _concat_command(tmp_path, threads):
    commands = []

    def run(command, _output_file):
        commands.append(command)
        Path(command[-1]).write_bytes(b"encoded-video")
        return SimpleNamespace(returncode=0)

    with (
        patch.object(video, "_run_concat_with_heartbeat", side_effect=run),
        patch.object(video, "_get_effective_video_codec", return_value="libx264"),
    ):
        video.concat_video_clips_with_ffmpeg(
            [str(tmp_path / "clip.mp4")], str(tmp_path / "out.mp4"), threads, str(tmp_path), 2.0
        )
    return commands[0]


@pytest.mark.parametrize("threads", [None, 0])
def test_concat_leaves_the_thread_count_to_ffmpeg_when_unset(tmp_path, threads):
    command = _concat_command(tmp_path, threads)
    assert "-threads" not in command
    # The remaining output options are unchanged.
    assert command[command.index("-c:v") + 1] == "libx264"
    assert command[command.index("-vf") + 1] == video._BT709_VIDEO_FILTER
    assert command[command.index("-pix_fmt") + 1] == "yuv420p"
    assert command[command.index("-t") + 1] == "2.000"


def test_concat_uses_an_explicit_thread_count(tmp_path):
    command = _concat_command(tmp_path, 3)
    assert command[command.index("-threads") + 1] == "3"
    assert command.index("-c:v") < command.index("-threads") < command.index("-vf")


@pytest.mark.parametrize("n_threads, expected", [(None, None), (1, 1), (4, 4)])
def test_final_render_passes_the_thread_count_to_the_writer(tmp_path, n_threads, expected):
    params = VideoParams(
        video_subject="test", video_aspect="9:16", subtitle_enabled=False, bgm_type="",
        n_threads=n_threads,
    )
    calls = []
    with (
        patch.object(video, "_open_video_clip_quietly", return_value=ColorClip((1080, 1920), color=(0, 0, 0), duration=1)),
        patch.object(video, "AudioFileClip", return_value=_FakeAudio()),
        patch.object(video, "_write_videofile_with_codec_fallback", side_effect=lambda *a, **k: calls.append(k)),
        patch.object(video, "_get_configured_video_codec", return_value="libx264"),
    ):
        video.generate_video(
            video_path="combined.mp4", audio_path="voice.mp3", subtitle_path="",
            output_file=str(tmp_path / "final.mp4"), params=params,
        )
    assert calls[0]["threads"] == expected
