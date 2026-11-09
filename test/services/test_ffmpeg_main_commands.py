"""Our own ffmpeg/ffprobe commands on main: the final render's filter-graph option per ffmpeg version and the
protocol whitelist of the ffprobe measurement (the other commands are covered by test_ffmpeg_protocol_whitelist)."""

import json
import subprocess
from unittest.mock import patch

import pytest

from app.services import material_upload, video
from app.utils import ffmpeg_inputs, utils


@pytest.fixture(autouse=True)
def fresh_cache():
    video._filter_script_option.cache_clear()
    yield
    video._filter_script_option.cache_clear()


@pytest.mark.parametrize("version, option", [
    ("ffmpeg version 5.1.6-0+deb12u1", "-filter_complex_script"),
    ("ffmpeg version 6.1.1-3ubuntu5", "-filter_complex_script"),
    ("ffmpeg version n7.0.2", "-/filter_complex"),
    ("ffmpeg version 7.1-essentials_build-www.gyan.dev", "-/filter_complex"),
    ("ffmpeg version 9.0.2-essentials_build-www.gyan.dev", "-/filter_complex"),
    ("ffmpeg version N-117000-gabcdef0123-20260901", "-/filter_complex"),
])
def test_filter_graph_option_follows_the_ffmpeg_version(version, option):
    with patch.object(utils, "ffmpeg_version_line", return_value=version):
        assert video._filter_script_option(f"ffmpeg-{version}") == option


def test_unreadable_version_uses_the_current_option():
    with patch.object(utils, "ffmpeg_version_line", side_effect=OSError("missing")):
        assert video._filter_script_option("missing-ffmpeg") == "-/filter_complex"


def test_the_installed_ffmpeg_reads_the_graph_file(tmp_path):
    """9.0 removed -filter_complex_script: the chosen option must work with the ffmpeg in use."""
    binary = utils.get_ffmpeg_binary()
    (tmp_path / "graph.txt").write_text("[0:v]scale=32:32[out]", encoding="utf-8")
    result = subprocess.run(
        [binary, "-nostdin", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=d=0.2:s=64x64",
         video._filter_script_option(binary), "graph.txt", "-map", "[out]", "-f", "null", "-"],
        cwd=tmp_path, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr


def test_ffprobe_measurement_restricts_its_input(tmp_path):
    clip = str(tmp_path / "clip.mp4")
    seen = []

    def run(command, **kwargs):
        seen.append(command)
        payload = {"streams": [{"avg_frame_rate": "30/1", "duration": "2.5"}]}
        return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")

    with (
        patch.object(material_upload, "_ffprobe_binary", return_value="ffprobe"),
        patch.object(material_upload.subprocess, "run", side_effect=run),
    ):
        assert material_upload._probe_video_metadata(clip) == (2.5, 30.0)
    assert seen[0][-3:] == ["-protocol_whitelist", ffmpeg_inputs.FILE_PROTOCOLS, clip]


def test_real_ffprobe_rejects_a_playlist(tmp_path):
    ffprobe = material_upload._ffprobe_binary(utils.get_ffmpeg_binary())
    if not ffprobe:
        pytest.skip("no ffprobe next to the ffmpeg in use (imageio-ffmpeg ships none)")
    playlist = tmp_path / "remote.m3u8"
    playlist.write_text("#EXTM3U\n#EXT-X-TARGETDURATION:1\n#EXTINF:1.0,\nhttp://127.0.0.1:9/a.ts\n#EXT-X-ENDLIST\n",
                        encoding="utf-8")
    with pytest.raises(subprocess.CalledProcessError) as error:
        material_upload._probe_video_metadata(str(playlist))
    assert "not on whitelist 'file'" in error.value.stderr
