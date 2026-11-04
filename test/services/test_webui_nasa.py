"""WebUI 中的 NASA 素材源：来源列表、设置窗口、生成前检查和语言文件。"""

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from streamlit.testing.v1 import AppTest

from app.config import config
from app.services import nasa
from webui import settings_providers

ROOT_DIR = Path(__file__).parent.parent.parent
WEBUI_MAIN = ROOT_DIR / "webui" / "Main.py"
I18N_DIR = ROOT_DIR / "webui" / "i18n"
NASA_KEYS = (
    "NASA Images (no key)", "NASA Source Info", "NASA Face Filter", "NASA Face Filter Help",
    "NASA Face Filter Missing Extra", "NASA Face Filter Unavailable", "NASA Credit Overlay",
    "NASA Credit Overlay Help", "NASA Usage Terms Note", "NASA Usage Guidelines",
)


def _widget_by_key(elements, key):
    return next(
        item
        for item in elements
        if str(getattr(item, "key", "")) == key
        or str(getattr(item, "key", "")).startswith(f"{key}_")
    )


def _app_config(**overrides):
    # 没有任何素材 API Key：NASA 不需要
    return dict(config.app, llm_provider="openai", video_source="pexels", pexels_api_keys=[],
                pixabay_api_keys=[], coverr_api_keys=[], **overrides)


def _submit_with_nasa(app_config, face_extra=True):
    with (
        patch.object(config, "app", app_config),
        patch.object(config, "ui", dict(config.ui, voice_mode="none")),
        patch.object(config, "try_save_config", return_value=True),
        patch.object(nasa, "face_extra_installed", return_value=face_extra),
        patch("app.services.webui_task.submit_generation") as submit_generation,
    ):
        app = AppTest.from_file(str(WEBUI_MAIN), default_timeout=60)
        app.session_state["ui_language"] = "en"
        app.run()
        _widget_by_key(app.text_area, "video_subject").set_value("Galaxies").run()
        _widget_by_key(app.text_area, "video_script").set_value("Galaxies are cities of stars.").run()
        _widget_by_key(app.text_area, "video_terms").set_value("spiral galaxy, nebula cloud").run()
        app.session_state["video_source_select_en"] = "nasa"
        app.run()
        _widget_by_key(app.button, "generate_video_button").click().run()
        return app, submit_generation


@pytest.mark.parametrize("overlay", [False, True])
def test_nasa_source_submits_without_any_api_key(overlay):
    app, submit_generation = _submit_with_nasa(_app_config(nasa_credit_overlay=overlay))

    assert [str(item.value) for item in app.exception] == []
    assert [str(item.value) for item in app.error] == []
    assert submit_generation.call_count == 1
    params = submit_generation.call_args.kwargs["params"]
    assert params.video_source == "nasa"
    assert params.nasa_credit_overlay is overlay


def test_missing_face_extra_is_reported_but_does_not_block():
    app, submit_generation = _submit_with_nasa(_app_config(), face_extra=False)

    assert any("Face detection for NASA images is not installed" in str(item.value) for item in app.info)
    assert submit_generation.call_count == 1


def test_face_extra_message_is_skipped_when_the_filter_is_off():
    app, _ = _submit_with_nasa(_app_config(nasa_face_filter=False), face_extra=False)

    assert not any("Face detection" in str(item.value) for item in app.info)


@pytest.mark.parametrize("face_extra", [True, False])
def test_nasa_settings_dialog_has_switches_and_terms_link(face_extra):
    settings = _app_config()
    with (
        patch.object(config, "app", settings),
        patch.object(config, "ui", dict(config.ui, voice_mode="none", language="en")),
        patch.object(config, "try_save_config", return_value=True),
        patch.object(config, "save_config"),
        patch.object(nasa, "face_extra_installed", return_value=face_extra),
    ):
        app = AppTest.from_file(str(WEBUI_MAIN), default_timeout=60)
        app.session_state["ui_language"] = "en"
        app.session_state["settings_dialog_open"] = True
        app.session_state["settings_dialog_target_tab"] = "video"
        app.session_state["settings_material_providers"] = {"stock_video": "nasa"}
        app.run()

        assert [str(item.value) for item in app.exception] == []
        assert app.checkbox(key="nasa_face_filter_input").value is True
        assert app.checkbox(key="nasa_credit_overlay_input").value is False
        # NASA 不需要 API Key 输入框
        assert not any("nasa_api_keys" in str(item.key) for item in app.text_input)
        assert any(nasa.USAGE_GUIDELINES_URL in str(item.value) for item in app.markdown)
        assert any("uv sync --extra face" in str(item.value) for item in app.warning) is (not face_extra)

        app.checkbox(key="nasa_credit_overlay_input").check().run()
        app.checkbox(key="nasa_face_filter_input").uncheck().run()
        assert config.snapshot_config_with_pending(config.app)["nasa_credit_overlay"] is True
        assert config.snapshot_config_with_pending(config.app)["nasa_face_filter"] is False


def test_nasa_counts_as_configured_without_keys():
    groups = {"stock_video": ("pexels", "pixabay", "coverr", "nasa")}

    assert settings_providers.configured_material_providers({}, groups)["stock_video"] == ("nasa",)


@pytest.mark.parametrize("language", sorted(file.stem for file in I18N_DIR.glob("*.json")))
def test_every_language_has_the_nasa_texts(language):
    messages = json.loads((I18N_DIR / f"{language}.json").read_text(encoding="utf-8"))["Translation"]

    assert all(messages.get(key, "").strip() for key in NASA_KEYS)
    assert "uv sync --extra face" in messages["NASA Face Filter Missing Extra"]
    assert "NASA" in messages["NASA Images (no key)"]
