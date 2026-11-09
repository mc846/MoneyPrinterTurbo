"""Startup log of the ffmpeg in use: path, where it came from and its version line, once per process."""

import importlib.util
import runpy
import subprocess
import sys
import threading
from pathlib import Path
from unittest.mock import patch

import pytest
from loguru import logger
from streamlit.testing.v1 import AppTest

import cli
from app.config import config
from app.utils import utils

ROOT = Path(__file__).resolve().parents[2]
VERSION_OUTPUT = (
    "ffmpeg version 9.0.2-essentials_build-www.gyan.dev Copyright (c) 2000-2026 the FFmpeg developers\n"
    "built with gcc 15.2.0 (Rev8, Built by MSYS2 project)\n"
)


@pytest.fixture
def records(monkeypatch):
    """Collect log messages and reset the once-per-process state."""
    monkeypatch.setitem(sys.__dict__, utils._FFMPEG_VERSION_LOG_STATE, {"lock": threading.Lock(), "logged": False})
    messages = []
    sink = logger.add(lambda message: messages.append(message.record), level="DEBUG")
    yield messages
    logger.remove(sink)


def _ffmpeg_lines(messages):
    return [(r["level"].name, r["message"]) for r in messages if "using ffmpeg" in r["message"]]


def _version_run(command, **kwargs):
    assert command[1:] == ["-version"]
    assert kwargs["stdin"] is subprocess.DEVNULL
    return subprocess.CompletedProcess(command, 0, VERSION_OUTPUT, "")


def test_logs_path_source_and_version_line_once(records, monkeypatch, tmp_path):
    binary = str(tmp_path / "ffmpeg.exe")
    monkeypatch.setenv("IMAGEIO_FFMPEG_EXE", binary)
    with (
        patch.dict(config.app, {"ffmpeg_path": ""}),
        patch.object(utils.subprocess, "run", side_effect=_version_run) as run,
    ):
        utils.log_ffmpeg_version()
        utils.log_ffmpeg_version()
    assert run.call_count == 1
    assert _ffmpeg_lines(records) == [
        (
            "INFO",
            f"using ffmpeg {binary} (set by IMAGEIO_FFMPEG_EXE): "
            "ffmpeg version 9.0.2-essentials_build-www.gyan.dev",
        )
    ]


def test_a_reimported_module_does_not_log_again(records, monkeypatch, tmp_path):
    """Streamlit's source watcher re-imports app modules; the process still logs once."""
    monkeypatch.setenv("IMAGEIO_FFMPEG_EXE", str(tmp_path / "ffmpeg.exe"))
    spec = importlib.util.spec_from_file_location("reimported_utils", utils.__file__)
    reimported = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(reimported)
    with (
        patch.dict(config.app, {"ffmpeg_path": ""}),
        patch.object(subprocess, "run", side_effect=_version_run) as run,
    ):
        utils.log_ffmpeg_version()
        reimported.log_ffmpeg_version()
    assert run.call_count == 1
    assert len(_ffmpeg_lines(records)) == 1


def test_names_app_ffmpeg_path_when_config_set_the_variable(records, monkeypatch, tmp_path):
    binary = tmp_path / "ffmpeg.exe"
    monkeypatch.setenv("IMAGEIO_FFMPEG_EXE", str(binary))
    with (
        patch.dict(config.app, {"ffmpeg_path": str(binary)}),
        patch.object(utils.subprocess, "run", side_effect=_version_run),
    ):
        utils.log_ffmpeg_version()
    assert "(set by app.ffmpeg_path)" in _ffmpeg_lines(records)[0][1]


@pytest.mark.parametrize(
    "on_path, bundled, expected",
    [
        ("/usr/bin/ffmpeg", "/bundled/ffmpeg", "found on PATH"),
        (None, "/bundled/ffmpeg", "bundled with imageio-ffmpeg"),
    ],
)
def test_names_path_and_bundled_binaries(records, monkeypatch, on_path, bundled, expected):
    import imageio_ffmpeg

    monkeypatch.delenv("IMAGEIO_FFMPEG_EXE", raising=False)
    with (
        patch.object(utils.shutil, "which", return_value=on_path),
        patch.object(imageio_ffmpeg, "get_ffmpeg_exe", return_value=bundled),
        patch.object(utils.subprocess, "run", side_effect=_version_run),
    ):
        utils.log_ffmpeg_version()
    assert _ffmpeg_lines(records)[0][1] == (
        f"using ffmpeg {on_path or bundled} ({expected}): "
        "ffmpeg version 9.0.2-essentials_build-www.gyan.dev"
    )


def test_unreadable_version_is_a_warning_with_the_source(records, monkeypatch, tmp_path):
    binary = str(tmp_path / "missing" / "ffmpeg.exe")
    monkeypatch.setenv("IMAGEIO_FFMPEG_EXE", binary)
    with patch.dict(config.app, {"ffmpeg_path": ""}):
        utils.log_ffmpeg_version()
    [(level, message)] = _ffmpeg_lines(records)
    assert level == "WARNING"
    assert message.startswith(f"using ffmpeg {binary} (set by IMAGEIO_FFMPEG_EXE), but its version could not be read")


def test_real_binary_version_line(records):
    """The ffmpeg this test run uses answers -version with a parseable first line."""
    utils.log_ffmpeg_version()
    [(level, message)] = _ffmpeg_lines(records)
    assert level == "INFO"
    assert ": ffmpeg version " in message and "Copyright" not in message


def test_cli_logs_ffmpeg_at_startup():
    with (
        patch.object(utils, "log_ffmpeg_version") as log_version,
        patch("app.services.task.start", return_value={"script": "ok"}),
        patch("builtins.print"),
    ):
        assert cli.run_cli(["--video-subject", "ffmpeg", "--stop-at", "script"]) == 0
    log_version.assert_called_once_with()


def test_api_server_logs_ffmpeg_at_startup():
    with patch.object(utils, "log_ffmpeg_version") as log_version, patch("uvicorn.run") as run_server:
        runpy.run_path(str(ROOT / "main.py"), run_name="__main__")
    log_version.assert_called_once_with()
    run_server.assert_called_once()


def test_webui_logs_ffmpeg_on_load():
    with (
        patch.object(utils, "log_ffmpeg_version") as log_version,
        patch.object(config, "try_save_config", return_value=True),
    ):
        app = AppTest.from_file(str(ROOT / "webui/Main.py"), default_timeout=60).run()
        assert not app.exception
    log_version.assert_called()
