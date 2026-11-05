"""Cached stock videos: the metadata probe that replaced VideoFileClip in the validator."""

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from moviepy import VideoFileClip

from app.services import material, material_upload
from app.utils import utils


@pytest.fixture(autouse=True)
def _fresh_ffprobe_lookup():
    material_upload._ffprobe_binary.cache_clear()
    yield
    material_upload._ffprobe_binary.cache_clear()


def _video(path, seconds=1, rate=25):
    subprocess.run(
        [utils.get_ffmpeg_binary(), "-y", "-f", "lavfi", "-i", f"testsrc=s=64x48:r={rate}", "-t", str(seconds),
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)],
        check=True, capture_output=True,
    )
    return path


def test_fallback_probe_reads_the_values_videofileclip_reported(tmp_path):
    source = _video(tmp_path / "clip.mp4", seconds=1.2, rate=25)
    with patch.object(material_upload, "_ffprobe_binary", return_value=""):
        duration, fps = material_upload._probe_video_metadata(str(source))
    with VideoFileClip(str(source), audio=False) as clip:
        assert (duration, fps) == (clip.duration, clip.fps)
    assert duration == pytest.approx(1.2, abs=0.05) and fps == 25


def test_valid_cached_video_is_accepted_without_opening_a_videofileclip(tmp_path):
    source = _video(tmp_path / "clip.mp4")
    with (
        patch.object(material_upload, "_ffprobe_binary", return_value=""),
        patch.object(material, "VideoFileClip", side_effect=AssertionError("frame reader opened")),
    ):
        assert material._is_valid_video_file(str(source)) is True


@pytest.mark.parametrize("damage", ["truncated", "garbage"])
def test_broken_video_is_rejected(tmp_path, damage):
    source = _video(tmp_path / "clip.mp4", seconds=2)
    data = source.read_bytes()
    source.write_bytes(data[: len(data) // 2] if damage == "truncated" else b"not a video" * 100)
    with patch.object(material_upload, "_ffprobe_binary", return_value=""):
        assert material._is_valid_video_file(str(source)) is False


def test_corrupt_cached_video_is_downloaded_again(tmp_path):
    url = "https://example.com/stock.mp4"
    valid = _video(tmp_path / "valid.mp4").read_bytes()
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    cached = cache_dir / f"vid-{utils.md5(url)}.mp4"
    cached.write_bytes(valid[: len(valid) // 3])

    class Response:
        headers = {}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def raise_for_status(self):
            pass

        def iter_content(self, chunk_size):
            yield valid

    with (
        patch.object(material_upload, "_ffprobe_binary", return_value=""),
        patch.object(material.requests, "get", return_value=Response()) as get,
    ):
        assert Path(material.save_video(url, str(cache_dir))) == cached
    get.assert_called_once()
    assert cached.read_bytes() == valid


@pytest.mark.parametrize("probe, valid", [((5.0, 0.0), False), ((0.0, 30.0), False), ((float("nan"), 30.0), False), ((5.0, 30.0), True)])
def test_validity_criterion_is_positive_duration_and_fps(tmp_path, probe, valid):
    with (
        patch.object(material_upload, "_validate_video"),
        patch.object(material_upload, "_probe_video_metadata", return_value=probe),
    ):
        assert material._is_valid_video_file(str(tmp_path / "clip.mp4")) is valid


def _ffprobe_output(payload):
    return SimpleNamespace(stdout=json.dumps(payload), returncode=0)


@pytest.mark.parametrize(
    "payload, expected",
    [
        ({"streams": [{"avg_frame_rate": "30000/1001", "duration": "4.5"}]}, (4.5, 30000 / 1001)),
        ({"streams": [{"avg_frame_rate": "0/0", "r_frame_rate": "25/1"}], "format": {"duration": "3.0"}}, (3.0, 25.0)),
        ({"streams": [], "format": {"duration": "3.0"}}, (3.0, 0.0)),
        ({}, (0.0, 0.0)),
    ],
)
def test_ffprobe_output_is_parsed(payload, expected):
    with (
        patch.object(material_upload, "_ffprobe_binary", return_value="ffprobe"),
        patch.object(material_upload.subprocess, "run", return_value=_ffprobe_output(payload)) as run,
    ):
        assert material_upload._probe_video_metadata("clip.mp4") == pytest.approx(expected)
    command = run.call_args.args[0]
    assert command[0] == "ffprobe" and command[-1] == "clip.mp4"
    assert command[command.index("-select_streams") + 1] == "V:0"
    assert run.call_args.kwargs["check"] is True


def test_ffprobe_failure_keeps_the_existing_cache(tmp_path):
    url = "https://example.com/cached.mp4"
    cached = tmp_path / f"vid-{utils.md5(url)}.mp4"
    cached.write_bytes(b"healthy cached video")
    failure = subprocess.CalledProcessError(1, ["ffprobe"])
    with (
        patch.object(material_upload, "_validate_video"),
        patch.object(material_upload, "_ffprobe_binary", return_value="ffprobe"),
        patch.object(material_upload.subprocess, "run", side_effect=failure),
        patch.object(material.requests, "get") as get,
    ):
        assert Path(material.save_video(url, str(tmp_path))) == cached
    get.assert_not_called()
    assert cached.read_bytes() == b"healthy cached video"


def test_ffprobe_is_found_next_to_ffmpeg_or_on_path(tmp_path):
    (tmp_path / "ffmpeg.exe").write_bytes(b"")
    (tmp_path / "ffprobe.exe").write_bytes(b"")
    assert material_upload._ffprobe_binary(str(tmp_path / "ffmpeg.exe")) == str(tmp_path / "ffprobe.exe")
    # imageio-ffmpeg ships a single renamed binary without ffprobe.
    bundled = tmp_path / "ffmpeg-win-x86_64-v7.1.exe"
    with patch.object(material_upload.shutil, "which", return_value=None):
        assert material_upload._ffprobe_binary(str(bundled)) == ""
    material_upload._ffprobe_binary.cache_clear()
    with patch.object(material_upload.shutil, "which", return_value="/usr/bin/ffprobe"):
        assert material_upload._ffprobe_binary(str(bundled)) == "/usr/bin/ffprobe"
