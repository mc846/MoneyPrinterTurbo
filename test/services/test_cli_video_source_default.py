"""--video-source 未显式指定时沿用 config.toml 的 [app].video_source。"""

import io
import sys
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import cli  # noqa: E402
from app.config import config as app_config  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_ui_config():
    # build_video_params 同样读取 [ui]；与 test_cli 一致，隔离开发机的保存值。
    with patch.dict(app_config.ui, {}, clear=True):
        yield


def _app_config(**values):
    app = {key: value for key, value in app_config.app.items() if key != "video_source"}
    app.update(values)
    return patch.dict(app_config.app, app, clear=True)


def test_explicit_option_wins_over_config():
    with _app_config(video_source="pixabay"):
        args = cli.parse_args(["--video-subject", "test", "--video-source", "coverr"])
        params = cli.build_video_params(args)

    assert args.video_source == "coverr"
    assert params.video_source == "coverr"


def test_config_video_source_is_used_when_option_is_omitted():
    with _app_config(video_source="pixabay"):
        args = cli.parse_args(["--video-subject", "test"])
        params = cli.build_video_params(args)

    assert args.video_source == "pixabay"
    assert params.video_source == "pixabay"


def test_config_value_is_normalized_like_the_option():
    with _app_config(video_source=" Pixabay "):
        args = cli.parse_args(["--video-subject", "test"])

    assert args.video_source == "pixabay"


def test_pexels_is_used_when_config_has_no_video_source():
    with _app_config():
        args = cli.parse_args(["--video-subject", "test"])
        params = cli.build_video_params(args)

    assert args.video_source == "pexels"
    assert params.video_source == "pexels"


@pytest.mark.parametrize("saved", ["", "  ", "loomloom", "unknown", 123, None])
def test_unusable_config_value_falls_back_to_pexels(saved):
    # loomloom 只在 WebUI 中可用，CLI 不能接收它，因此同样回退到默认值。
    with _app_config(video_source=saved):
        args = cli.parse_args(["--video-subject", "test"])

    assert args.video_source == "pexels"


def test_paid_source_from_config_still_requires_charge_confirmation():
    # 来源取自配置时，付费确认仍须在参数校验阶段生效，不能被悄悄跳过。
    with _app_config(video_source="wavespeed"):
        with pytest.raises(SystemExit) as raised:
            cli.parse_args(["--video-subject", "test"])
        assert raised.value.code == 2

        args = cli.parse_args(
            ["--video-subject", "test", "--confirm-wavespeed-charge"]
        )

    assert args.video_source == "wavespeed"


def test_help_documents_config_default():
    output = io.StringIO()
    with redirect_stdout(output), pytest.raises(SystemExit):
        cli.parse_args(["--help"])

    help_text = " ".join(output.getvalue().split())
    assert "[app].video_source from config.toml; pexels when unset" in help_text
