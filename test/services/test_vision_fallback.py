"""The fallback model of the visual screening (material_vision_fallback_model): a model that cannot
answer is replaced once for the task, a candidate keeps its decision across the switch, no fallback
or a failing fallback stops as before; the batch report lists unscreened scenes and asks for a check.
Fake backends; no network."""
# The images fixture comes from test_vision_screen; tests name it as an argument.
# ruff: noqa: F811

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT_DIR = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT_DIR))

from app.config import config  # noqa: E402
from app.services import batch_package, batch_production, vision_screen  # noqa: E402
from test.services.test_vision_screen import _item, answer, images  # noqa: E402,F401  (fixture)


@pytest.fixture
def backends():
    """Fake gemini and claude backends; ``fail`` makes one raise VisionUnavailable."""
    calls, fail = [], set()

    def make(name):
        def ask(images, prompt, model):
            calls.append((name, model))
            if name in fail:
                raise vision_screen.VisionUnavailable(f"{name} quota reached")
            return answer(image_type="cgi" if "CGI" in prompt else "observation")
        return ask

    with patch.dict(vision_screen.BACKENDS, {"gemini": make("gemini"), "claude": make("claude")}):
        yield calls, fail


def _session(fallback=("claude", "sonnet")):
    return vision_screen.Session(anchors=["moon"], subject="Ay", backend="gemini", model="gemini-3.5-flash-lite",
                                 fallback=fallback)


def test_a_failing_model_switches_to_the_fallback_for_the_task(images, backends):
    calls, fail = backends
    session = _session()
    assert session.screen(_item("A1"))
    fail.add("gemini")
    messages = []
    with _logs(messages):
        assert session.screen(_item("A2"))  # gemini fails, claude answers the same candidate
        assert session.screen(_item("A3"))  # straight to claude
        vision_screen.finish(session)
    assert calls == [("gemini", "gemini-3.5-flash-lite"), ("gemini", "gemini-3.5-flash-lite"),
                     ("claude", "sonnet"), ("claude", "sonnet")]
    assert (session.backend, session.model, session.switched_from) == ("claude", "sonnet",
                                                                      "gemini:gemini-3.5-flash-lite")
    assert not session.stopped and session.unscreened == 0 and session.accepted == 3
    assert any("switching to claude:sonnet for this task" in m and "gemini quota reached" in m for m in messages)
    summary = next(m for m in messages if "vision screening: model=" in m)
    assert "model=gemini:gemini-3.5-flash-lite→claude:sonnet," in summary and "status=complete" in summary


def test_a_candidate_keeps_its_decision_across_the_switch(images, backends):
    calls, fail = backends
    session = _session(fallback=("claude", "sonnet"))
    session.max_per_term = 2
    first = _item("A1")
    assert session.screen(first)
    fail.add("gemini")
    assert session.screen(_item("A2"))  # the switch
    # Asked again at download time: the same decision, no call, not counted against the term limit.
    before = len(calls)
    assert session.screen(_item("A1")) and len(calls) == before and session.repeats == 1
    assert first.source_info["vision"]["model"] == "gemini:gemini-3.5-flash-lite"
    # The term had its two candidates: a third one moves on (the limit counts candidates, not models).
    assert session.screen(_item("A3")) is False


def test_no_fallback_or_a_failing_fallback_stops_as_before(images, backends):
    calls, fail = backends
    fail.update({"gemini", "claude"})
    for fallback in (None, ("claude", "sonnet")):
        session = _session(fallback=fallback)
        assert session.screen(_item("B1")) and session.screen(_item("B2"))
        assert session.unscreened == 2 and session.stopped
        assert session.stopped == ("claude quota reached" if fallback else "gemini quota reached")
        assert _item_vision(session, "B3")["decision"] == "unscreened"


def test_the_disk_cache_is_per_model(images, backends):
    calls, fail = backends
    fail.add("gemini")
    _session().screen(_item("C1"))  # decided by claude after the switch
    fail.clear()
    calls.clear()
    # A later task that starts with gemini asks gemini (the claude decision is not gemini's).
    _session().screen(_item("C1"))
    assert calls == [("gemini", "gemini-3.5-flash-lite")]
    # A task that starts with claude reads the cached claude decision.
    calls.clear()
    vision_screen.Session(anchors=["moon"], subject="Ay", backend="claude", model="sonnet").screen(_item("C1"))
    assert calls == []


def test_the_setting_and_the_presets():
    with patch.dict(config.app, {"material_vision_screening": True, "material_vision_model": "gemini:",
                                 "material_vision_fallback_model": "claude:sonnet"}):
        session = vision_screen.start(["moon"])
        assert session.fallback == ("claude", "sonnet")
    with patch.dict(config.app, {"material_vision_screening": True, "material_vision_model": "claude:sonnet",
                                 "material_vision_fallback_model": "claude:sonnet"}):
        assert vision_screen.start(["moon"]).fallback is None  # the same model is no fallback
    with patch.dict(config.app, {"material_vision_screening": True, "material_vision_fallback_model": ""}):
        assert vision_screen.start(["moon"]).fallback is None
    preset = batch_production.load_preset(ROOT_DIR / "presets" / "gercek.toml")
    assert preset.app["material_vision_fallback_model"] == "claude:sonnet"
    assert batch_production.APP_OPTIONS["material_vision_fallback_model"] is str


# ---------------------------------------------------------------- the batch report


def test_unscreened_scenes_need_a_check_and_are_listed():
    sources = [
        {"provider": "nasa", "asset_id": "N1", "search_term": "full moon",
         "vision": {"decision": "use", "image_type": "observation"}},
        {"provider": "pixabay", "asset_id": "P2", "search_term": "lunar rover",
         "vision": {"decision": "unscreened", "reasons": ["stopped"]}},
        {"provider": "nasa_svs", "asset_id": "5415", "search_term": "moon phases"},  # screening was off
    ]
    scenes = batch_package.unscreened_scenes(sources)
    assert scenes == [{"asset_id": "P2", "provider": "pixabay", "terim": "lunar rover", "neden": "stopped"}]
    reasons = batch_package.review_reasons({"durum": "tamam", "sayim": {}}, {"taranmamis_sahneler": scenes})
    assert reasons == ["görsel tarama: 1 sahne taranmadan kullanıldı (P2)"]
    assert batch_package.review_reasons({"durum": "tamam", "sayim": {}}, {"taranmamis_sahneler": []}) == []


def test_the_report_reads_the_switch():
    line = ("vision screening: model=gemini:gemini-3.5-flash-lite→claude:sonnet, calls=5, cached=2, repeats=3, "
            "accepted=4, rejected=1, unscreened=0, seconds=12.0, status=complete")
    vision = batch_package.log_summary([line], [])["gorsel_tarama"]
    assert vision["durum"] == "tamamlandı" and vision["cagri"] == 5
    assert vision["yedege_gecti"] == {"ilk": "gemini:gemini-3.5-flash-lite", "yedek": "claude:sonnet"}


def _item_vision(session, asset):
    item = _item(asset)
    session.screen(item)
    return item.source_info["vision"]


class _logs:
    def __init__(self, messages):
        self.messages = messages

    def __enter__(self):
        from loguru import logger

        self.handler = logger.add(lambda m: self.messages.append(str(m)), level="INFO")

    def __exit__(self, *exc):
        from loguru import logger

        logger.remove(self.handler)
