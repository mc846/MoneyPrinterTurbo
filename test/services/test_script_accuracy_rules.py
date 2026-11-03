"""文案科学准确性规则（script_accuracy_rules）。"""

import re
import sys
import tomllib
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from streamlit.testing.v1 import AppTest

ROOT_DIR = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT_DIR))

import cli  # noqa: E402
from app import asgi  # noqa: E402
from app.config import config  # noqa: E402
from app.controllers.v1 import llm as llm_controller  # noqa: E402
from app.models.schema import (  # noqa: E402
    TaskVideoRequest,
    VideoParams,
    VideoScriptRequest,
)
from app.services import llm, voice  # noqa: E402
from app.services import task as tm  # noqa: E402

# 上游 DEFAULT_SCRIPT_SYSTEM_PROMPT 的原文。关闭规则时最终 prompt 必须与它逐字一致。
UPSTREAM_SYSTEM_PROMPT = """# Role: Video Script Generator

## Goals:
Generate a script for a video, depending on the subject of the video.

## Constrains:
1. the script is to be returned as a string with the specified number of paragraphs.
2. do not under any circumstance reference this prompt in your response.
3. get straight to the point, don't start with unnecessary things like, "welcome to this video".
4. you must not include any type of markdown or formatting in the script, never use a title.
5. only return the raw content of the script.
6. do not include "voiceover", "narrator" or similar indicators of what should be spoken at the beginning of each paragraph or line.
7. you must not mention the prompt, or anything about the script itself. also, never talk about the amount of paragraphs or lines. just write the script.
8. respond in the same language as the video subject."""
RULE_1 = (
    "be scientifically accurate: state only well-established facts. If something is an "
    "estimate, a hypothesis or still debated, say so, and never give figures more "
    "precisely or confidently than current research supports."
)
RULE_2 = (
    "avoid popular misconceptions and include every effect that matters when comparing "
    "cases (for example, a clock in orbit is affected by both its speed and gravity; far "
    "from a black hole its gravity equals that of any object with the same mass; "
    "entanglement cannot carry information faster than light)."
)
RULE_3 = (
    "keep metaphors recognisable as metaphors, and do not describe a result or technology "
    'as absolute ("unbreakable", "infinite", "a single point") unless it is.'
)
INITIALIZATION = "\n\n# Initialization:\n- video subject: Kara delikler\n- number of paragraphs: 1\n- language: tr-TR"


def _prompt(**kwargs):
    return llm.build_script_prompt(video_subject="Kara delikler", language="tr-TR", **kwargs)


# ------------------------------------------------------------------ prompt


def test_rules_are_the_requested_text():
    assert llm.SCRIPT_ACCURACY_RULES == (RULE_1, RULE_2, RULE_3)


def test_disabled_prompt_matches_upstream_exactly():
    assert llm.DEFAULT_SCRIPT_SYSTEM_PROMPT == UPSTREAM_SYSTEM_PROMPT
    assert _prompt() == UPSTREAM_SYSTEM_PROMPT + INITIALIZATION
    assert _prompt(script_accuracy_rules=False) == _prompt()


def test_enabled_rules_continue_the_constraint_numbering():
    prompt = _prompt(script_accuracy_rules=True)

    assert prompt == f"{UPSTREAM_SYSTEM_PROMPT}\n9. {RULE_1}\n10. {RULE_2}\n11. {RULE_3}{INITIALIZATION}"
    constraints = prompt.split("## Constrains:\n", 1)[1].split("\n\n# Initialization:", 1)[0]
    assert [int(n) for n in re.findall(r"^(\d+)\. ", constraints, re.M)] == list(range(1, 12))


def test_additional_requirements_stay_after_the_rules():
    prompt = _prompt(script_accuracy_rules=True, video_script_prompt="Yaklaşık 30 saniye.")

    assert prompt.index(f"11. {RULE_3}") < prompt.index("# Initialization:")
    assert prompt.endswith("# Additional User Requirements:\nYaklaşık 30 saniye.")


def test_custom_system_prompt_gets_the_rules_as_a_separate_section():
    custom = "# Role: Narrator\n1. write short sentences."

    assert _prompt(custom_system_prompt=custom) == custom + INITIALIZATION
    assert _prompt(custom_system_prompt=custom, script_accuracy_rules=True) == (
        f"{custom}\n\n## Scientific Accuracy:\n1. {RULE_1}\n2. {RULE_2}\n3. {RULE_3}{INITIALIZATION}"
    )


def test_rules_already_in_a_custom_prompt_are_not_repeated():
    with_all = f"# Role: Narrator\n1. {RULE_1}\n2. {RULE_2.upper()}\n3. {RULE_3}"
    with_one = f"# Role: Narrator\n1. {RULE_2}"

    assert _prompt(custom_system_prompt=with_all, script_accuracy_rules=True) == with_all + INITIALIZATION
    assert _prompt(custom_system_prompt=with_one, script_accuracy_rules=True) == (
        f"{with_one}\n\n## Scientific Accuracy:\n1. {RULE_1}\n2. {RULE_3}{INITIALIZATION}"
    )


@pytest.mark.parametrize("enabled", [False, True])
def test_generate_script_sends_the_built_prompt(enabled):
    prompts = []

    def model(prompt, **kwargs):
        prompts.append(prompt)
        return "Kara delikler, büyük yıldızların çekirdekleri çöktüğünde oluşur."

    with patch.object(llm, "_generate_response", side_effect=model):
        kwargs = {"script_accuracy_rules": True} if enabled else {}
        llm.generate_script("Kara delikler", language="tr-TR", **kwargs)

    assert prompts == [_prompt(script_accuracy_rules=enabled)]
    assert (RULE_1 in prompts[0]) is enabled


# ------------------------------------------------------------------ layers


def test_example_config_documents_the_default():
    app = tomllib.loads((ROOT_DIR / "config.example.toml").read_text(encoding="utf-8"))["app"]

    assert app["script_accuracy_rules"] is False


def test_models_carry_the_setting():
    assert VideoParams(video_subject="t").script_accuracy_rules is False
    assert VideoScriptRequest().script_accuracy_rules is False
    assert TaskVideoRequest.model_validate({"video_subject": "t", "script_accuracy_rules": True}).script_accuracy_rules
    assert VideoScriptRequest.model_validate({"script_accuracy_rules": True}).script_accuracy_rules


@pytest.mark.parametrize(
    ("flags", "expected"),
    [([], False), (["--script-accuracy-rules"], True), (["--no-script-accuracy-rules"], False)],
)
def test_cli_flag_reaches_video_params(flags, expected):
    with patch.dict(config.ui, {}, clear=True):
        params = cli.build_video_params(cli.parse_args(["--video-subject", "t", *flags]))

    assert params.script_accuracy_rules is expected


@pytest.mark.parametrize("enabled", [False, True])
def test_task_forwards_the_rules_only_when_enabled(enabled):
    params = VideoParams(video_subject="Kara delikler", video_language="tr-TR", script_accuracy_rules=enabled)
    with patch.object(tm.llm, "generate_script", return_value="Metin.") as generate:
        assert tm.generate_script("task", params) == "Metin."

    expected = {"video_subject": "Kara delikler", "language": "tr-TR", "paragraph_number": 1,
                "video_script_prompt": "", "custom_system_prompt": ""}
    if enabled:
        expected["script_accuracy_rules"] = True
    generate.assert_called_once_with(**expected)


@pytest.mark.parametrize("enabled", [False, True])
def test_api_forwards_the_rules_only_when_enabled(enabled):
    body = VideoScriptRequest(video_subject="Kara delikler", script_accuracy_rules=enabled)
    with patch.object(llm_controller.llm, "generate_script", return_value="Metin.") as generate:
        assert llm_controller.generate_video_script(None, body)["data"] == {"video_script": "Metin."}

    assert ("script_accuracy_rules" in generate.call_args.kwargs) is enabled


def test_api_request_reaches_the_model_prompt():
    prompts = []
    with (
        patch.dict(config.app, {"api_key": ""}),
        patch.object(llm, "_generate_response", side_effect=lambda prompt, **kw: prompts.append(prompt) or "Metin."),
        TestClient(asgi.get_application()) as client,
    ):
        reply = client.post("/api/v1/scripts", json={"video_subject": "Kara delikler", "script_accuracy_rules": True})

    assert reply.status_code == 200
    assert f"11. {RULE_3}" in prompts[0]


# ------------------------------------------------------------------ WebUI


def _widget(elements, key):
    return next(item for item in elements if str(getattr(item, "key", "")) == key)


@contextmanager
def _webui(app_config):
    ui_config = dict(config.ui, language="en", voice_mode="tts", tts_server="azure-tts-v1",
                     voice_name="en-US-JennyNeural-Female")
    with ExitStack() as stack:
        stack.enter_context(patch.object(config, "app", app_config))
        stack.enter_context(patch.object(config, "ui", ui_config))
        stack.enter_context(patch.object(config, "try_save_config", return_value=True))
        stack.enter_context(
            patch.object(voice, "get_all_azure_voices", return_value=["en-US-JennyNeural-Female"])
        )
        generate = stack.enter_context(patch.object(llm, "generate_script", return_value="Metin."))
        stack.enter_context(patch.object(llm, "generate_terms", return_value=["black hole"]))
        app = AppTest.from_file(str(ROOT_DIR / "webui" / "Main.py"), default_timeout=60)
        app.session_state["ui_language"] = "en"
        app.run()
        assert [str(item.value) for item in app.exception] == []
        yield app, generate


def test_webui_checkbox_follows_config_and_reaches_generation():
    app_config = dict(config.app, llm_provider="openai", script_generation_backend="local",
                      script_accuracy_rules=True)
    with _webui(app_config) as (app, generate):
        assert _widget(app.checkbox, "script_accuracy_rules").value is True
        _widget(app.text_area, "video_subject").set_value("Black holes").run()
        _widget(app.button, "auto_generate_script").click().run()
        assert generate.call_args.kwargs["script_accuracy_rules"] is True

        _widget(app.checkbox, "script_accuracy_rules").uncheck().run()
        assert app_config["script_accuracy_rules"] is False
        _widget(app.button, "auto_generate_script").click().run()
        assert generate.call_args.kwargs["script_accuracy_rules"] is False
