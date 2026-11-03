"""crop_materials_to_aspect 在配置、VideoParams、API、CLI、任务编排、合成和 WebUI 之间的传递。"""

import sys
import tomllib
from pathlib import Path
from unittest.mock import patch

import pytest
from streamlit.testing.v1 import AppTest

ROOT_DIR = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT_DIR))

import cli  # noqa: E402
from app.config import config  # noqa: E402
from app.models.schema import TaskVideoRequest, VideoFitMode, VideoParams  # noqa: E402
from app.services import task as tm  # noqa: E402
from app.services import video, voice  # noqa: E402
from app.services.state import MemoryState  # noqa: E402


def test_example_config_documents_disabled_default():
    example = tomllib.loads((ROOT_DIR / "config.example.toml").read_text(encoding="utf-8"))

    assert example["app"]["crop_materials_to_aspect"] is False


def test_video_params_and_api_request_carry_the_setting():
    assert VideoParams(video_subject="test").crop_materials_to_aspect is False
    request = TaskVideoRequest.model_validate(
        {"video_subject": "test", "crop_materials_to_aspect": True}
    )
    assert request.crop_materials_to_aspect is True


@pytest.mark.parametrize(
    ("flags", "expected"),
    [([], False), (["--crop-materials-to-aspect"], True), (["--no-crop-materials-to-aspect"], False)],
)
def test_cli_flag_reaches_video_params(flags, expected):
    with patch.dict(config.ui, {}, clear=True):
        args = cli.parse_args(["--video-subject", "test", *flags])
        params = cli.build_video_params(args)

    assert params.crop_materials_to_aspect is expected


@pytest.mark.parametrize(("enabled", "forwarded"), [(True, {"crop_to_aspect": True}), (False, {})])
def test_task_forwards_the_setting_to_material_download_only_when_enabled(enabled, forwarded):
    params = VideoParams(video_subject="test", video_source="pixabay", crop_materials_to_aspect=enabled)
    calls = []

    def fake_download_videos(**kwargs):
        calls.append(kwargs)
        return ["a.mp4"]

    with (
        patch.object(tm.sm, "state", MemoryState()),
        patch.object(tm.material, "download_videos", side_effect=fake_download_videos),
    ):
        assert tm.get_video_materials("crop-forwarding", params, ["scene"], 10) == ["a.mp4"]

    assert {key: calls[0][key] for key in forwarded} == forwarded
    assert enabled or "crop_to_aspect" not in calls[0]


@pytest.mark.parametrize(
    ("source", "enabled", "expected"),
    [("pixabay", True, True), ("pexels", True, True), ("coverr", True, False), ("pixabay", False, False)],
)
def test_task_requests_cover_for_cross_orientation_clips(source, enabled, expected):
    params = VideoParams(
        video_subject="test", video_source=source, video_fit_mode="contain",
        crop_materials_to_aspect=enabled,
    )
    with (
        patch.object(tm.video, "combine_videos") as combine_videos,
        patch.object(tm.video, "generate_video"),
        patch.object(tm.sm, "state", MemoryState()),
    ):
        tm.generate_final_videos(
            task_id="crop-combine", params=params, downloaded_videos=["material.mp4"],
            audio_file="audio.mp3", subtitle_path="", audio_duration=5,
        )

    assert combine_videos.call_args.kwargs.get("crop_cross_orientation_clips", False) is expected


@pytest.mark.parametrize(
    ("clip_size", "target_size", "fit_mode", "enabled", "expected"),
    [
        ((3840, 2160), (1080, 1920), VideoFitMode.contain, True, VideoFitMode.cover),
        ((2160, 3840), (1920, 1080), VideoFitMode.contain, True, VideoFitMode.cover),
        ((2160, 2160), (1080, 1920), VideoFitMode.contain, True, VideoFitMode.cover),
        ((1440, 1920), (1080, 1920), VideoFitMode.contain, True, VideoFitMode.contain),
        ((3840, 2160), (1080, 1920), VideoFitMode.contain, False, VideoFitMode.contain),
        ((3840, 2160), (1080, 1080), VideoFitMode.contain, True, VideoFitMode.contain),
        ((3840, 2160), (1080, 1920), VideoFitMode.cover, True, VideoFitMode.cover),
    ],
)
def test_cross_orientation_clips_are_always_center_cropped(clip_size, target_size, fit_mode, enabled, expected):
    assert video._clip_fit_mode(*clip_size, *target_size, fit_mode, enabled) == expected


def _crop_checkbox(app):
    return next(item for item in app.checkbox if str(getattr(item, "key", "")) == "crop_materials_to_aspect")


def test_webui_checkbox_follows_config_and_saves_changes():
    app_config = dict(config.app, video_source="pixabay", crop_materials_to_aspect=True)
    ui_config = dict(config.ui, language="en", voice_mode="tts", tts_server="azure-tts-v1",
                     voice_name="en-US-JennyNeural-Female")
    with (
        patch.object(config, "app", app_config),
        patch.object(config, "ui", ui_config),
        patch.object(config, "try_save_config", return_value=True),
        patch.object(voice, "get_all_azure_voices", return_value=["en-US-JennyNeural-Female"]),
    ):
        app = AppTest.from_file(str(ROOT_DIR / "webui" / "Main.py"), default_timeout=60)
        app.session_state["ui_language"] = "en"
        app.run()
        assert [str(item.value) for item in app.exception] == []
        assert _crop_checkbox(app).value is True

        _crop_checkbox(app).set_value(False)
        app.run()

    assert app_config["crop_materials_to_aspect"] is False
