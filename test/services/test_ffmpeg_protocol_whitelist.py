"""
Every ffmpeg input is restricted to local protocols: a local playlist that names a
URL is rejected before any connection, and ordinary media is unaffected.

The playlists point at 127.0.0.1:9 (discard port on loopback), so even an ffmpeg
that ignored the whitelist would not resolve a host or leave the machine.
"""

import re
import subprocess
from pathlib import Path

import pytest
from moviepy import AudioFileClip, ColorClip, VideoFileClip
from moviepy.video.io.ffmpeg_reader import ffmpeg_parse_infos

from app.services import video  # noqa: F401 -- importing installs the library guard
from app.utils import ffmpeg_inputs, utils

ROOT = Path(__file__).resolve().parents[2]
FF = utils.get_ffmpeg_binary()
REJECTED = "not on whitelist 'file'"
HLS = "#EXTM3U\n#EXT-X-TARGETDURATION:1\n#EXTINF:1.0,\n{}\n#EXT-X-ENDLIST\n"
DASH = (
    '<?xml version="1.0"?>\n<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static" '
    'mediaPresentationDuration="PT1S" minBufferTime="PT1S" profiles="urn:mpeg:dash:profile:isoff-on-demand:2011">'
    '<Period><AdaptationSet mimeType="video/mp4"><Representation id="1" bandwidth="1000" codecs="avc1.42c01e" '
    'width="64" height="64"><BaseURL>http://127.0.0.1:9/video.mp4</BaseURL></Representation></AdaptationSet>'
    "</Period></MPD>\n"
)


def _run(*args):
    return subprocess.run([FF, "-nostdin", "-hide_banner", "-v", "error", *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=60)


def _demuxers():
    return _run("-demuxers").stdout


def _ffmpeg_version():
    first = _run("-version").stdout.splitlines()[:1]
    match = re.match(r"ffmpeg version n?(\d+)\.(\d+)(?:\.(\d+))?", first[0] if first else "")
    return tuple(int(part or 0) for part in match.groups()) if match else None


@pytest.fixture(scope="module")
def media(tmp_path_factory):
    folder = tmp_path_factory.mktemp("media")
    paths = {
        "mp4": folder / "clip.mp4",
        "mp3": folder / "voice.mp3",
        "png": folder / "frame.png",
    }
    assert _run("-y", "-f", "lavfi", "-i", "testsrc2=d=1:s=64x64:r=30", "-f", "lavfi", "-i", "sine=d=1",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(paths["mp4"])).returncode == 0
    assert _run("-y", "-f", "lavfi", "-i", "sine=d=1", str(paths["mp3"])).returncode == 0
    assert _run("-y", "-f", "lavfi", "-i", "testsrc2=s=64x64", "-frames:v", "1", str(paths["png"])).returncode == 0
    return paths


@pytest.fixture
def hls(tmp_path):
    if " hls " not in _demuxers():
        pytest.skip("ffmpeg has no HLS demuxer")
    path = tmp_path / "remote.m3u8"
    path.write_text(HLS.format("http://127.0.0.1:9/segment.ts"), encoding="utf-8")
    return path


def test_input_arguments_and_rewrite():
    assert ffmpeg_inputs.ffmpeg_input(Path("a.mp4")) == ["-protocol_whitelist", "file", "-i", "a.mp4"]
    assert ffmpeg_inputs.ffmpeg_input("-") == ["-protocol_whitelist", "fd,pipe", "-i", "-"]
    assert ffmpeg_inputs.input_protocols("pipe:0") == "fd,pipe"
    assert ffmpeg_inputs.input_protocols("cache:pipe:0") == "cache,pipe"
    command = ["ffmpeg", "-y", "-f", "rawvideo", "-i", "-", "-ss", "1", "-i", Path("a.mp3"),
               "-protocol_whitelist", "file,crypto", "-i", "b.m3u8", "out.mp4"]
    assert ffmpeg_inputs.restrict_ffmpeg_inputs(command) == [
        "ffmpeg", "-y", "-f", "rawvideo", "-protocol_whitelist", "fd,pipe", "-i", "-",
        "-ss", "1", "-protocol_whitelist", "file", "-i", "a.mp3",
        "-protocol_whitelist", "file,crypto", "-i", "b.m3u8", "out.mp4",
    ]
    assert ffmpeg_inputs.restrict_ffmpeg_inputs([FF, "-version"]) == [FF, "-version"]
    assert ffmpeg_inputs.is_ffmpeg_command([r"C:\x\ffmpeg-win-x86_64-v7.1.exe", "-i", "a"])
    assert not ffmpeg_inputs.is_ffmpeg_command(["ffprobe", "a"])
    assert not ffmpeg_inputs.is_ffmpeg_command("ffmpeg -i a")


def test_moviepy_and_pydub_are_guarded():
    names = [name for name, _ in ffmpeg_inputs.LIBRARY_MODULES]
    assert ffmpeg_inputs.guard_libraries() == names
    assert ffmpeg_inputs.guard_libraries() == names  # idempotent


def test_application_commands_use_the_whitelist():
    """No application module passes an ffmpeg input without going through ffmpeg_inputs."""
    allowed = {
        "app/utils/ffmpeg_inputs.py",
        # VideoProject._run() rewrites every command with restrict_ffmpeg_inputs().
        "app/services/video_project.py",
    }
    offenders = [
        f"{path.relative_to(ROOT).as_posix()}:{number}"
        for path in sorted((ROOT / "app").rglob("*.py"))
        if path.relative_to(ROOT).as_posix() not in allowed
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if re.search(r"""["']-i["']""", line)
    ]
    assert offenders == []


def test_library_users_install_the_guard():
    """Modules that let MoviePy or pydub start ffmpeg call guard_libraries()."""
    library_use = re.compile(r"VideoFileClip|AudioFileClip|AudioSegment|ffmpeg_parse_infos|ffmpeg_tools")
    missing = [
        path.relative_to(ROOT).as_posix()
        for path in sorted((ROOT / "app").rglob("*.py"))
        if path.name != "ffmpeg_inputs.py"
        and library_use.search(text := path.read_text(encoding="utf-8"))
        and "guard_libraries()" not in text
    ]
    assert missing == []


def test_video_project_restricts_its_commands(tmp_path, hls):
    from app.services.video_project import ProjectError, VideoProject

    with pytest.raises(ProjectError, match=REJECTED):
        VideoProject(tmp_path / "project")._run(
            [FF, "-v", "error", "-i", str(hls), "-f", "null", "-"], tmp_path / "cancel.json", 30
        )


def test_local_playlist_cannot_open_urls(hls):
    result = _run(*ffmpeg_inputs.ffmpeg_input(hls), "-f", "null", "-")
    assert result.returncode != 0
    assert f"Protocol 'http' {REJECTED}" in result.stderr


@pytest.mark.parametrize("entry", ["http://127.0.0.1:9/clip.mp4", "data:video/mp2t;base64,R0dH"])
def test_concat_list_cannot_open_urls_or_inline_data(tmp_path, entry):
    """ffmpeg's default for local inputs (file,crypto,data) would open the data: URI."""
    listing = tmp_path / "list.txt"
    listing.write_text(f"file '{entry}'\n", encoding="utf-8")
    result = _run("-f", "concat", "-safe", "0", *ffmpeg_inputs.ffmpeg_input(listing), "-f", "null", "-")
    assert result.returncode != 0
    assert f"Protocol '{entry.split(':', 1)[0]}' {REJECTED}" in result.stderr


@pytest.mark.parametrize("name", ["remote.mpd", "disguised-manifest.mp4"])
def test_local_dash_manifest_cannot_open_urls(tmp_path, name):
    if " dash " not in _demuxers():
        pytest.skip("ffmpeg has no DASH demuxer (built without libxml2)")
    version = _ffmpeg_version()
    if version is not None and version < (7, 1, 1):
        pytest.skip(f"ffmpeg {version} ignores the protocol whitelist in its DASH demuxer (CVE-2023-6605)")
    manifest = tmp_path / name
    manifest.write_text(DASH, encoding="utf-8")
    result = _run(*ffmpeg_inputs.ffmpeg_input(manifest), "-f", "null", "-")
    assert result.returncode != 0
    assert f"Protocol 'http' {REJECTED}" in result.stderr


# MoviePy's FFMPEG_AudioReader.__del__ touches attributes a failed __init__ never set.
@pytest.mark.filterwarnings("ignore::pytest.PytestUnraisableExceptionWarning")
def test_moviepy_readers_reject_a_playlist(hls, monkeypatch):
    commands = []
    original = ffmpeg_inputs.restrict_ffmpeg_inputs

    def spy(command):
        commands.append(original(command))
        return commands[-1]

    monkeypatch.setattr(ffmpeg_inputs, "restrict_ffmpeg_inputs", spy)
    with pytest.raises(OSError, match=REJECTED):
        ffmpeg_parse_infos(str(hls))
    with pytest.raises(OSError, match=REJECTED):
        VideoFileClip(str(hls))
    with pytest.raises(OSError, match=REJECTED):
        AudioFileClip(str(hls))
    assert commands and all(c[c.index("-i") - 2:c.index("-i")] == ["-protocol_whitelist", "file"] for c in commands)


def test_pydub_rejects_a_playlist(hls):
    from pydub import AudioSegment
    from pydub.exceptions import CouldntDecodeError

    from app.services import voice

    voice._configure_pydub_ffmpeg(AudioSegment)
    with pytest.raises(CouldntDecodeError, match=REJECTED):
        AudioSegment.from_file(str(hls), format="hls", codec="pcm_s16le")


def test_ordinary_media_is_unaffected(media, tmp_path):
    for kind in ("mp4", "mp3", "png"):
        result = _run(*ffmpeg_inputs.ffmpeg_input(media[kind]), "-f", "null", "-")
        assert result.returncode == 0, (kind, result.stderr)
    # The concat demuxer opens the listed files under the list's whitelist.
    listing = tmp_path / "list.txt"
    listing.write_text(f"file '{media['mp4'].as_posix()}'\nfile '{media['mp4'].as_posix()}'\n", encoding="utf-8")
    result = _run("-f", "concat", "-safe", "0", *ffmpeg_inputs.ffmpeg_input(listing), "-c", "copy",
                  "-y", str(tmp_path / "joined.mp4"))
    assert result.returncode == 0, result.stderr
    with VideoFileClip(str(tmp_path / "joined.mp4")) as clip:
        assert clip.get_frame(1.5).shape == (64, 64, 3)
        assert round(clip.duration) == 2
    with AudioFileClip(str(media["mp3"])) as audio:
        assert audio.subclipped(0, 0.5).to_soundarray(fps=8000).shape[0] == 4000
    # MoviePy's writer reads frames from a pipe ("-i -") and the audio from a file.
    output = tmp_path / "written.mp4"
    with AudioFileClip(str(media["mp3"])) as audio:
        ColorClip((32, 32), color=(10, 20, 30), duration=0.5).with_audio(audio.subclipped(0, 0.5)).write_videofile(
            str(output), fps=10, logger=None
        )
    assert ffmpeg_parse_infos(str(output))["audio_found"]
