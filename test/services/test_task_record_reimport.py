"""The WebUI keeps following a task when Streamlit re-imports the application modules.

Streamlit's source watcher removes the application's modules from sys.modules when it
reports a changed file, and the next page run imports them again. The in-memory task
store and the record of running generations used to be module globals, so a finished
video was then reported as "task record unavailable". On Windows the watcher also
reported files that were only read (app/controllers was a namespace package).
"""

import importlib.util
import json
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from streamlit.testing.v1 import AppTest

from app.config import config
from app.controllers.manager.memory_manager import InMemoryTaskManager
from app.models import const
from app.models.schema import VideoParams
from app.services import state as sm
from app.services import webui_task
from app.utils import logging_utils, utils
from app.utils.process_objects import process_object

ROOT = Path(__file__).resolve().parents[2]
ENTRY = ROOT / "webui" / "Main.py"


def _translations(key):
    return {
        json.loads((ROOT / "webui" / "i18n" / f"{language}.json").read_text(encoding="utf-8"))[
            "Translation"
        ][key]
        for language in ("en", "zh")
    }


PAID_WARNINGS = _translations("Generation Task Unavailable")
FREE_WARNING_STARTS = {
    text.split("`{folder}`")[0] for text in _translations("Generation Task Unavailable Free Sources")
}
COMPLETED = _translations("Video Generation Completed")


def _unavailable_warnings(app):
    return [
        warning.value
        for warning in app.warning
        if warning.value in PAID_WARNINGS
        or any(warning.value.startswith(start) for start in FREE_WARNING_STARTS)
    ]


def _fresh_copy(module):
    """Execute a module's file again under another name, as a re-import would."""
    name = f"_reimported_{module.__name__.replace('.', '_')}_{uuid4().hex}"
    spec = importlib.util.spec_from_file_location(name, module.__file__)
    copy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(copy)
    return copy


def _application_modules():
    return {
        name: module
        for name, module in sys.modules.items()
        if name in {"app", "webui"} or name.startswith(("app.", "webui."))
    }


def test_process_object_is_created_once_per_process():
    name = f"test.process_object.{uuid4().hex}"
    calls = []

    def factory():
        calls.append(1)
        return object()

    first = process_object(name, factory)
    assert process_object(name, factory) is first
    assert calls == [1]


def test_a_reimported_module_shares_the_task_store_and_running_generations():
    state_copy = _fresh_copy(sm)
    task_copy = _fresh_copy(webui_task)

    assert state_copy.state is sm.state
    for name in (
        "_task_manager",
        "_task_logs",
        "_task_logs_lock",
        "_submission_lock",
        "_active_submissions",
    ):
        assert getattr(task_copy, name) is getattr(webui_task, name), name


def test_a_reimported_logging_module_replaces_the_same_terminal_handler():
    copy = _fresh_copy(logging_utils)
    previous = logging_utils._terminal_handler["id"]
    try:
        with (
            patch.object(logging_utils.logger, "remove") as remove,
            patch.object(logging_utils.logger, "add", return_value=456),
        ):
            logging_utils._terminal_handler["id"] = 123
            copy.configure_terminal_logger(sink=object(), level="DEBUG", colorize=False)

        # The copy removed the handler the first module added instead of adding a second one.
        remove.assert_called_once_with(123)
        assert logging_utils._terminal_handler["id"] == 456
    finally:
        logging_utils._terminal_handler["id"] = previous


def test_namespace_packages_hold_no_module_subfolders():
    """Streamlit watches a namespace package as a folder and compares an event for a file
    in its subfolders with the folder's own timestamp and listing. Those never match, so
    on Windows reading such a file (its last-access time changes) re-imported every
    module. A regular package is watched file by file."""
    offenders = []
    for top in ("app", "webui"):
        for folder in (ROOT / top).rglob("*"):
            if (
                not folder.is_dir()
                or "__pycache__" in folder.parts
                or (folder / "__init__.py").exists()
            ):
                continue
            nested = [
                path
                for path in folder.rglob("*.py")
                if path.parent != folder and "__pycache__" not in path.parts
            ]
            if nested:
                offenders.append(folder.relative_to(ROOT).as_posix())
    assert offenders == []


def test_page_follows_a_running_task_after_the_modules_are_reimported(tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    job = str(uuid4())
    video = tmp_path / "final-1.mp4"
    video.write_bytes(b"placeholder")

    def generate(task_id, **kwargs):
        sm.state.update_task(task_id, state=const.TASK_STATE_PROCESSING, progress=10)
        entered.set()
        assert release.wait(60)
        sm.state.update_task(
            task_id, state=const.TASK_STATE_COMPLETE, progress=100, videos=[str(video)]
        )

    monkeypatch.setattr(webui_task.tm, "start", generate)
    saved = _application_modules()
    try:
        params = VideoParams(video_subject="re-import", video_source="pixabay")
        assert webui_task.submit_generation(job, params, capture_logs=False) == job
        assert entered.wait(10)
        # What Streamlit's watcher does before the next page run.
        for name in saved:
            sys.modules.pop(name, None)

        app = AppTest.from_file(str(ENTRY), default_timeout=90)
        app.query_params["generation_task"] = job
        app.run()
        assert not app.exception
        assert sys.modules["app.services.state"] is not sm  # the page imported fresh modules
        assert not _unavailable_warnings(app)
        assert app.button(key="generate_video_button").disabled

        release.set()
        deadline = time.monotonic() + 30
        while webui_task.is_generation_active(job) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert sm.state.get_task(job)["state"] == const.TASK_STATE_COMPLETE
        app.run()
        assert not app.exception
        assert not _unavailable_warnings(app)
        assert any(success.value in COMPLETED for success in app.success)
        assert not app.button(key="generate_video_button").disabled
    finally:
        release.set()
        for name in [name for name in sys.modules if name in {"app", "webui"} or name.startswith(("app.", "webui."))]:
            del sys.modules[name]
        sys.modules.update(saved)
        sm.state.delete_task(job)


def _unavailable_page(tmp_path, monkeypatch, video_source):
    """Open the page for a task this process has no record of, e.g. after a restart."""
    monkeypatch.setattr(sm, "state", sm.MemoryState())
    monkeypatch.setattr(webui_task, "_active_submissions", {})
    monkeypatch.setattr(webui_task, "_task_manager", InMemoryTaskManager(1, 10))
    monkeypatch.setattr(utils, "task_dir", lambda *_a, **_k: str(tmp_path))
    job = str(uuid4())
    if video_source is not None:
        (tmp_path / job).mkdir()
        (tmp_path / job / "script.json").write_text(
            json.dumps({"params": {"video_source": video_source}}), encoding="utf-8"
        )
    with patch.object(config, "try_save_config", return_value=True):
        app = AppTest.from_file(str(ENTRY), default_timeout=60)
        app.query_params["generation_task"] = job
        app.run()
    assert not app.exception
    return job, _unavailable_warnings(app)


def test_unavailable_task_with_free_sources_points_to_its_folder(tmp_path, monkeypatch):
    job, warnings = _unavailable_page(tmp_path, monkeypatch, "pixabay")
    assert len(warnings) == 1
    assert warnings[0] not in PAID_WARNINGS
    assert str(tmp_path / job) in warnings[0]


def test_unavailable_task_with_a_paid_or_unknown_source_keeps_the_charge_warning(tmp_path, monkeypatch):
    for video_source in ("muapi", None):
        _job, warnings = _unavailable_page(tmp_path, monkeypatch, video_source)
        assert len(warnings) == 1, video_source
        assert warnings[0] in PAID_WARNINGS, video_source
