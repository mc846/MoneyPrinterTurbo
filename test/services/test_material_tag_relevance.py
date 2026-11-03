"""素材标签相关度（material_tag_relevance）、AI 素材排除与每页结果数。"""

import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from streamlit.testing.v1 import AppTest

ROOT_DIR = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT_DIR))

import cli  # noqa: E402
from app.config import config  # noqa: E402
from app.models.schema import (  # noqa: E402
    MaterialInfo,
    MaterialTagRelevance,
    TaskVideoRequest,
    VideoAspect,
    VideoConcatMode,
    VideoParams,
)
from app.services import material, material_cache, voice  # noqa: E402
from app.services import task as tm  # noqa: E402
from app.services.state import MemoryState  # noqa: E402

RANK, STRICT, OFF = MaterialTagRelevance.rank, MaterialTagRelevance.strict, MaterialTagRelevance.off
PORTRAIT = VideoAspect.portrait


@pytest.mark.parametrize(
    ("text", "words"),
    [
        ("Maiden's Tower", ["maiden", "tower"]),
        ("maiden’s tower", ["maiden", "tower"]),
        ("Tower of London", ["tower", "london"]),
        ("the Blue Mosques", ["blue", "mosque"]),
        ("cities, churches, ferries", ["city", "church", "ferry"]),
        ("glass bus Paris", ["glass", "bus", "paris"]),
        ("kız-kulesi, Istanbul!", ["kız", "kulesi", "istanbul"]),
        ("tower towers TOWER", ["tower"]),
        ("of the and", []),
    ],
)
def test_relevance_words_normalize_terms_and_tags(text, words):
    assert material._relevance_words(text) == words


def _clip(term, name, tags, *, landscape=False, ai=False):
    width, height = (3840, 2160) if landscape else (1080, 1920)
    return MaterialInfo(
        provider="pixabay",
        url=f"https://cdn.example/{name}.mp4",
        duration=3,
        source_info={
            "provider": "pixabay", "search_term": term, "asset_id": name,
            "rendition": {"id": "large", "width": width, "height": height},
            "tags": tags, "ai_generated": ai,
        },
    )


def _names(items):
    return [item.source_info["asset_id"] for item in items]


def test_score_counts_term_words_found_in_tags():
    full = _clip("Maiden's Tower", "full", ["maiden's tower", "istanbul"])
    half = _clip("Maiden's Tower", "half", ["tower", "belgrade"])
    none = _clip("Maiden's Tower", "none", [])

    assert material._tag_relevance("Maiden's Tower", full) == (1, 2)
    assert material._tag_relevance("Maiden's Tower", half) == (pytest.approx(0.5), 1)
    assert material._tag_relevance("Maiden's Tower", none) == (0, 0)
    assert material._tag_relevance("of the", none)[0] == 1


GALATA = [
    _clip("Galata tower", "belgrade", ["building", "tower", "belgrade"]),
    _clip("Galata tower", "galata-landscape", ["galata tower", "istanbul"], landscape=True),
    _clip("Galata tower", "radio", ["radio tower"]),
    _clip("Galata tower", "galata", ["galata", "tower", "istanbul"]),
]


def test_rank_orders_by_score_then_provider_order():
    ordered = material._order_material_candidates(GALATA, PORTRAIT, tag_relevance=RANK)

    assert _names(ordered) == ["galata-landscape", "galata", "belgrade", "radio"]


def test_relevance_comes_before_orientation_and_provider_order():
    ordered = material._order_material_candidates(GALATA, PORTRAIT, tag_relevance=RANK, crop_to_aspect=True)

    # 同分时同方向优先：galata（竖屏）排在 galata-landscape（裁剪）前面。
    assert _names(ordered) == ["galata", "galata-landscape", "belgrade", "radio"]


def test_strict_drops_partial_matches():
    ordered = material._order_material_candidates(GALATA, PORTRAIT, tag_relevance=STRICT, crop_to_aspect=True)

    assert _names(ordered) == ["galata", "galata-landscape"]


def test_sources_without_tags_keep_their_order():
    tagless = [MaterialInfo(provider="pexels", url=f"https://v.example/{n}.mp4", duration=3,
                            source_info={"provider": "pexels", "search_term": "Galata tower", "asset_id": n,
                                         "rendition": {"width": 1080, "height": 1920}})
               for n in ("p1", "p2")]

    for relevance in (RANK, STRICT):
        assert _names(material._order_material_candidates(tagless, PORTRAIT, tag_relevance=relevance)) == ["p1", "p2"]


def test_ai_generated_candidates_are_excluded_only_when_enabled():
    items = [_clip("beach", "ai", ["beach"], ai=True), _clip("beach", "real", ["beach"])]

    assert _names(material._order_material_candidates(items, PORTRAIT, exclude_ai_generated=True)) == ["real"]
    assert _names(material._order_material_candidates(items, PORTRAIT)) == ["ai", "real"]


def _attempted(results, audio_duration, *, source="pixabay", **kwargs):
    attempted, search_options = [], []

    def fake_search(search_term, minimum_duration, video_aspect, **options):
        search_options.append(options)
        return results[search_term]

    def fake_save_video(video_url, save_dir=""):
        attempted.append(video_url.rsplit("/", 1)[-1].removesuffix(".mp4"))
        return f"/tmp/{video_url.rsplit('/', 1)[-1]}"

    with (
        patch.dict(config.app, {"material_directory": ""}),
        patch.object(material, f"search_videos_{source}", side_effect=fake_search),
        patch.object(material, "save_video", side_effect=fake_save_video),
        patch.object(material.material_cache, "load_material_search_cache", return_value=None),
        patch.object(material.material_cache, "save_material_search_cache"),
        patch.object(material.task_artifacts, "patch_script_data", return_value=True),
    ):
        material.download_videos(
            task_id="tag-relevance", search_terms=list(results), source=source,
            audio_duration=audio_duration, max_clip_duration=3, **kwargs,
        )
    return attempted, search_options


def test_script_order_ranks_each_term_by_its_own_tags():
    results = {
        "Galata tower": list(GALATA),
        "Bosphorus": [_clip("Bosphorus", "dubai", ["dubai"]), _clip("Bosphorus", "bridge", ["bosphorus bridge"])],
    }

    attempted, _ = _attempted(results, 5, match_script_order=True, crop_to_aspect=True, tag_relevance=RANK)

    assert attempted == ["galata", "bridge"]


def test_strict_leaves_a_term_without_matches_empty_in_script_order():
    results = {
        "Galata tower": list(GALATA),
        "Maiden's Tower": [_clip("Maiden's Tower", "eiffel", ["eiffel tower"])],
    }

    attempted, _ = _attempted(results, 8, match_script_order=True, tag_relevance=STRICT)

    # Maiden's Tower 没有完全匹配的素材，只有 Galata tower 提供素材；
    # 未开启裁剪时不分方向，两个完全匹配按素材源顺序使用。
    assert attempted == ["galata-landscape", "galata"]


def test_pool_scores_each_candidate_against_the_term_that_found_it():
    results = {
        "Galata tower": [GALATA[0], GALATA[3]],
        "Bosphorus": [_clip("Bosphorus", "dubai", ["dubai"]), _clip("Bosphorus", "bridge", ["bosphorus bridge"])],
    }

    attempted, _ = _attempted(results, 20, video_concat_mode=VideoConcatMode.sequential, tag_relevance=RANK)

    # 比例 1：galata（2/2）、bridge（1/1）；比例 1/2：belgrade；比例 0：dubai。
    assert attempted == ["galata", "bridge", "belgrade", "dubai"]


def test_random_pool_shuffles_only_within_equal_priority():
    results = {
        "Galata tower": [GALATA[0], GALATA[3]],
        "Bosphorus": [
            _clip("Bosphorus", "dubai", ["dubai"]),
            _clip("Bosphorus", "bridge", ["bosphorus bridge"]),
            _clip("Bosphorus", "strait", ["bosphorus strait"]),
        ],
    }
    shuffled = []

    def fake_shuffle(items):
        shuffled.append(_names(items))
        items.reverse()

    with patch.object(material.random, "shuffle", side_effect=fake_shuffle):
        attempted, _ = _attempted(results, 20, video_concat_mode=VideoConcatMode.random, tag_relevance=RANK)

    # galata（2/2）与 bridge、strait（1/1）比例相同，但命中数不同，分属不同优先级。
    assert shuffled == [["galata"], ["bridge", "strait"], ["belgrade"], ["dubai"]]
    assert attempted == ["galata", "strait", "bridge", "belgrade", "dubai"]


def test_tagless_source_ignores_the_settings():
    results = {"Galata tower": [MaterialInfo(provider="pexels", url=f"https://v.example/{n}.mp4", duration=3,
                                             source_info={"provider": "pexels", "asset_id": n})
                                for n in ("p1", "p2")]}

    attempted, search_options = _attempted(
        results, 5, source="pexels", match_script_order=True, tag_relevance=STRICT, exclude_ai_generated=True,
    )

    assert attempted == ["p1", "p2"]
    assert search_options == [{}]


def test_ai_exclusion_through_download_videos():
    results = {"beach": [_clip("beach", "ai", ["beach"], ai=True), _clip("beach", "real", ["beach"])]}

    attempted, _ = _attempted(results, 2, match_script_order=True, exclude_ai_generated=True)

    assert attempted == ["real"]


def test_disabled_settings_keep_provider_order_and_call_shape():
    results = {"Galata tower": list(GALATA)}

    attempted, search_options = _attempted(results, 5, match_script_order=True)

    assert attempted == ["belgrade", "galata-landscape"]
    assert search_options == [{}]


def _pixabay_response(hits):
    return SimpleNamespace(status_code=200, headers={}, json=lambda: {"hits": hits})


def test_pixabay_records_tags_ai_flag_and_configured_per_page():
    hit = {"id": 1, "duration": 8, "tags": "galata, tower,  istanbul ", "isAiGenerated": True,
           "videos": {"large": {"width": 1080, "height": 1920, "url": "https://cdn.example/1.mp4"}}}
    requested = []

    def fake_get(url, **_):
        requested.append(url)
        return _pixabay_response([hit])

    with (
        patch.dict(config.app, {"pixabay_per_page": 120}),
        patch.object(material, "get_api_key", return_value="fixture-key"),
        patch.object(material.requests, "get", side_effect=fake_get),
    ):
        items = material.search_videos_pixabay("galata", 5, PORTRAIT)

    assert "per_page=120" in requested[0]
    assert items[0].source_info["tags"] == ["galata", "tower", "istanbul"]
    assert items[0].source_info["ai_generated"] is True


@pytest.mark.parametrize(
    ("provider", "value", "expected"),
    [("pixabay", None, 50), ("pixabay", 120, 120), ("pixabay", "80", 80), ("pixabay", "abc", 50),
     ("pixabay", True, 50), ("pixabay", 500, 200), ("pixabay", 1, 3), ("pexels", None, 20),
     ("pexels", 100, 80), ("pexels", 0, 1)],
)
def test_search_per_page_uses_provider_limits(provider, value, expected):
    with patch.dict(config.app, {f"{provider}_per_page": value}):
        assert material._search_per_page(provider) == expected


def test_per_page_reaches_the_pexels_query():
    requested = []
    response = SimpleNamespace(status_code=200, headers={}, json=lambda: {"videos": []})

    def fake_get(url, **_):
        requested.append(url)
        return response

    with (
        patch.dict(config.app, {"pexels_per_page": 40}),
        patch.object(material, "get_api_key", return_value="fixture-key"),
        patch.object(material.requests, "get", side_effect=fake_get),
    ):
        material.search_videos_pexels("galata", 5, PORTRAIT)

    assert "per_page=40" in requested[0]


def test_only_non_default_per_page_enters_the_cache_key():
    seen = []

    def fake_load(**kwargs):
        seen.append(kwargs)

    for value in (None, 120):
        with (
            patch.dict(config.app, {"pixabay_per_page": value}),
            patch.object(material.material_cache, "load_material_search_cache", side_effect=fake_load),
        ):
            material._search_videos_with_cache("pixabay", lambda **_: [], "galata", 5, PORTRAIT)

    assert "per_page" not in seen[0]
    assert seen[-1]["per_page"] == 120
    args = {"provider": "pixabay", "search_term": "galata", "minimum_duration": 5, "video_aspect": PORTRAIT}
    assert material_cache._cache_key(**args) != material_cache._cache_key(**args, per_page=120)


def test_cache_keeps_tag_data_and_refreshes_old_entries_without_it(tmp_path):
    common = {"provider": "pixabay", "search_term": "galata", "minimum_duration": 5, "video_aspect": PORTRAIT}
    old_entry = MaterialInfo(provider="pixabay", url="https://cdn.example/old.mp4", duration=8,
                             source_info={"provider": "pixabay", "rendition": {"width": 1080, "height": 1920}})
    fresh = _clip("galata", "fresh", ["galata"], ai=True)
    remote_calls = []

    def remote(search_term, minimum_duration, video_aspect):
        remote_calls.append(search_term)
        return [fresh]

    with patch.object(material_cache.utils, "storage_dir", return_value=str(tmp_path)):
        material_cache.save_material_search_cache(**common, items=[old_entry])
        # 关闭时旧缓存照常命中；需要标签数据时刷新并写入带标签的结果。
        assert material._search_videos_with_cache(search_videos=remote, **common)[0].url.endswith("old.mp4")
        refreshed = material._search_videos_with_cache(search_videos=remote, require_tag_data=True, **common)
        reloaded = material_cache.load_material_search_cache(**common)

    assert remote_calls == ["galata"] and refreshed[0].url.endswith("fresh.mp4")
    assert reloaded[0].source_info["tags"] == ["galata"] and reloaded[0].source_info["ai_generated"] is True


def test_example_config_documents_defaults():
    app = tomllib.loads((ROOT_DIR / "config.example.toml").read_text(encoding="utf-8"))["app"]

    assert app["material_tag_relevance"] == "off"
    assert app["exclude_ai_generated_materials"] is False
    assert (app["pixabay_per_page"], app["pexels_per_page"]) == (50, 20)


def test_video_params_and_api_request_carry_the_settings():
    params = VideoParams(video_subject="test")
    assert (params.material_tag_relevance, params.exclude_ai_generated_materials) == (OFF, False)

    request = TaskVideoRequest.model_validate(
        {"video_subject": "test", "material_tag_relevance": "strict", "exclude_ai_generated_materials": True}
    )
    assert (request.material_tag_relevance, request.exclude_ai_generated_materials) == (STRICT, True)
    with pytest.raises(ValueError):
        TaskVideoRequest.model_validate({"video_subject": "test", "material_tag_relevance": "loose"})


@pytest.mark.parametrize(
    ("flags", "relevance", "exclude"),
    [([], OFF, False), (["--material-tag-relevance", "rank"], RANK, False),
     (["--material-tag-relevance", "strict", "--exclude-ai-generated-materials"], STRICT, True),
     (["--no-exclude-ai-generated-materials"], OFF, False)],
)
def test_cli_flags_reach_video_params(flags, relevance, exclude):
    with patch.dict(config.ui, {}, clear=True):
        params = cli.build_video_params(cli.parse_args(["--video-subject", "test", *flags]))

    assert (params.material_tag_relevance, params.exclude_ai_generated_materials) == (relevance, exclude)


def test_cli_choices_match_the_model():
    assert cli._MATERIAL_TAG_RELEVANCE_MODES == tuple(mode.value for mode in MaterialTagRelevance)


@pytest.mark.parametrize(
    ("relevance", "exclude", "forwarded"),
    [(OFF, False, {}), (RANK, False, {"tag_relevance": RANK}),
     (STRICT, True, {"tag_relevance": STRICT, "exclude_ai_generated": True})],
)
def test_task_forwards_settings_only_when_enabled(relevance, exclude, forwarded):
    params = VideoParams(video_subject="test", video_source="pixabay",
                         material_tag_relevance=relevance, exclude_ai_generated_materials=exclude)
    calls = []

    def fake_download_videos(**kwargs):
        calls.append(kwargs)
        return ["a.mp4"]

    with (
        patch.object(tm.sm, "state", MemoryState()),
        patch.object(tm.material, "download_videos", side_effect=fake_download_videos),
    ):
        tm.get_video_materials("tag-forwarding", params, ["scene"], 10)

    assert {key: calls[0][key] for key in ("tag_relevance", "exclude_ai_generated") if key in calls[0]} == forwarded


def test_webui_controls_follow_config_and_save_changes():
    app_config = dict(config.app, video_source="pixabay", material_tag_relevance="strict",
                      exclude_ai_generated_materials=True)
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
        relevance = next(item for item in app.selectbox if str(item.key).startswith("material_tag_relevance_select"))
        exclude = next(item for item in app.checkbox if str(item.key) == "exclude_ai_generated_materials")
        assert (relevance.value, exclude.value) == ("strict", True)

        relevance.set_value("rank")
        exclude.set_value(False)
        app.run()

    assert (app_config["material_tag_relevance"], app_config["exclude_ai_generated_materials"]) == ("rank", False)
