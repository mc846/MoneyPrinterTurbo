"""成片合成方式选择：默认 MoviePy，保存的 ffmpeg 原样显示，选择后写入 config.app，切换语言后保留。"""

from pathlib import Path
from unittest.mock import patch

import pytest
from streamlit.testing.v1 import AppTest

from app.config import config
from app.services import state as sm, version_checker

WEBUI_MAIN = Path(__file__).resolve().parents[2] / "webui" / "Main.py"


def _select(app, key):
    return next(v for v in app.selectbox if v.key == key or str(v.key).startswith(key + "_"))


@pytest.fixture
def webui(tmp_path):
    started = []

    def start(**saved):
        app_config = {"video_source": "pixabay", "llm_provider": "openai", **saved}
        for item in (
            patch.object(config, "app", app_config),
            patch.object(config, "ui", {"language": "en", "voice_mode": "none"}),
            patch.object(config, "try_save_config", return_value=True),
            patch.object(sm.state, "get_all_tasks", return_value=([], 0)),
            patch.object(version_checker, "poll_available_update",
                         return_value=version_checker.UpdateCheckSnapshot(complete=True)),
            patch("app.utils.utils.task_dir", return_value=str(tmp_path)),
        ):
            item.start()
            started.append(item)
        app = AppTest.from_file(str(WEBUI_MAIN), default_timeout=60)
        app.session_state["ui_language"] = "en"
        app.run()
        assert not app.exception
        return app, app_config

    yield start
    for item in reversed(started):
        item.stop()


def test_default_is_moviepy_and_ffmpeg_can_be_selected(webui):
    app, app_config = webui()
    widget = _select(app, "final_render_engine_select")
    assert widget.label == "Final Video Rendering"
    assert widget.options == ["MoviePy (default)", "FFmpeg (faster)"]
    assert widget.value == "moviepy"
    widget.set_value("ffmpeg").run()
    assert not app.exception
    assert app_config["final_render_engine"] == "ffmpeg"
    _select(app, "top_language_code_selector").select("tr").run()
    assert not app.exception
    widget = _select(app, "final_render_engine_select")
    assert widget.label == "Son video oluşturma" and widget.value == "ffmpeg"


@pytest.mark.parametrize("saved, shown", [("ffmpeg", "ffmpeg"), ("moviepy", "moviepy"), ("gpu", "moviepy")])
def test_saved_value_is_restored(webui, saved, shown):
    app, app_config = webui(final_render_engine=saved)
    assert _select(app, "final_render_engine_select").value == shown
    assert app_config["final_render_engine"] == shown
