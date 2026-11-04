"""CLI 的 --llm-provider / --llm-model：只影响本次运行的内存配置，不写回 config.toml。"""

import importlib.util
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT_DIR = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT_DIR))

import cli  # noqa: E402
from app.config import config  # noqa: E402

spec = importlib.util.spec_from_file_location("mpt_agent_llm_override", ROOT_DIR / "docs" / "skill" / "mpt_agent.py")
agent = importlib.util.module_from_spec(spec)
spec.loader.exec_module(agent)


def _config_bytes():
    return Path(config.config_file).read_bytes() if Path(config.config_file).exists() else b""


@pytest.mark.parametrize(
    ("flags", "provider", "model_key", "model"),
    [
        (["--llm-provider", "ollama", "--llm-model", "gpt-oss:120b-cloud"], "ollama",
         "ollama_model_name", "gpt-oss:120b-cloud"),
        (["--llm-provider", "Claude_Code", "--llm-model", "claude-opus-5-5"], "claude_code",
         "claude_code_model_name", "claude-opus-5-5"),
        (["--llm-provider", "deepseek"], "deepseek", None, None),
    ],
)
def test_overrides_apply_to_the_selected_provider_in_memory(flags, provider, model_key, model):
    before = _config_bytes()
    with patch.dict(config.app, {"llm_provider": "moonshot", "ollama_model_name": "llama3"}):
        cli.apply_llm_overrides(cli.parse_args(["--video-subject", "t", *flags]))
        assert config.app["llm_provider"] == provider
        if model_key:
            assert config.app[model_key] == model
        # 其它 Provider 的模型字段保持不变
        if provider != "ollama":
            assert config.app["ollama_model_name"] == "llama3"

    assert _config_bytes() == before


def test_model_flag_alone_uses_the_configured_provider():
    with patch.dict(config.app, {"llm_provider": "ollama"}):
        cli.apply_llm_overrides(cli.parse_args(["--video-subject", "t", "--llm-model", "qwen3:8b"]))
        assert config.app["llm_provider"] == "ollama"
        assert config.app["ollama_model_name"] == "qwen3:8b"


def test_without_flags_nothing_changes():
    with patch.dict(config.app, {"llm_provider": "ollama"}):
        snapshot = dict(config.app)
        cli.apply_llm_overrides(cli.parse_args(["--video-subject", "t"]))
        assert dict(config.app) == snapshot


def test_unknown_provider_is_rejected_with_the_valid_names(caplog):
    with patch.dict(config.app, {"llm_provider": "ollama"}), patch.object(cli.logger, "error") as error:
        code = cli.run_cli(["--video-subject", "t", "--llm-provider", "not-a-provider", "--stop-at", "script"])
        assert config.app["llm_provider"] == "ollama"

    assert code == 2
    message = error.call_args.args[0]
    assert "unknown --llm-provider 'not-a-provider'" in message
    assert "claude_code" in message and "ollama" in message


def test_run_uses_the_override_and_leaves_config_toml_untouched():
    before = _config_bytes()
    seen = {}

    def fake_start(task_id, params, stop_at, **kwargs):
        seen.update(provider=config.app["llm_provider"], model=config.app.get("ollama_model_name"))
        return {"state": 1, "script": "Metin."}

    with (
        patch.dict(config.app, {"llm_provider": "claude_code"}),
        patch("app.services.task.start", side_effect=fake_start),
    ):
        code = cli.run_cli(["--video-subject", "Zombi yıldızlar", "--stop-at", "script",
                            "--llm-provider", "ollama", "--llm-model", "gpt-oss:120b-cloud"])

    assert code == 0
    assert seen == {"provider": "ollama", "model": "gpt-oss:120b-cloud"}
    assert _config_bytes() == before


def test_help_lists_the_llm_flags(capsys):
    with pytest.raises(SystemExit):
        cli.parse_args(["--help"])

    text = capsys.readouterr().out
    assert "--llm-provider" in text and "--llm-model" in text and "never writes config.toml" in text


def test_agent_checks_the_provider_given_on_the_command_line(tmp_path):
    config_file = tmp_path / "config.toml"
    config_file.write_text('[app]\nllm_provider = "deepseek"\ndeepseek_api_key = ""\n', encoding="utf-8")
    # 本地素材：只检查 LLM 相关字段
    args = ["--video-subject", "t", "--video-source=local", "--video-materials=owned.mp4"]

    assert agent.missing_config(config_file, args) == ("deepseek", ["deepseek_api_key"])
    assert agent.missing_config(config_file, [*args, "--llm-provider", "claude_code"]) == ("claude_code", [])
    assert agent.missing_config(config_file, [*args, "--llm-provider=OLLAMA"]) == ("ollama", [])


def test_agent_accepts_a_model_flag_for_the_custom_openai_provider(tmp_path):
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        '[app]\nllm_provider = "oneapi"\noneapi_api_key = "fixture"\noneapi_base_url = "https://gw.example/v1"\n',
        encoding="utf-8",
    )
    args = ["--video-subject", "t", "--video-source=local", "--video-materials=owned.mp4"]

    assert agent.missing_config(config_file, args) == ("oneapi", ["oneapi_model_name"])
    assert agent.missing_config(config_file, [*args, "--llm-model", "gw-model"]) == ("oneapi", [])
