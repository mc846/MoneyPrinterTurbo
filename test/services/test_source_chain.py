"""Ordered fallback chain of stock sources (video_source_fallback as a list): decisions, early stop,
untagged sources, missing keys, HTTP 429, pools, credits, CLI, agent and WebUI. No network."""

import hashlib
import os
import sys
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

ROOT_DIR = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT_DIR))

import cli  # noqa: E402
from app.config import config  # noqa: E402
from app.models.schema import MaterialInfo, MaterialTagRelevance, VideoAspect, VideoParams  # noqa: E402
from app.services import material, nasa, source_chain  # noqa: E402

PORTRAIT = VideoAspect.portrait
RANK = MaterialTagRelevance.rank
KEYS = {"pixabay_api_keys": ["p-key"], "coverr_api_keys": ["c-key"], "pexels_api_keys": ["x-key"]}


def _item(provider, term, name, tags=None, creator=None):
    info = {"provider": provider, "search_term": term, "asset_id": name,
            "source_page": f"https://{provider}.example/{name}",
            "rendition": {"id": "large", "width": 1080, "height": 1920}}
    if creator:
        info["creator"] = {"id": "1", "name": creator}
    if tags is not None:
        info["tags"] = tags
        info["ai_generated"] = False
    url = (f"https://images-assets.nasa.gov/image/{name}/{name}~orig.jpg" if provider == "nasa"
           else f"https://cdn.{provider}.example/{name}.mp4")
    return MaterialInfo(provider=provider, url=url, duration=5, source_info=info)


def _run(terms, results, *, chain, settings=None, rate_limited=(), audio_duration=None, **kwargs):
    """NASA primary; ``results[source][term]`` lists; returns the calls, decisions and sources."""
    calls = {source: [] for source in ("nasa", "pixabay", "coverr", "pexels")}
    decisions, sources = [], []

    def search(source):
        def fake(search_term, minimum_duration, video_aspect, **options):
            calls[source].append(search_term)
            if source in rate_limited:
                source_chain.note_rate_limited(source)
                return []
            return list(results.get(source, {}).get(search_term, []))
        return fake

    def fake_save_video(video_url, save_dir=""):
        return f"/tmp/{video_url.rsplit('/', 1)[-1]}"

    values = {"material_directory": "", **KEYS, **(settings or {})}
    with ExitStack() as stack:
        stack.enter_context(patch.dict(config.app, values))
        stack.enter_context(patch.object(nasa, "search_images", side_effect=search("nasa")))
        for source in ("pixabay", "coverr", "pexels"):
            stack.enter_context(patch.object(material, f"search_videos_{source}", side_effect=search(source)))
        stack.enter_context(patch.object(nasa, "save_image_clip",
                                         side_effect=lambda item, save_dir="", **o: f"/tmp/{item.source_info['asset_id']}.mp4"))
        stack.enter_context(patch.object(nasa, "face_filter_active", return_value=False))
        # The NASA text filter comes with a later branch (feat/nasa-text-filter); off here.
        stack.enter_context(patch.object(nasa, "text_filter_active", return_value=False, create=True))
        stack.enter_context(patch.object(material, "save_video", side_effect=fake_save_video))
        stack.enter_context(patch.object(material.material_cache, "load_material_search_cache", return_value=None))
        stack.enter_context(patch.object(material.material_cache, "save_material_search_cache"))
        stack.enter_context(patch.object(
            material.task_artifacts, "patch_script_data",
            side_effect=lambda task_id, material_sources: sources.extend(material_sources) or True))
        stack.enter_context(patch.object(material, "_source_decision_listener",
                                         lambda term, decision: decisions.append((term, decision))))
        paths = material.download_videos(
            task_id="chain", search_terms=list(terms), source="nasa",
            audio_duration=audio_duration or 5 * len(terms), max_clip_duration=5,
            match_script_order=True, tag_relevance=kwargs.pop("tag_relevance", RANK),
            fallback_source=chain, **kwargs,
        )
    by_term = {}
    for record in sources:
        by_term.setdefault(record["search_term"], set()).add(record["provider"])
    return SimpleNamespace(paths=[os.path.basename(p) for p in paths], calls=calls,
                           decisions=dict(decisions), by_term=by_term, sources=sources)


# ------------------------------------------------------------------ the setting


@pytest.mark.parametrize(("value", "expected"), [
    ("", []), (None, []), ("none", []), ("pixabay", ["pixabay"]), (" Pixabay ", ["pixabay"]),
    ("pixabay, coverr", ["pixabay", "coverr"]), (["pixabay", "coverr"], ["pixabay", "coverr"]), ([], []),
])
def test_one_value_and_a_list_are_both_accepted(value, expected):
    assert source_chain.validate("nasa", value) == expected
    assert source_chain.compact(expected) == ("" if not expected else expected[0] if len(expected) == 1 else expected)


@pytest.mark.parametrize(("primary", "value", "message"), [
    ("nasa", ["pixabay", "pixabay"], "more than once"),
    ("nasa", ["pixabay", "nasa"], "must differ from video_source"),
    ("pixabay", "pixabay", "must differ from video_source"),
    ("nasa", ["pixabay", "flickr"], "unknown source 'flickr'"),
    ("local", ["pixabay"], "needs a stock video_source"),
    ("nasa", ["none", "pixabay"], "cannot be combined"),
])
def test_invalid_chains_are_refused_with_a_readable_reason(primary, value, message):
    with pytest.raises(source_chain.SourceChainError, match=message):
        source_chain.validate(primary, value)


def test_the_api_model_takes_a_name_or_a_list():
    assert VideoParams(video_subject="t", video_source_fallback="pixabay").video_source_fallback == "pixabay"
    params = VideoParams(video_subject="t", video_source_fallback=["pixabay", "coverr"])
    assert params.video_source_fallback == ["pixabay", "coverr"]


# ------------------------------------------------------------------ the decision


def test_sources_are_tried_in_order_and_later_ones_get_no_request():
    results = {
        "nasa": {"moon": [_item("nasa", "moon", "N1", ["moon"])], "astronaut food": []},
        "pixabay": {"astronaut food": [_item("pixabay", "astronaut food", "P1", ["astronaut", "food"])]},
        "coverr": {"astronaut food": [_item("coverr", "astronaut food", "C1", ["astronaut", "food"])]},
    }
    run = _run(["moon", "astronaut food"], results, chain=["pixabay", "coverr"])
    # "moon": NASA is enough, no fallback is asked; "astronaut food": Pixabay is enough, Coverr is not asked.
    assert run.calls == {"nasa": ["moon", "astronaut food"], "pixabay": ["astronaut food"], "coverr": [],
                         "pexels": []}
    assert run.by_term == {"moon": {"nasa"}, "astronaut food": {"pixabay"}}
    decision = run.decisions["astronaut food"]
    assert (decision["chosen"], decision["reason"]) == ("pixabay", "met")
    assert [(e["source"], e["candidates"], e["suitable"]) for e in decision["tried"]] == [
        ("nasa", 0, 0), ("pixabay", 1, 1)]
    assert run.decisions["moon"]["tried"] == [
        {"source": "nasa", "tagged": True, "candidates": 1, "suitable": 1}]


def test_when_none_is_enough_the_most_suitable_source_wins_and_ties_go_to_the_earlier():
    settings = {"video_source_fallback_min_candidates": 3}
    results = {
        "nasa": {"t": [_item("nasa", "t", "N1", ["t"])]},
        "pixabay": {"t": [_item("pixabay", "t", f"P{i}", ["t"]) for i in range(2)]},
        "coverr": {"t": [_item("coverr", "t", f"C{i}", ["t"]) for i in range(2)]},
    }
    run = _run(["t"], results, chain=["pixabay", "coverr"], settings=settings)
    decision = run.decisions["t"]
    assert [e["suitable"] for e in decision["tried"]] == [1, 2, 2]
    assert (decision["chosen"], decision["reason"]) == ("pixabay", "most_suitable")
    assert run.by_term == {"t": {"pixabay"}}


def test_a_source_without_candidates_is_never_chosen():
    results = {"nasa": {"t": []}, "pixabay": {"t": []},
               "coverr": {"t": [_item("coverr", "t", "C1", ["other words"])]}}
    run = _run(["t"], results, chain=["pixabay", "coverr"])
    decision = run.decisions["t"]
    assert [(e["source"], e["candidates"], e["suitable"]) for e in decision["tried"]] == [
        ("nasa", 0, 0), ("pixabay", 0, 0), ("coverr", 1, 0)]
    assert (decision["chosen"], decision["reason"]) == ("coverr", "no_suitable")
    empty = _run(["t"], {}, chain=["pixabay"])
    assert (empty.decisions["t"]["chosen"], empty.decisions["t"]["reason"]) == (None, "no_candidates")
    assert empty.paths == []


def test_an_untagged_fallback_is_only_the_last_resort():
    weak = {"nasa": {"t": [_item("nasa", "t", "N1", ["other"])]},
            "pixabay": {"t": [_item("pixabay", "t", "P1", ["t", "x"])]},
            "pexels": {"t": [_item("pexels", "t", f"X{i}") for i in range(5)]}}
    # Pixabay has a suitable candidate (relevance 1 >= 0.5) but fewer than 2: it still beats
    # five untagged Pexels clips, and Pexels gets no request.
    run = _run(["t"], weak, chain=["pexels", "pixabay"], settings={"video_source_fallback_min_candidates": 2})
    assert run.calls["pexels"] == [] and run.by_term == {"t": {"pixabay"}}
    assert run.decisions["t"]["reason"] == "most_suitable"
    # No tagged source has a single suitable candidate: Pexels is asked, in chain order.
    none = {"nasa": {"t": [_item("nasa", "t", "N1", ["other"])]}, "pixabay": {"t": []},
            "pexels": {"t": [_item("pexels", "t", "X1")]}}
    run = _run(["t"], none, chain=["pexels", "pixabay"])
    assert run.calls["pexels"] == ["t"] and run.by_term == {"t": {"pexels"}}
    decision = run.decisions["t"]
    assert (decision["chosen"], decision["reason"]) == ("pexels", "untagged_last_resort")
    assert decision["tried"][-1] == {"source": "pexels", "tagged": False, "candidates": 1, "suitable": None}


def test_with_tag_relevance_off_every_source_counts_its_candidates():
    results = {"nasa": {"t": []}, "pexels": {"t": [_item("pexels", "t", "X1")]},
               "pixabay": {"t": [_item("pixabay", "t", "P1", ["t"])]}}
    run = _run(["t"], results, chain=["pexels", "pixabay"], tag_relevance=MaterialTagRelevance.off)
    assert run.calls["pixabay"] == [] and run.by_term == {"t": {"pexels"}}
    assert run.decisions["t"]["reason"] == "met"


def test_a_source_without_a_key_is_skipped_with_one_warning_per_task(caplog_loguru):
    results = {"nasa": {"a": [], "b": []}, "pixabay": {"a": [_item("pixabay", "a", "P1", ["a"])],
                                                     "b": [_item("pixabay", "b", "P2", ["b"])]}}
    run = _run(["a", "b"], results, chain=["coverr", "pixabay"], settings={"coverr_api_keys": []})
    assert run.calls["coverr"] == [] and run.by_term == {"a": {"pixabay"}, "b": {"pixabay"}}
    # The decision still names the skipped source, so the record shows the whole chain.
    assert run.decisions["a"]["chain"] == ["nasa", "pixabay"]
    assert run.decisions["a"]["unavailable"] == [{"source": "coverr", "reason": "no_api_key"}]
    warnings = [m for m in caplog_loguru if "coverr is skipped for this task" in m]
    assert len(warnings) == 1 and "coverr_api_keys" in warnings[0]


def test_a_rate_limited_source_is_skipped_for_the_rest_of_the_task(caplog_loguru):
    results = {"nasa": {"a": [], "b": [], "c": []},
               "coverr": {term: [_item("coverr", term, f"C{term}", [term])] for term in "abc"}}
    run = _run(["a", "b", "c"], results, chain=["pixabay", "coverr"], rate_limited={"pixabay"})
    assert run.calls["pixabay"] == ["a"]  # one 429, then no more requests in this task
    assert run.by_term == {"a": {"coverr"}, "b": {"coverr"}, "c": {"coverr"}}
    assert run.decisions["a"]["tried"][1] == {"source": "pixabay", "tagged": True, "rate_limited": True,
                                               "candidates": 0, "suitable": 0}
    assert run.decisions["b"]["tried"][1]["skipped"] == "rate_limited"
    assert len([m for m in caplog_loguru if "pixabay answered HTTP 429" in m]) == 1


def test_the_stock_search_reports_http_429_to_the_chain():
    response = SimpleNamespace(status_code=429, headers={"retry-after": "60"}, text="")
    with source_chain.watch_rate_limit() as seen:
        assert material._stock_search_results("pexels", response, "videos") is None
    assert seen == {"pexels"}
    material._stock_search_results("pexels", response, "videos")  # outside a chain: nothing breaks


def test_pools_never_mix_within_a_term():
    results = {
        "nasa": {"t": [_item("nasa", "t", "N1", ["other"])]},
        "pixabay": {"t": [_item("pixabay", "t", "P1", ["t"]), _item("pixabay", "t", "P2", ["t"])]},
    }
    run = _run(["t"], results, chain=["pixabay"], audio_duration=15)
    assert run.by_term == {"t": {"pixabay"}}
    assert all(record["provider"] == "pixabay" for record in run.sources)


# ------------------------------------------------------------------ Coverr and Pexels responses


def _response(payload, status=200):
    return SimpleNamespace(status_code=status, headers={"content-type": "application/json"},
                           json=lambda: payload, text="")


COVERR_HIT = {  # the example of https://api.coverr.co/docs/videos/ (fields of a hit)
    "id": "abc123", "title": "Astronaut Eating Food on the Space Station",
    "description": "An astronaut floats while eating", "is_vertical": True,
    "tags": ["astronaut", "space food"], "duration": 12, "max_height": 1920, "max_width": 1080,
    "is_ai_generated": False,
    "urls": {"mp4": "https://cdn.coverr.co/a.mp4", "mp4_preview": "https://cdn.coverr.co/p.mp4",
             "mp4_download": "https://cdn.coverr.co/abc123.mp4?token=x"},
}


def test_coverr_hits_carry_their_tags_and_title_for_relevance():
    payload = {"hits": [COVERR_HIT, {**COVERR_HIT, "id": "plain", "tags": [], "title": ""}], "page": 0, "pages": 1}
    with patch.dict(config.app, {"coverr_api_keys": ["c-key"]}), \
            patch.object(material.requests, "get", return_value=_response(payload)):
        items = material.search_videos_coverr("astronaut food", 5, PORTRAIT)
    tagged = next(item for item in items if item.source_info["asset_id"] == "abc123")
    assert tagged.source_info["tags"] == ["astronaut", "space food", "Astronaut Eating Food on the Space Station"]
    assert tagged.source_info["ai_generated"] is False
    assert material._tag_relevance("astronaut food", tagged)[0] == 1.0
    plain = next(item for item in items if item.source_info["asset_id"] == "plain")
    assert "tags" not in plain.source_info  # no tags and no title: untagged, like Pexels
    assert "coverr" in material.MATERIAL_TAG_SOURCES and "pexels" not in material.MATERIAL_TAG_SOURCES


def test_pexels_hits_have_no_tags():
    payload = {"videos": [{"id": 7, "url": "https://www.pexels.com/video/moon-7/", "duration": 9,
                           "width": 1080, "height": 1920, "user": {"id": 1, "name": "Ana", "url": "https://p/ana"},
                           "video_files": [{"id": 1, "quality": "hd", "file_type": "video/mp4", "width": 1080,
                                            "height": 1920, "link": "https://videos.pexels.com/7.mp4"}]}]}
    with patch.dict(config.app, {"pexels_api_keys": ["x-key"]}), \
            patch.object(material.requests, "get", return_value=_response(payload)):
        items = material.search_videos_pexels("moon", 5, PORTRAIT)
    assert items and "tags" not in (items[0].source_info or {})


# ------------------------------------------------------------------ credits


def _source(provider, name, creator=None, page=None):
    record = {"provider": provider, "asset_id": name, "local_file": f"{name}.mp4", "duration": 5}
    if creator:
        record["creator"] = {"name": creator}
    if page:
        record["source_page"] = page
    return record


def test_credits_follow_each_sources_terms():
    sources = [_source("nasa", "N1"), _source("pixabay", "P1", "maker", "https://pixabay.com/videos/p1/"),
               _source("coverr", "C1"), _source("coverr", "C2", "Jane Doe"),
               _source("pexels", "X1", "Ana", "https://www.pexels.com/video/x1/")]
    text = nasa.credits_text(sources)
    assert "[Pixabay] maker" in text
    assert "[Coverr] Video on Coverr\n   https://coverr.co" in text
    assert "[Coverr] Video by Jane Doe on Coverr" in text
    assert "[Pexels] Video by Ana on Pexels\n   https://www.pexels.com/video/x1/" in text
    assert "Images: NASA and partners, Pixabay, Coverr, Pexels" in text and "not endorsed by NASA" in text
    assert "Coverr: https://coverr.co" in text


def test_a_task_with_coverr_but_no_nasa_writes_credits_without_the_nasa_lines(tmp_path):
    path = nasa.write_credits_file(str(tmp_path), [_source("pixabay", "P1", "maker"), _source("coverr", "C1")])
    text = Path(path).read_text(encoding="utf-8")
    assert "Footage: Pixabay, Coverr" in text and "NASA" not in text
    # Pixabay and Pexels alone need no attribution: no file, as before.
    assert nasa.write_credits_file(str(tmp_path), [_source("pexels", "X1", "Ana")]) == ""
    assert not (tmp_path / "credits.txt").exists()


def test_the_on_screen_credit_stays_nasa_only():
    assert nasa.overlay_credit_text([_source("coverr", "C1")], "tr-TR") == ""


# ------------------------------------------------------------------ CLI and agent


def _cli_params(*flags):
    with patch.dict(config.ui, {}, clear=True):
        args = cli.parse_args(["--video-subject", "t", "--video-source", "nasa", *flags])
        return cli.build_video_params(args)


@pytest.mark.parametrize(("flags", "expected"), [
    (["--video-source-fallback", "pixabay"], "pixabay"),
    (["--video-source-fallback", "pixabay", "coverr"], ["pixabay", "coverr"]),
    (["--video-source-fallback=pixabay,coverr"], ["pixabay", "coverr"]),
    (["--video-source-fallback", "none"], ""),
])
def test_cli_takes_several_fallback_sources(flags, expected):
    assert _cli_params(*flags).video_source_fallback == expected


@pytest.mark.parametrize("argv", [
    ["--video-source", "nasa", "--video-source-fallback", "pixabay", "pixabay"],
    ["--video-source", "nasa", "--video-source-fallback", "pixabay", "nasa"],
])
def test_cli_refuses_a_repeated_source_or_the_primary(argv):
    with patch.dict(config.ui, {}, clear=True), patch("app.services.task.start") as start:
        assert cli.run_cli(["--video-subject", "t", *argv]) == 2
    start.assert_not_called()


def test_cli_refuses_an_unknown_source_while_parsing():
    with pytest.raises(SystemExit):
        cli.parse_args(["--video-subject", "t", "--video-source-fallback", "pixabay", "flickr"])


def test_agent_reads_every_fallback_value():
    sys.path.insert(0, str(ROOT_DIR / "docs" / "skill"))
    import mpt_agent

    args = ["--video-source", "nasa", "--video-source-fallback", "pixabay", "coverr", "--video-subject", "x"]
    assert mpt_agent._last_cli_values(args, "--video-source-fallback") == ["pixabay", "coverr"]
    assert mpt_agent._last_cli_values(["--video-source-fallback=pixabay,coverr"], "--video-source-fallback") == [
        "pixabay", "coverr"]


# ------------------------------------------------------------------ WebUI


def _config_file_hash():
    return hashlib.sha256(Path(config.config_file).read_bytes()).hexdigest()


def test_webui_edits_the_chain_in_order_and_warns_about_missing_keys():
    from streamlit.testing.v1 import AppTest

    settings = dict(config.app, llm_provider="openai", video_source="pixabay", pixabay_api_keys=["p-key"],
                    coverr_api_keys=[], pexels_api_keys=[], video_source_fallback="coverr")
    before = _config_file_hash()
    with (
        patch.object(config, "app", settings),
        patch.object(config, "ui", dict(config.ui, voice_mode="none", language="en")),
        patch.object(config, "try_save_config", return_value=True),
        patch.object(config, "save_config"),
    ):
        app = AppTest.from_file(str(ROOT_DIR / "webui" / "Main.py"), default_timeout=60)
        app.session_state["ui_language"] = "en"
        app.session_state["settings_dialog_open"] = True
        app.session_state["settings_dialog_target_tab"] = "video"
        app.session_state["settings_material_providers"] = {"stock_video": "pixabay"}
        app.run()
        assert [str(item.value) for item in app.exception] == []
        chain = app.multiselect(key="video_source_fallback_input")
        # The saved single value is shown as a one-element chain; the primary is not offered.
        assert chain.value == ["Coverr"] and "Pixabay" not in chain.options
        assert any("Coverr has no API key" in str(item.value) for item in app.warning)
        chain.set_value(["NASA", "Coverr"]).run()
        assert config.snapshot_config_with_pending(config.app)["video_source_fallback"] == ["nasa", "coverr"]
        assert any("Pixabay → NASA → Coverr" in str(item.value) for item in app.caption)
        app.multiselect(key="video_source_fallback_input").set_value(["Pexels"]).run()
        assert any("Pexels clips have no tags" in str(item.value) for item in app.caption)
    assert _config_file_hash() == before


def test_webui_submits_the_chain_without_the_primary():
    from streamlit.testing.v1 import AppTest

    settings = dict(config.app, llm_provider="openai", video_source="pexels", pexels_api_keys=[],
                    pixabay_api_keys=["p-key"], coverr_api_keys=[], video_source_fallback=["nasa", "pixabay", "coverr"])
    with (
        patch.object(config, "app", settings),
        patch.object(config, "ui", dict(config.ui, voice_mode="none")),
        patch.object(config, "try_save_config", return_value=True),
        patch.object(nasa, "face_extra_installed", return_value=True),
        patch("app.services.webui_task.submit_generation") as submit_generation,
    ):
        app = AppTest.from_file(str(ROOT_DIR / "webui" / "Main.py"), default_timeout=60)
        app.session_state["ui_language"] = "en"
        app.run()

        def widget(elements, key):
            return next(item for item in elements if str(getattr(item, "key", "")).startswith(key))

        widget(app.text_area, "video_subject").set_value("Moon").run()
        widget(app.text_area, "video_script").set_value("The moon.").run()
        widget(app.text_area, "video_terms").set_value("moon").run()
        app.session_state["video_source_select_en"] = "nasa"
        app.run()
        widget(app.button, "generate_video_button").click().run()
    params = submit_generation.call_args.kwargs["params"]
    assert params.video_source == "nasa" and params.video_source_fallback == ["pixabay", "coverr"]


@pytest.fixture
def caplog_loguru():
    from loguru import logger

    messages = []
    handler = logger.add(lambda message: messages.append(str(message)), level="WARNING")
    yield messages
    logger.remove(handler)


def test_one_parseable_log_line_per_term(caplog_info):
    results = {"nasa": {"t": []}, "pixabay": {"t": []}, "pexels": {"t": [_item("pexels", "t", "X1")]}}
    _run(["t"], results, chain=["pixabay", "pexels"], rate_limited={"pixabay"})
    (line,) = [m for m in caplog_info if "material source chain" in m]
    assert "term='t', tried=nasa:0/0,pixabay:0/0:429,pexels:1/-, using=pexels, reason=untagged_last_resort" in line


@pytest.fixture
def caplog_info():
    from loguru import logger

    messages = []
    handler = logger.add(lambda message: messages.append(str(message)), level="INFO")
    yield messages
    logger.remove(handler)


def test_decision_does_not_depend_on_the_cache(tmp_path):
    """A cache hit repeats the decision: it only depends on the filtered candidate lists."""
    results = {"nasa": {"t": []}, "pixabay": {"t": [_item("pixabay", "t", "P1", ["t"])]}}
    first = _run(["t"], results, chain=["pixabay"])
    second = _run(["t"], results, chain=["pixabay"])
    assert first.decisions == second.decisions
