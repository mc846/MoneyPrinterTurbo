"""Visual screening (vision_screen): the answer and the rules, the flow (reject -> next candidate ->
next source -> empty term), three frames of a NASA video, the cache key, the per-term and per-task
limits, quota and session errors, the backends' commands, records and the report. Fake model answers."""

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from PIL import Image

ROOT_DIR = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT_DIR))

from app.config import config  # noqa: E402
from app.models.schema import MaterialInfo, MaterialTagRelevance, VideoAspect  # noqa: E402
from app.services import batch_package, batch_production, material, vision_screen  # noqa: E402

PORTRAIT = VideoAspect.portrait


def answer(image_type="observation", subject="the Moon", matches=True, text=False, person="none", logo=False):
    return json.dumps({"image_type": image_type, "subject": subject, "subject_matches_anchor": matches,
                       "text_overlay": text, "person": person, "logo": logo})


def _item(asset, provider="nasa", term="impact basin", tags=("Moon",), **extra):
    info = {"provider": provider, "asset_id": asset, "search_term": term, "tags": list(tags), "ai_generated": False,
            "preview": f"https://images-assets.nasa.gov/image/{asset}/{asset}~small.jpg",
            "rendition": {"width": 1080, "height": 1920}, **extra}
    return MaterialInfo(provider=provider, url=f"https://x/{asset}", duration=10, source_info=info)


@pytest.fixture
def images(tmp_path):
    """Previews are local files; the cache is per test."""
    def download(url, folder):
        path = Path(folder) / f"{abs(hash(url))}.jpg"
        Image.new("RGB", (64, 36), (40, 40, 40)).save(path)
        return str(path)

    with (patch.object(vision_screen, "_download", side_effect=download),
          patch.object(vision_screen.utils, "storage_dir", return_value=str(tmp_path))):
        yield


@pytest.fixture
def backend():
    """A fake claude backend answering from a queue (or a function of the prompt)."""
    calls = []
    answers = {}

    def ask(images, prompt, model):
        calls.append({"images": len(images), "prompt": prompt, "model": model})
        reply = answers.get("next")
        return reply(prompt) if callable(reply) else reply
    with patch.dict(vision_screen.BACKENDS, {"claude": ask}):
        yield calls, answers


def _session(**kwargs):
    defaults = {"anchors": ["moon"], "subject": "Ay'ın karanlık yüzü", "backend": "claude", "model": "haiku"}
    return vision_screen.Session(**{**defaults, **kwargs})


# ---------------------------------------------------------------- answer and rules


def test_parse_accepts_fenced_json_and_rejects_unknown_values():
    assert vision_screen.parse("```json\n" + answer() + "\n```")["image_type"] == "observation"
    assert vision_screen.parse('Sure: {"image_type": "photo", "subject": "x", "subject_matches_anchor": "true", '
                               '"text_overlay": false, "person": "none", "logo": "false"}')["logo"] is False
    assert vision_screen.parse(answer(image_type="painting")) is None
    assert vision_screen.parse(answer(person="maybe")) is None
    assert vision_screen.parse('{"image_type": "photo"}') is None
    assert vision_screen.parse("") is None


@pytest.mark.parametrize(("kwargs", "use", "reasons"), [
    ({}, True, []),
    ({"image_type": "cgi"}, False, ["type:cgi"]),
    ({"image_type": "illustration"}, False, ["type:illustration"]),
    ({"image_type": "diagram"}, False, ["type:diagram"]),
    ({"matches": False, "subject": "Mars"}, False, ["subject:Mars"]),
    ({"person": "recognizable"}, False, ["person"]),
    ({"person": "unrecognizable"}, True, []),
    ({"logo": True}, False, ["logo"]),
    ({"text": True}, True, []),  # the text filters decide about text
    ({"image_type": "cgi", "logo": True}, False, ["type:cgi", "logo"]),
])
def test_the_real_footage_rules(kwargs, use, reasons):
    result = vision_screen.parse(answer(**kwargs))
    assert vision_screen.decide(result, ["moon"])[:2] == (use, reasons)


def test_a_data_visualization_is_used_with_a_label_and_no_anchor_means_no_subject_rule():
    result = vision_screen.parse(answer(image_type="data_visualization"))
    assert vision_screen.decide(result, ["moon"]) == (True, [], "data_visualization")
    other = vision_screen.parse(answer(matches=False, subject="Mars"))
    assert vision_screen.decide(other, [])[0] is True


# ---------------------------------------------------------------- one candidate


def test_a_rejected_candidate_is_recorded(images, backend):
    calls, answers = backend
    answers["next"] = answer(image_type="cgi", subject="a rover on the Moon")
    item = _item("PROMISE")
    session = _session()
    with patch.object(vision_screen.task_materials, "rejected") as rejected:
        assert session.screen(item) is False
    assert item.source_info["vision"]["decision"] == "reject"
    assert item.source_info["vision"]["reasons"] == ["type:cgi"]
    assert rejected.call_args.args[1:] == ("vision", "type:cgi")
    assert calls[0]["images"] == 1 and calls[0]["model"] == "haiku"
    # The prompt carries the topic, the term, the anchors and the source's title.
    assert "Ay'ın karanlık yüzü" in calls[0]["prompt"] and '"impact basin"' in calls[0]["prompt"]
    assert "any one of them is enough): moon" in calls[0]["prompt"] and "Moon" in calls[0]["prompt"]


def test_a_nasa_video_shows_three_frames_of_its_window(images, backend, tmp_path):
    calls, answers = backend
    answers["next"] = answer()
    item = _item("vid", segment={"start": 12.0, "duration": 5.0},
                 media_type="video", video={"width": 1920, "height": 1080, "renditions": {"preview": "https://x"}})

    def frames(item, folder):
        paths = []
        for index in range(3):
            path = Path(folder) / f"window-{index}.jpg"
            Image.new("RGB", (64, 36)).save(path)
            paths.append(str(path))
        return paths

    with patch.object(vision_screen, "_frames", wraps=vision_screen._frames), \
            patch("app.services.nasa_video.window_frames", side_effect=frames) as window:
        assert _session().screen(item) is True
    assert calls[0]["images"] == 3 and "three frames (start, middle, end)" in calls[0]["prompt"]
    window.assert_called_once()


def test_the_cache_key():
    item = _item("a")
    key = vision_screen.cache_key(item, "preview", "claude", "haiku", ["moon"])
    assert key == vision_screen.cache_key(_item("a"), "preview", "claude", "haiku", ["Moon "])
    assert key != vision_screen.cache_key(item, "preview", "claude", "sonnet", ["moon"])
    assert key != vision_screen.cache_key(item, "preview", "claude", "haiku", ["mars"])
    assert key != vision_screen.cache_key(item, "window:12.0:5.0", "claude", "haiku", ["moon"])
    assert key != vision_screen.cache_key(_item("b"), "preview", "claude", "haiku", ["moon"])
    with patch.object(vision_screen, "PROMPT_VERSION", vision_screen.PROMPT_VERSION + 1):
        assert key != vision_screen.cache_key(item, "preview", "claude", "haiku", ["moon"])


def test_a_second_task_takes_the_decision_from_the_cache(images, backend):
    calls, answers = backend
    answers["next"] = answer(matches=False, subject="Mercury")
    with patch.object(vision_screen.task_materials, "rejected"):
        assert _session().screen(_item("PIA13675")) is False
        second = _session()
        item = _item("PIA13675")
        assert second.screen(item) is False
    assert len(calls) == 1 and second.cached == 1 and second.calls == 0
    assert item.source_info["vision"]["cached"] is True


def test_per_term_limit_moves_the_term_on_without_a_call(images, backend):
    calls, answers = backend
    answers["next"] = answer(image_type="cgi")
    session = _session(max_per_term=2)
    with patch.object(vision_screen.task_materials, "rejected") as rejected:
        results = [session.screen(_item(f"c{i}")) for i in range(3)]
        other_term = session.screen(_item("d1", term="full moon"))
        other_source = session.screen(_item("p1", provider="pixabay"))
    assert results == [False, False, False] and len(calls) == 4
    assert rejected.call_args_list[2].args[2].startswith("limit: 2")
    assert other_term is False and other_source is False  # counted separately, both screened


def test_a_warm_cache_decides_like_a_cold_one(images, backend):
    """The term limit counts candidates, cached ones too: E2E 10.11, a warm run had screened past
    the limit (only calls counted) and taken a different source for "moon phases"."""
    calls, answers = backend
    answers["next"] = lambda prompt: answer(image_type="cgi") if "title-bad" in prompt else answer()
    items = lambda: [_item(f"c{i}", tags=["Moon", "title-bad"]) for i in range(3)] + [_item("good")]  # noqa: E731
    with patch.object(vision_screen.task_materials, "rejected"):
        cold_session = _session(max_per_term=3)
        cold = [cold_session.screen(item) for item in items()]
        warm_session = _session(max_per_term=3)
        warm = [warm_session.screen(item) for item in items()]
    assert cold == warm == [False, False, False, False]  # the fourth is past the limit both times
    assert len(calls) == 3 and warm_session.calls == 0 and warm_session.cached == 3


def test_a_candidate_asked_again_in_the_task_is_counted_once(images, backend):
    calls, answers = backend
    answers["next"] = answer()
    session = _session(max_per_term=1)
    item = _item("a")
    assert session.screen(item) and session.screen(item) and session.screen(_item("a"))
    assert (len(calls), session.calls, session.repeats, session.accepted) == (1, 1, 2, 1)
    assert "cached=0, repeats=2, accepted=1" in session.summary()


def test_the_task_limit_stops_the_screening_and_the_task_goes_on(images, backend):
    calls, answers = backend
    answers["next"] = answer()
    session = _session(max_per_task=1)
    assert session.screen(_item("a")) is True
    item = _item("b")
    assert session.screen(item) is True  # unscreened, used
    assert len(calls) == 1 and session.stopped.startswith("material_vision_max_per_task=1")
    assert item.source_info["vision"] == {"decision": "unscreened", "reasons": ["stopped"]}
    assert "status=stopped: material_vision_max_per_task=1" in session.summary()


def test_a_quota_or_session_error_stops_the_screening(images, backend):
    calls, answers = backend

    def limit(prompt):
        raise vision_screen.VisionUnavailable("claude CLI: Claude AI usage limit reached")
    answers["next"] = limit
    session = _session()
    assert session.screen(_item("a")) is True and session.screen(_item("b")) is True
    assert len(calls) == 1 and "usage limit" in session.stopped and session.unscreened == 2


def test_an_unreadable_answer_is_asked_twice_then_used_unscreened(images, backend):
    calls, answers = backend
    answers["next"] = "I think it is the Moon."
    item = _item("a")
    assert _session().screen(item) is True
    assert len(calls) == 2 and item.source_info["vision"]["reasons"] == ["unreadable"]


def test_no_image_means_unscreened(images, backend):
    calls, _answers = backend
    item = _item("a")
    item.source_info.pop("preview")
    assert _session().screen(item) is True and not calls


# ---------------------------------------------------------------- the flow


def test_reject_next_candidate_next_source_then_empty(images, backend):
    calls, answers = backend
    verdicts = {"n1": answer(image_type="cgi"), "n2": answer(matches=False, subject="Mars"),
                "p1": answer(), "q1": answer(image_type="illustration"), "q2": answer(logo=True)}
    answers["next"] = lambda prompt: next(v for k, v in verdicts.items() if f"title-{k}" in prompt)
    session = _session()
    results = {
        "moon": {"nasa": [_item("n1", tags=["Moon", "title-n1"]), _item("n2", tags=["Moon", "title-n2"])],
                 "pixabay": [_item("p1", provider="pixabay", tags=["moon", "title-p1"])]},
        "empty": {"nasa": [_item("q1", tags=["Moon", "title-q1"], term="empty")],
                  "pixabay": [_item("q2", provider="pixabay", tags=["moon", "title-q2"], term="empty")]},
    }

    def fake_search(provider, search_videos, search_term, **kwargs):
        for item in results[search_term][provider]:
            item.source_info["search_term"] = search_term
        return list(results[search_term][provider])

    with (patch.dict(config.app, {"nasa_text_filter": False, "pixabay_api_keys": ["k"]}),
          patch.object(material, "_search_videos_with_cache", side_effect=fake_search),
          patch.object(vision_screen.task_materials, "rejected")):
        search, _ = material.chain_source_search(
            "nasa", ["pixabay"], crop_to_aspect=False, tag_relevance=MaterialTagRelevance.off,
            exclude_ai_generated=False, anchors=["moon"], screens={"nasa": session.screen, "pixabay": session.screen})
        found = search("moon", 5, PORTRAIT)
        empty = search("empty", 5, PORTRAIT)
    assert [item.source_info["asset_id"] for item in found] == ["p1"]
    assert search.state.decisions["moon"]["chosen"] == "pixabay"
    assert search.state.decisions["moon"]["tried"][0]["screen_rejected"] == 2
    assert empty == [] and search.state.decisions["empty"]["empty"] is True
    assert len(calls) == 5


def test_off_by_default_and_the_settings():
    assert vision_screen.start(["moon"]) is None
    with patch.dict(config.app, {"material_vision_screening": True, "material_vision_model": "ollama:gemma4:31b-cloud",
                                 "material_vision_max_per_term": 2, "material_vision_max_per_task": "5"}):
        session = vision_screen.start(["moon"], "Ay")
    assert (session.backend, session.model, session.max_per_term, session.max_per_task) == (
        "ollama", "gemma4:31b-cloud", 2, 5)
    with patch.dict(config.app, {"material_vision_model": "gpt:x"}):
        assert vision_screen.model_setting() == ("claude", "sonnet")


def _chain_result():
    return (lambda *a, **k: [], {"crop_to_aspect": False, "tag_relevance": MaterialTagRelevance.rank,
                                 "exclude_ai_generated": False})


def test_download_without_screening_keeps_the_old_screens(images):
    """With the setting off, download_videos builds no session and no vision screen."""
    with (patch.object(material, "resolve_fallback_chain", return_value=["coverr"]),
          patch.object(material, "_download_videos_by_script_order", return_value=["a.mp4"]) as by_order,
          patch.object(material, "chain_source_search", return_value=_chain_result()) as chain):
        material.download_videos("t", ["moon"], source="pixabay", match_script_order=True,
                                 tag_relevance=MaterialTagRelevance.rank, fallback_source="coverr")
    assert chain.call_args.kwargs["screens"] is None
    assert "screen_material" not in by_order.call_args.kwargs  # Pixabay and Coverr have no screen of their own


def test_download_with_screening_screens_every_source():
    with (patch.dict(config.app, {"material_vision_screening": True}),
          patch.object(material, "resolve_fallback_chain", return_value=["coverr"]),
          patch.object(material, "_download_videos_by_script_order", return_value=["a.mp4"]) as by_order,
          patch.object(material, "chain_source_search") as chain):
        chain.return_value = (lambda *a, **k: [], {"crop_to_aspect": False, "tag_relevance": MaterialTagRelevance.rank,
                                                   "exclude_ai_generated": False})
        material.download_videos("t", ["moon"], source="pixabay", match_script_order=True,
                                 tag_relevance=MaterialTagRelevance.rank, fallback_source="coverr",
                                 anchors=["moon"], video_subject="Ay")
    assert set(chain.call_args.kwargs["screens"]) == {"pixabay", "coverr"}
    assert "screen_material" in by_order.call_args.kwargs and "save_material" in by_order.call_args.kwargs


# ---------------------------------------------------------------- backends


def _completed(stdout, code=0):
    return subprocess.CompletedProcess(args=[], returncode=code, stdout=stdout, stderr="")


def test_the_claude_backend_command_and_errors(monkeypatch):
    monkeypatch.setenv("CLAUDECODE", "1")
    events = "\n".join([json.dumps({"type": "system", "subtype": "init", "tools": []}),
                        json.dumps({"type": "result", "is_error": False, "result": answer()})])
    with (patch.object(vision_screen, "_claude_cli", return_value="claude"),
          patch.object(vision_screen.subprocess, "run", return_value=_completed(events)) as run):
        assert vision_screen.ask_claude(["AAAA", "BBBB"], "prompt", "haiku") == answer()
    command, kwargs = run.call_args.args[0], run.call_args.kwargs
    assert command[command.index("--input-format") + 1] == "stream-json"
    assert command[command.index("--tools") + 1] == "" and "--safe-mode" in command
    assert command[command.index("--model") + 1] == "haiku"
    message = json.loads(kwargs["input"])
    assert [part["type"] for part in message["message"]["content"]] == ["image", "image", "text"]
    assert "CLAUDECODE" not in kwargs["env"]
    limit = json.dumps({"type": "result", "is_error": True, "result": "Claude AI usage limit reached|1731250000"})
    with (patch.object(vision_screen, "_claude_cli", return_value="claude"),
          patch.object(vision_screen.subprocess, "run", return_value=_completed(limit, 1))):
        with pytest.raises(vision_screen.VisionUnavailable, match="usage limit"):
            vision_screen.ask_claude(["AAAA"], "prompt", "haiku")
    with (patch.object(vision_screen, "_claude_cli", return_value="claude"),
          patch.object(vision_screen.subprocess, "run", side_effect=subprocess.TimeoutExpired("claude", 180))):
        with pytest.raises(vision_screen.VisionUnavailable, match="timed out"):
            vision_screen.ask_claude(["AAAA"], "prompt", "haiku")


def test_the_ollama_backend_reports_credits_as_unavailable():
    class Response:
        status_code = 402

        @staticmethod
        def json():
            return {"error": "Add usage credits or subscribe"}

    with patch.object(vision_screen.requests, "post", return_value=Response()) as post:
        with pytest.raises(vision_screen.VisionUnavailable, match="402"):
            vision_screen.ask_ollama(["AAAA"], "prompt", "gemma4:31b-cloud")
    body = post.call_args.kwargs["json"]
    assert body["messages"][1]["images"] == ["AAAA"] and body["options"]["temperature"] == 0


# ---------------------------------------------------------------- records and the report


def test_the_source_record_keeps_the_decision():
    item = _item("a", vision={"decision": "use", "image_type": "data_visualization", "subject": "the Moon",
                              "subject_matches_anchor": True, "text_overlay": True, "person": "none", "logo": False,
                              "reasons": [], "label": "data_visualization", "model": "claude:haiku",
                              "prompt_version": 1, "cached": False, "raw": "secret"})
    with patch.object(material.task_materials, "downloaded"):
        record = material._material_source_record(item, "C:/x/a.mp4")
    assert record["vision"]["label"] == "data_visualization" and "raw" not in record["vision"]
    assert record["vision"]["model"] == "claude:haiku" and record["vision"]["prompt_version"] == 1


def test_the_report_reads_the_summary_and_the_stop():
    complete = _session()
    complete.calls, complete.cached, complete.rejected, complete.seconds = 4, 2, 1, 12.5
    stopped = _session()
    stopped.stopped, stopped.unscreened = "claude CLI: Claude AI usage limit reached", 3
    summary = batch_package.log_summary(["vision screening: skipped PIA13675 for 'x' (subject:Mercury; Mercury)",
                                         complete.summary()], ["x"])
    assert summary["gorsel_tarama"]["durum"] == "tamamlandı" and summary["gorsel_tarama"]["cagri"] == 4
    assert summary["filtre_elemeleri"]["gorsel_tarama"] == 1
    checks = batch_package.log_summary([stopped.summary()], ["x"])
    warnings = batch_package.check_warnings({**checks, "etiket_gerekli": ["PIA16580"]})
    assert any(w.startswith("görsel tarama tamamlanamadı (claude CLI: Claude AI usage limit reached)") for w in warnings)
    assert "etiket gerekli: PIA16580 (veri görselleştirmesi)" in warnings


def test_the_gercek_preset_screens():
    preset = batch_production.load_preset(ROOT_DIR / "presets" / "gercek.toml")
    assert preset.app["material_vision_screening"] is True
    assert preset.app["material_vision_model"].startswith("claude:")
