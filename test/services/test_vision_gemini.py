"""The Gemini backend of the visual screening (vision_screen.ask_gemini): the request (images, prompt,
JSON schema, thinking), the model name, the spacing of requests, 429 and 5xx retries, daily and zero
quotas, other errors, and that the API key never reaches a log, an error, the cache or a record.
A fake google-genai client; no network."""
# The images fixture comes from test_vision_screen; tests name it as an argument.
# ruff: noqa: F811

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from google import genai
from google.genai import errors

ROOT_DIR = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT_DIR))

from app.config import config  # noqa: E402
from app.services import batch_production, vision_screen  # noqa: E402
from test.services.test_vision_screen import _item, answer, images  # noqa: E402,F401  (fixture)

KEY = "AIzaSyTEST-not-a-real-key-0123456789abc"


@pytest.fixture
def gemini(monkeypatch):
    """A fake genai.Client answering from ``outcomes`` (text or an exception); sleeps are recorded."""
    calls, outcomes, sleeps = [], [], []

    class Models:
        def generate_content(self, model, contents, config):
            calls.append({"model": model, "contents": contents, "config": config})
            outcome = outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return SimpleNamespace(text=outcome, usage_metadata=None)

    class Client:
        def __init__(self, api_key=None, http_options=None):
            calls.append({"api_key": api_key, "timeout": http_options.timeout})
            self.models = Models()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(genai, "Client", Client)
    monkeypatch.setattr(vision_screen, "_sleep", sleeps.append)
    monkeypatch.setattr(vision_screen, "_gemini_next", 0.0)
    monkeypatch.setitem(config.app, "gemini_api_key", KEY)
    monkeypatch.setitem(config.app, "material_vision_gemini_rpm", 600)  # 0.1 s apart
    return SimpleNamespace(calls=calls, outcomes=outcomes, sleeps=sleeps)


def _error(code, status, message="error", details=()):
    return errors.ClientError(code, {"error": {"code": code, "message": message, "status": status,
                                               "details": list(details)}}) if code < 500 else \
        errors.ServerError(code, {"error": {"code": code, "message": message, "status": status}})


def _quota(quota_id, value, delay=None):
    details = [{"@type": "type.googleapis.com/google.rpc.QuotaFailure",
                "violations": [{"quotaMetric": "generativelanguage.googleapis.com/generate_content_free_tier_requests",
                                "quotaId": quota_id, "quotaValue": str(value)}]}]
    if delay is not None:
        details.append({"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": f"{delay}s"})
    return _error(429, "RESOURCE_EXHAUSTED", "You exceeded your current quota", details)


PER_MINUTE = "GenerateRequestsPerMinutePerProjectPerModel-FreeTier"
PER_DAY = "GenerateRequestsPerDayPerProjectPerModel-FreeTier"


# ---------------------------------------------------------------- model name


@pytest.mark.parametrize(("value", "expected"), [
    ("gemini:gemini-3.8-flash", ("gemini", "gemini-3.8-flash")),
    (" Gemini : models/gemini-3.6-flash ", ("gemini", "gemini-3.6-flash")),
    ("gemini:", ("gemini", vision_screen.DEFAULT_GEMINI_MODEL)),
    ("gemini", ("gemini", vision_screen.DEFAULT_GEMINI_MODEL)),
    ("ollama:gemma4:31b-cloud", ("ollama", "gemma4:31b-cloud")),
    ("claude:sonnet", ("claude", "sonnet")),
    ("", ("claude", "sonnet")),
    ("vertex:gemini-3.8-flash", ("claude", "sonnet")),
])
def test_the_model_name(value, expected):
    assert vision_screen.parse_model(value) == expected


def test_the_setting_and_the_session_take_a_gemini_model():
    with patch.dict(config.app, {"material_vision_screening": True, "material_vision_model": "gemini:gemini-3.8-flash"}):
        assert vision_screen.model_setting() == ("gemini", "gemini-3.8-flash")
        session = vision_screen.start(["moon"], "Ay")
    assert (session.backend, session.model) == ("gemini", "gemini-3.8-flash")
    assert batch_production.APP_OPTIONS["material_vision_gemini_rpm"] is int


# ---------------------------------------------------------------- the request


def test_three_frames_the_prompt_and_the_schema_in_one_request(gemini):
    gemini.outcomes.append(answer(image_type="cgi"))
    text = vision_screen.ask_gemini(["YQ==", "Yg==", "Yw=="], "the prompt", "gemini-3.8-flash")  # b"a", b"b", b"c"
    assert vision_screen.parse(text)["image_type"] == "cgi"
    client, request = gemini.calls
    assert client == {"api_key": KEY, "timeout": vision_screen.CALL_TIMEOUT * 1000}
    assert request["model"] == "gemini-3.8-flash"
    parts = request["contents"][0].parts
    assert [part.inline_data.mime_type for part in parts[:3]] == ["image/jpeg"] * 3
    assert [part.inline_data.data for part in parts[:3]] == [b"a", b"b", b"c"] and parts[3].text == "the prompt"
    settings = request["config"]
    assert settings.system_instruction == vision_screen.SYSTEM
    assert settings.response_mime_type == "application/json"
    assert settings.response_json_schema == vision_screen.RESPONSE_SCHEMA
    assert settings.thinking_config.thinking_level.value == "LOW"


def test_older_models_keep_their_own_thinking(gemini):
    gemini.outcomes.append(answer())
    vision_screen.ask_gemini(["YQ=="], "p", "gemini-2.5-flash")
    assert gemini.calls[1]["config"].thinking_config is None


def test_the_schema_matches_the_parser():
    schema = vision_screen.RESPONSE_SCHEMA
    assert schema["properties"]["image_type"]["enum"] == list(vision_screen.IMAGE_TYPES)
    assert schema["properties"]["person"]["enum"] == list(vision_screen.PERSON_VALUES)
    assert set(schema["required"]) == set(json.loads(answer()))


# ---------------------------------------------------------------- spacing and rate limits


def test_requests_are_spaced_by_the_rpm_across_calls(monkeypatch):
    sleeps = []
    monkeypatch.setattr(vision_screen, "_sleep", sleeps.append)
    monkeypatch.setattr(vision_screen, "_gemini_next", 0.0)
    monkeypatch.setitem(config.app, "material_vision_gemini_rpm", 30)  # 2 s apart
    clock = iter([100.0, 100.5, 101.0])
    monkeypatch.setattr(vision_screen.time, "monotonic", lambda: next(clock, 200.0))
    for _ in range(3):
        vision_screen._gemini_pace()
    # The first goes at once; the next ones wait for their slot (102.0, 104.0).
    assert sleeps == [pytest.approx(1.5), pytest.approx(3.0)]


def test_a_429_waits_the_server_delay_and_tries_again(gemini):
    gemini.outcomes += [_quota(PER_MINUTE, 15, delay=7), answer()]
    messages = []
    with _logs(messages):
        text = vision_screen.ask_gemini(["YQ=="], "p", "gemini-3.5-flash-lite")
    assert vision_screen.parse(text)["subject"] == "the Moon"
    assert 7.0 in gemini.sleeps
    assert any(f"{PER_MINUTE}=15" in m and "retrying in 7s (1/3)" in m for m in messages)


def test_a_lasting_429_stops_after_the_retries(gemini):
    gemini.outcomes += [_quota(PER_MINUTE, 15)] * 4
    with pytest.raises(vision_screen.VisionUnavailable, match="429 RESOURCE_EXHAUSTED after 3 retries"):
        vision_screen.ask_gemini(["YQ=="], "p", "gemini-3.5-flash-lite")
    assert [s for s in gemini.sleeps if s >= 10] == [10.0, 20.0, 40.0]
    assert len([c for c in gemini.calls if "model" in c]) == 4


@pytest.mark.parametrize(("quota_id", "value"), [(PER_DAY, 1000), (PER_MINUTE, 0)])
def test_a_daily_or_zero_quota_stops_at_once(gemini, quota_id, value):
    gemini.outcomes.append(_quota(quota_id, value, delay=30))
    with pytest.raises(vision_screen.VisionUnavailable, match=f"gemini quota {quota_id} = {value} reached"):
        vision_screen.ask_gemini(["YQ=="], "p", "gemini-3.8-flash")
    assert not [s for s in gemini.sleeps if s >= 10]


def test_a_server_error_is_retried(gemini):
    gemini.outcomes += [_error(503, "UNAVAILABLE"), answer()]
    assert vision_screen.parse(vision_screen.ask_gemini(["YQ=="], "p", "gemini-3.8-flash"))
    assert 10.0 in gemini.sleeps


@pytest.mark.parametrize(("code", "status"), [(400, "INVALID_ARGUMENT"), (403, "PERMISSION_DENIED"), (404, "NOT_FOUND")])
def test_other_errors_stop_and_never_show_the_key(gemini, code, status):
    gemini.outcomes.append(_error(code, status, f"API key {KEY} not valid; also AIzaSyOTHERkey0123456789abcdefgh"))
    with pytest.raises(vision_screen.VisionUnavailable) as caught:
        vision_screen.ask_gemini(["YQ=="], "p", "gemini-3.8-flash")
    assert f"gemini {code} {status}" in str(caught.value)
    assert KEY not in str(caught.value) and "AIzaSyOTHER" not in str(caught.value) and "***" in str(caught.value)


def test_a_network_error_stops_without_the_key(gemini):
    gemini.outcomes.append(ConnectionError(f"https://generativelanguage.googleapis.com/?key={KEY} refused"))
    with pytest.raises(vision_screen.VisionUnavailable) as caught:
        vision_screen.ask_gemini(["YQ=="], "p", "gemini-3.8-flash")
    assert "ConnectionError" in str(caught.value) and KEY not in str(caught.value)


def test_no_key_is_unavailable(gemini, monkeypatch):
    monkeypatch.setitem(config.app, "gemini_api_key", "")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(vision_screen.VisionUnavailable, match="no API key"):
        vision_screen.ask_gemini(["YQ=="], "p", "gemini-3.8-flash")
    assert gemini.calls == []


# ---------------------------------------------------------------- in a task, and the key


def test_a_task_screens_with_gemini_and_a_daily_quota_stops_it(gemini, images, tmp_path):
    gemini.outcomes += [answer(image_type="cgi", subject="a base"), _quota(PER_DAY, 250)]
    session = vision_screen.Session(anchors=["moon"], subject="Ay", backend="gemini", model="gemini-3.8-flash")
    messages = []
    with _logs(messages):
        first = session.screen(_item("A1"))
        second = session.screen(_item("A2"))
        third = session.screen(_item("A3"))
        vision_screen.finish(session)
    assert (first, second, third) == (False, True, True)
    assert session.stopped == f"gemini quota {PER_DAY} = 250 reached"
    assert (session.calls, session.rejected, session.unscreened) == (2, 1, 2)
    assert any("status=stopped: gemini quota" in m for m in messages)


def test_the_key_never_reaches_logs_cache_or_records(gemini, images, tmp_path):
    gemini.outcomes += [answer(), _error(400, "INVALID_ARGUMENT", f"bad key {KEY}")]
    session = vision_screen.Session(anchors=["moon"], subject="Ay", backend="gemini", model="gemini-3.8-flash")
    items = [_item("K1"), _item("K2")]
    messages = []
    with _logs(messages, level="DEBUG"):
        for item in items:
            session.screen(item)
        vision_screen.finish(session)
    written = "".join(path.read_text(encoding="utf-8") for path in Path(tmp_path).rglob("*.json"))
    assert written and "gemini:gemini-3.8-flash" in written
    for text in (*messages, written, session.stopped, json.dumps([i.source_info for i in items])):
        assert KEY not in text
    assert vision_screen.redact(f"x {KEY} y AIzaSyANOTHER0123456789abcdefghij") == "x *** y ***"


class _logs:
    """Collect loguru messages."""

    def __init__(self, messages, level="INFO"):
        self.messages, self.level = messages, level

    def __enter__(self):
        from loguru import logger

        self.handler = logger.add(lambda m: self.messages.append(str(m)), level=self.level)

    def __exit__(self, *exc):
        from loguru import logger

        logger.remove(self.handler)
