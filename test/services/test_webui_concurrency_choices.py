"""并发数选择：选项包含 3；配置里的选项外正整数原样显示，不被改回 1；无法识别的值降级为 1。"""

from pathlib import Path
from unittest.mock import patch

import pytest
from streamlit.testing.v1 import AppTest

from app.config import config
from app.services import state as sm, version_checker

WEBUI_MAIN = Path(__file__).resolve().parents[2] / "webui" / "Main.py"


def _select(app, key):
    return next(v for v in app.selectbox if v.key == key or str(v.key).startswith(key + "_"))


def _run(tmp_path, **saved):
    app_config = {"video_source": "pixabay", "llm_provider": "openai", **saved}
    patches = (
        patch.object(config, "app", app_config),
        patch.object(config, "ui", {"language": "en", "voice_mode": "none"}),
        patch.object(config, "try_save_config", return_value=True),
        patch.object(sm.state, "get_all_tasks", return_value=([], 0)),
        patch.object(version_checker, "poll_available_update",
                     return_value=version_checker.UpdateCheckSnapshot(complete=True)),
        patch("app.utils.utils.task_dir", return_value=str(tmp_path)),
    )
    for item in patches:
        item.start()
    app = AppTest.from_file(str(WEBUI_MAIN), default_timeout=60)
    app.session_state["ui_language"] = "en"
    app.run()
    assert not app.exception
    return app, app_config, patches


@pytest.fixture
def webui(tmp_path):
    started = []

    def start(**saved):
        app, app_config, patches = _run(tmp_path, **saved)
        started.extend(patches)
        return app, app_config

    yield start
    for item in reversed(started):
        item.stop()


def test_three_is_offered_and_kept(webui):
    app, app_config = webui(material_concurrency=3, video_clip_concurrency=3)
    for key in ("material_concurrency_select", "clip_rendering_concurrency_select"):
        widget = _select(app, key)
        assert widget.options == ["1", "2", "3", "4", "6", "8"]
        assert widget.value == 3
    assert app_config["material_concurrency"] == 3 and app_config["video_clip_concurrency"] == 3


@pytest.mark.parametrize("saved, shown", [(5, 5), ("7", 7), (5.0, 5), (12, 12)])
def test_values_outside_the_list_are_shown_as_they_are(webui, saved, shown):
    app, app_config = webui(material_concurrency=saved, video_clip_concurrency=saved)
    for key in ("material_concurrency_select", "clip_rendering_concurrency_select"):
        widget = _select(app, key)
        assert widget.value == shown
        assert str(shown) in widget.options and "3" in widget.options
    # 写回的是同一个值，不再是 1。
    assert app_config["material_concurrency"] == shown and app_config["video_clip_concurrency"] == shown


@pytest.mark.parametrize("saved", ["abc", True, 0, -2, 2.5])
def test_unreadable_values_still_fall_back_to_one(webui, saved):
    app, app_config = webui(material_concurrency=saved, video_clip_concurrency=saved)
    for key in ("material_concurrency_select", "clip_rendering_concurrency_select"):
        widget = _select(app, key)
        assert widget.value == 1
        assert widget.options == ["1", "2", "3", "4", "6", "8"]
    assert app_config["video_clip_concurrency"] == 1


def test_selecting_three_is_saved(webui):
    app, app_config = webui()
    _select(app, "clip_rendering_concurrency_select").set_value(3).run()
    assert not app.exception
    assert app_config["video_clip_concurrency"] == 3
    _select(app, "material_concurrency_select").set_value(3).run()
    assert app_config["material_concurrency"] == 3
