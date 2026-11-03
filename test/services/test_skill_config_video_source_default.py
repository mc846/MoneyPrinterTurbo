"""Skill 预检与 cli.py 一致：未转发 --video-source 时沿用 [app].video_source。"""

import importlib.util
import tomllib
from pathlib import Path
from unittest.mock import patch

import pytest

import cli
from app.config import config as app_config

spec = importlib.util.spec_from_file_location("mpt_video_source_fixture", Path(__file__).parents[2] / "docs" / "skill" / "mpt_agent.py")
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)


def _config_text(source_line=""):
    return (
        '[app]\nllm_provider = "moonshot"\nmoonshot_api_key = ""\n'
        f"{source_line}pexels_api_keys = []\npixabay_api_keys = []\n"
    )


@pytest.mark.parametrize(
    ("source_line", "forwarded", "expected"),
    [
        ("", [], "pexels"),
        ('video_source = "pixabay"\n', [], "pixabay"),
        ('video_source = " Pixabay "\n', [], "pixabay"),
        ('video_source = ""\n', [], "pexels"),
        ('video_source = "loomloom"\n', [], "pexels"),
        ("video_source = 123\n", [], "pexels"),
        ('video_source = "pixabay"\n', ["--video-source", "coverr"], "coverr"),
        ('video_source = "pixabay"\n', ["--video-source=local", "--video-materials", "./owned.mp4"], "local"),
    ],
)
def test_skill_source_matches_cli_resolution(source_line, forwarded, expected):
    text = _config_text(source_line)
    assert helper.selected_video_source(forwarded, text) == expected

    with (
        patch.dict(app_config.app, tomllib.loads(text)["app"], clear=True),
        patch.dict(app_config.ui, {}, clear=True),
    ):
        actual = cli.parse_args(["--video-subject", "owned topic", *forwarded])
    assert actual.video_source == expected


def test_missing_config_checks_the_configured_source(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text(
        _config_text('video_source = "pixabay"\n').replace(
            'moonshot_api_key = ""', 'moonshot_api_key = "configured"'
        ),
        encoding="utf-8",
    )

    assert helper.missing_config(config, []) == ("moonshot", ["pixabay_api_keys"])


def test_pexels_validation_is_skipped_when_config_selects_another_source(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text(_config_text('video_source = "pixabay"\n'), encoding="utf-8")

    with patch.object(helper, "_validate_pexels_key", side_effect=AssertionError("no network")):
        assert helper.validate_pexels_config(config, []) is True


def test_local_source_from_config_skips_llm_for_prepared_script(tmp_path):
    # 本地素材不生成搜索词；脚本已提供时无需 LLM Key，与 cli.py 实际流程一致。
    config = tmp_path / "config.toml"
    config.write_text(_config_text('video_source = "local"\n'), encoding="utf-8")

    cli_args = ["--video-script", "prepared", "--video-materials", "./owned.mp4"]
    assert helper.missing_config(config, cli_args) == ("moonshot", [])
