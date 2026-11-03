"""视觉风格素材关键词（video_terms_style）、锚点排序与不规则复数。"""

import json
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
    MaterialInfo,
    MaterialTagRelevance,
    TaskVideoRequest,
    VideoAspect,
    VideoConcatMode,
    VideoParams,
    VideoTermsRequest,
    VideoTermsStyle,
)
from app.services import llm, material, voice  # noqa: E402
from app.services import task as tm  # noqa: E402
from app.services.state import MemoryState  # noqa: E402

LEGACY, VISUAL = VideoTermsStyle.legacy, VideoTermsStyle.visual
RANK, STRICT, OFF = MaterialTagRelevance.rank, MaterialTagRelevance.strict, MaterialTagRelevance.off
PORTRAIT = VideoAspect.portrait
LEGACY_RULE = "always add the main subject of the video"


def _model(*responses):
    """依次返回给定回复的假模型，并记录收到的提示词。"""
    prompts = []
    queue = list(responses)

    def generate_response(prompt, **kwargs):
        prompts.append(prompt)
        response = queue.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    return prompts, generate_response


# ---------------------------------------------------------------- prompts


def test_legacy_prompt_is_unchanged():
    prompts, model = _model('["Istanbul autumn"]')
    with patch.object(llm, "_generate_response", side_effect=model):
        assert llm.generate_terms("İstanbul'da sonbahar", "Script.", amount=5) == ["Istanbul autumn"]

    assert LEGACY_RULE in prompts[0]
    assert "anchors" not in prompts[0]


def test_visual_prompt_asks_for_concrete_terms_and_separate_anchors():
    prompt = llm._visual_terms_prompt("İstanbul'da sonbahar", "Script.", 5, False)

    assert LEGACY_RULE not in prompt
    assert "Generate 5 search terms for stock videos" in prompt
    assert "concrete, visual english noun phrase of 1-3 words" in prompt
    assert "do not add the main subject or the place to every term" in prompt
    assert "at most 2 of the terms may contain a place name" in prompt
    assert '"Galata Tower"' in prompt and '["istanbul", "turkey"]' in prompt
    assert "same order as the script narration" not in prompt
    example = json.loads(prompt.split("## Output Example:\n", 1)[1].split("\n", 1)[0])
    assert set(example) == {"terms", "anchors"}


def test_visual_prompt_keeps_the_script_order_count_and_rule():
    legacy_prompts, model = _model(json.dumps([f"t{i}" for i in range(8)]))
    with patch.object(llm, "_generate_response", side_effect=model):
        llm.generate_terms("topic", "Script.", amount=8, match_script_order=True)
    prompt = llm._visual_terms_prompt("topic", "Script.", 8, True)

    goal = "Generate 8 chronological stock-video search terms that follow the order of topics"
    rule = "keep the terms in the same order as the script narration; earlier terms must describe earlier visual moments."
    assert goal in legacy_prompts[0] and goal in prompt
    assert rule in legacy_prompts[0] and rule in prompt
    example = json.loads(prompt.split("## Output Example:\n", 1)[1].split("\n", 1)[0])
    assert len(example["terms"]) == 8
    assert "at most 3 of the terms" in prompt


@pytest.mark.parametrize(
    ("style", "app_config", "expected"),
    [
        (None, {}, LEGACY),
        (None, {"video_terms_style": "visual"}, VISUAL),
        (None, {"video_terms_style": " Visual "}, VISUAL),
        (None, {"video_terms_style": "fancy"}, LEGACY),
        ("legacy", {"video_terms_style": "visual"}, LEGACY),
        (VISUAL, {}, VISUAL),
    ],
)
def test_style_comes_from_the_request_then_the_config(style, app_config, expected):
    assert llm.resolve_video_terms_style(style, app_config=app_config) is expected


# ---------------------------------------------------- parsing and fallback


@pytest.mark.parametrize(
    ("response", "terms", "anchors"),
    [
        (
            '```json\n{"terms": [" autumn leaves ", "", "ferry"], '
            '"anchors": ["Istanbul", "istanbul", " turkey ", "far too many words here", 3, ""]}\n```',
            ["autumn leaves", "ferry"],
            ["Istanbul", "turkey"],
        ),
        ('Here: {"terms": ["ferry"], "anchors": ["istanbul"]} Enjoy.', ["ferry"], ["istanbul"]),
        ('["ferry", "seagull"]', ["ferry", "seagull"], []),
        ('{"terms": ["ferry"], "anchors": "istanbul"}', ["ferry"], []),
        ('{"terms": ["a", "b", "c", "d", "e", "f", "g"], "anchors": ["x"]}', ["a", "b", "c", "d", "e"], ["x"]),
    ],
)
def test_visual_response_yields_terms_and_cleaned_anchors(response, terms, anchors):
    prompts, model = _model(response)
    with patch.object(llm, "_generate_response", side_effect=model):
        assert llm.generate_visual_terms("topic", "Script.", amount=5) == (terms, anchors)
    assert len(prompts) == 1


@pytest.mark.parametrize(
    "response",
    ["no json here", '{"terms": "ferry"}', '{"terms": [1, 2]}', '{"anchors": ["x"]}', '{"terms": [" "]}', ""],
)
def test_unusable_visual_response_falls_back_to_the_legacy_prompt(response):
    prompts, model = _model(response, '["Istanbul ferry"]')
    with patch.object(llm, "_generate_response", side_effect=model):
        assert llm.generate_visual_terms("topic", "Script.", amount=5, match_script_order=True) == (
            ["Istanbul ferry"],
            [],
        )

    assert "anchors" in prompts[0]
    assert LEGACY_RULE in prompts[1] and "same order as the script narration" in prompts[1]


def test_model_exception_falls_back_to_the_legacy_prompt():
    prompts, model = _model(RuntimeError("timeout"), '["ferry"]')
    with patch.object(llm, "_generate_response", side_effect=model):
        assert llm.generate_visual_terms("topic", "Script.") == (["ferry"], [])
    assert len(prompts) == 2


def test_provider_error_ends_without_a_legacy_retry():
    prompts, model = _model("Error: quota exceeded")
    with patch.object(llm, "_generate_response", side_effect=model):
        assert llm.generate_visual_terms("topic", "Script.") == ([], [])
    assert len(prompts) == 1


def test_visual_generation_forwards_the_app_config_snapshot():
    snapshot = {"llm_provider": "openai"}
    calls = []

    def model(prompt, **kwargs):
        calls.append(kwargs)
        return "nonsense" if len(calls) == 1 else '["ferry"]'

    with patch.object(llm, "_generate_response", side_effect=model):
        llm.generate_visual_terms("topic", "Script.", app_config=snapshot)

    assert calls == [{"app_config": snapshot}, {"app_config": snapshot}]


# ------------------------------------------------------------------- API


def test_api_terms_response_keeps_its_shape_in_both_styles():
    responses = {
        LEGACY: '["Istanbul ferry"]',
        VISUAL: '{"terms": ["ferry"], "anchors": ["istanbul"]}',
    }
    bodies = {}
    for style, response in responses.items():
        with (
            patch.dict(config.app, {"api_key": ""}),
            patch.object(llm, "_generate_response", return_value=response),
            TestClient(asgi.get_application()) as client,
        ):
            reply = client.post("/api/v1/terms", json={
                "video_subject": "Istanbul", "video_script": "Ferries.", "video_terms_style": style.value,
            })
        assert reply.status_code == 200
        bodies[style] = reply.json()

    assert bodies[LEGACY]["data"] == {"video_terms": ["Istanbul ferry"]}
    assert bodies[VISUAL]["data"] == {"video_terms": ["ferry"]}
    assert set(bodies[LEGACY]) == set(bodies[VISUAL])


def test_api_without_a_style_keeps_the_legacy_call():
    body = VideoTermsRequest(video_subject="Coffee", video_script="Beans.", amount=4)
    with (
        patch.dict(config.app, {"video_terms_style": "legacy"}),
        patch.object(llm_controller.llm, "generate_terms", return_value=["beans"]) as legacy,
        patch.object(llm_controller.llm, "generate_visual_terms") as visual,
    ):
        response = llm_controller.generate_video_terms(None, body)

    assert response == {"status": 200, "data": {"video_terms": ["beans"]}}
    legacy.assert_called_once_with(video_subject="Coffee", video_script="Beans.", amount=4, match_script_order=False)
    visual.assert_not_called()


def test_api_follows_the_configured_style_and_rejects_unknown_styles():
    body = VideoTermsRequest(video_subject="Coffee", video_script="Beans.")
    with (
        patch.dict(config.app, {"video_terms_style": "visual"}),
        patch.object(llm_controller.llm, "generate_visual_terms", return_value=(["beans"], ["coffee"])),
    ):
        assert llm_controller.generate_video_terms(None, body)["data"] == {"video_terms": ["beans"]}

    with pytest.raises(ValueError):
        VideoTermsRequest.model_validate({"video_terms_style": "fancy"})


# ------------------------------------------------------- anchor ordering


def _clip(term, name, tags, *, landscape=False):
    width, height = (3840, 2160) if landscape else (1080, 1920)
    return MaterialInfo(
        provider="pixabay",
        url=f"https://cdn.example/{name}.mp4",
        duration=3,
        source_info={
            "provider": "pixabay", "search_term": term, "asset_id": name,
            "rendition": {"id": "large", "width": width, "height": height},
            "tags": tags, "ai_generated": False,
        },
    )


def _names(items):
    return [item.source_info["asset_id"] for item in items]


FERRIES = [
    _clip("ferry", "plain", ["ferry", "boat"]),
    _clip("ferry", "cropped-istanbul", ["ferry", "istanbul"], landscape=True),
    _clip("ferry", "istanbul", ["ferry", "istanbul", "bosphorus"]),
    _clip("ferry", "boat-istanbul", ["boat", "istanbul", "turkey"]),
    _clip("ferry", "istanbul-turkey", ["ferry", "istanbul", "turkey"], landscape=True),
]


def test_priority_is_score_then_anchors_then_orientation_then_provider():
    ordered = material._order_material_candidates(
        FERRIES, PORTRAIT, crop_to_aspect=True, tag_relevance=RANK, anchors=("istanbul", "turkey"),
    )

    # 关键词全部命中的四个里：两个锚点 > 一个锚点（同方向 > 裁剪）> 没有锚点；
    # boat-istanbul 有两个锚点，但关键词没有命中，排在最后。
    assert _names(ordered) == ["istanbul-turkey", "istanbul", "cropped-istanbul", "plain", "boat-istanbul"]


def test_equal_anchor_matches_keep_provider_order():
    items = [_clip("ferry", name, ["ferry", "istanbul"]) for name in ("first", "second", "third")]

    assert _names(material._order_material_candidates(items, PORTRAIT, tag_relevance=RANK, anchors=("istanbul",))) == [
        "first", "second", "third",
    ]


def test_an_anchor_matches_only_when_all_its_words_are_in_the_tags():
    items = [
        _clip("taxi", "york", ["taxi", "york"]),
        _clip("taxi", "new-york", ["taxi", "new york city"]),
    ]

    assert _names(material._order_material_candidates(items, PORTRAIT, tag_relevance=RANK, anchors=("New York",))) == [
        "new-york", "york",
    ]


def test_strict_does_not_filter_by_anchors():
    ordered = material._order_material_candidates(
        FERRIES, PORTRAIT, tag_relevance=STRICT, anchors=("bosphorus",),
    )

    # plain 没有锚点也保留；boat-istanbul 因关键词未完全命中被 strict 剔除。
    assert _names(ordered) == ["istanbul", "plain", "cropped-istanbul", "istanbul-turkey"]


def test_anchors_have_no_effect_when_relevance_is_off():
    with_anchors = material._order_material_candidates(FERRIES, PORTRAIT, crop_to_aspect=True, anchors=("turkey",))
    without = material._order_material_candidates(FERRIES, PORTRAIT, crop_to_aspect=True)

    assert _names(with_anchors) == _names(without) == [
        "plain", "istanbul", "boat-istanbul", "cropped-istanbul", "istanbul-turkey",
    ]


def _attempted(results, audio_duration, **kwargs):
    attempted, order_calls = [], []
    order = material._order_material_candidates

    def fake_search(search_term, minimum_duration, video_aspect, **options):
        return results[search_term]

    def fake_save_video(video_url, save_dir=""):
        attempted.append(video_url.rsplit("/", 1)[-1].removesuffix(".mp4"))
        return f"/tmp/{video_url.rsplit('/', 1)[-1]}"

    def recording_order(items, video_aspect, **options):
        order_calls.append(options)
        return order(items, video_aspect, **options)

    with (
        patch.dict(config.app, {"material_directory": ""}),
        patch.object(material, "search_videos_pixabay", side_effect=fake_search),
        patch.object(material, "save_video", side_effect=fake_save_video),
        patch.object(material, "_order_material_candidates", side_effect=recording_order),
        patch.object(material.material_cache, "load_material_search_cache", return_value=None),
        patch.object(material.material_cache, "save_material_search_cache"),
        patch.object(material.task_artifacts, "patch_script_data", return_value=True),
    ):
        material.download_videos(
            task_id="anchors", search_terms=list(results), source="pixabay",
            audio_duration=audio_duration, max_clip_duration=3, **kwargs,
        )
    return attempted, order_calls


def test_script_order_uses_anchors_per_term():
    results = {
        "ferry": [_clip("ferry", "plain", ["ferry"]), _clip("ferry", "istanbul", ["ferry", "istanbul"])],
        "autumn leaves": [_clip("autumn leaves", "fuji", ["autumn", "leaf", "japan"]),
                          _clip("autumn leaves", "park", ["autumn leaves", "istanbul", "park"])],
    }

    attempted, _ = _attempted(results, 5, match_script_order=True, tag_relevance=RANK, anchors=["istanbul"])

    assert attempted == ["istanbul", "park"]


def test_pool_uses_anchors_after_each_term_score():
    results = {
        "ferry": [_clip("ferry", "plain", ["ferry"]), _clip("ferry", "istanbul", ["ferry", "istanbul"])],
        "Galata tower": [_clip("Galata tower", "tower", ["tower", "istanbul"])],
    }

    attempted, _ = _attempted(
        results, 20, video_concat_mode=VideoConcatMode.sequential, tag_relevance=RANK, anchors=["istanbul"],
    )

    assert attempted == ["istanbul", "plain", "tower"]


def test_download_videos_passes_anchors_only_with_relevance():
    results = {"ferry": [_clip("ferry", "plain", ["ferry"])]}

    _, ranked = _attempted(results, 2, match_script_order=True, tag_relevance=RANK, anchors=["istanbul", " "])
    _, unranked = _attempted(results, 2, match_script_order=True, crop_to_aspect=True, anchors=["istanbul"])
    _, no_anchors = _attempted(results, 2, match_script_order=True, tag_relevance=RANK)

    assert ranked[0]["anchors"] == ("istanbul",)
    assert "anchors" not in unranked[0]
    assert "anchors" not in no_anchors[0]


# ------------------------------------------------------ irregular plurals


@pytest.mark.parametrize(
    ("plural", "singular"),
    [("leaves", "leaf"), ("people", "person"), ("children", "child"), ("geese", "goose"), ("mice", "mouse"),
     ("wolves", "wolf"), ("women", "woman"), ("men", "man"), ("feet", "foot"), ("teeth", "tooth"),
     ("knives", "knife"), ("cacti", "cactus")],
)
def test_irregular_plurals_reduce_to_the_singular(plural, singular):
    assert material._relevance_words(plural) == [singular]
    assert material._relevance_words(singular) == [singular]


def test_autumn_leaves_match_a_leaf_tag():
    item = _clip("autumn leaves", "leaf", ["autumn", "maple leaf"])

    assert material._tag_relevance("autumn leaves", item) == (1, 2)
    assert material._relevance_words("ferries, buses, cities") == ["ferry", "buse", "city"]


# -------------------------------------------------------------- layers


def test_example_config_documents_the_legacy_default():
    app = tomllib.loads((ROOT_DIR / "config.example.toml").read_text(encoding="utf-8"))["app"]

    assert app["video_terms_style"] == "legacy"


def test_video_params_and_api_request_carry_the_settings():
    params = VideoParams(video_subject="test")
    assert (params.video_terms_style, params.video_anchors) == (None, None)

    request = TaskVideoRequest.model_validate(
        {"video_subject": "test", "video_terms_style": "visual", "video_anchors": ["istanbul", "turkey"]}
    )
    assert (request.video_terms_style, request.video_anchors) == (VISUAL, ["istanbul", "turkey"])
    with pytest.raises(ValueError):
        TaskVideoRequest.model_validate({"video_subject": "test", "video_terms_style": "fancy"})


@pytest.mark.parametrize(
    ("flags", "style", "anchors"),
    [
        ([], None, None),
        (["--video-terms-style", "visual"], VISUAL, None),
        (["--video-terms", "ferry", "--video-anchors", " istanbul , turkey,， "], None, ["istanbul", "turkey"]),
        (["--video-terms-style", "legacy", "--video-anchors", " , "], LEGACY, None),
    ],
)
def test_cli_flags_reach_video_params(flags, style, anchors):
    with patch.dict(config.ui, {}, clear=True):
        params = cli.build_video_params(cli.parse_args(["--video-subject", "test", *flags]))

    assert (params.video_terms_style, params.video_anchors) == (style, anchors)


def test_cli_choices_match_the_model():
    assert cli._VIDEO_TERMS_STYLES == tuple(style.value for style in VideoTermsStyle)


def test_task_keeps_the_legacy_call_by_default():
    params = VideoParams(video_subject="Coffee", match_materials_to_script=True)
    with (
        patch.dict(config.app, {"video_terms_style": "legacy"}),
        patch.object(tm.llm, "generate_terms", return_value=["beans"]) as legacy,
        patch.object(tm.llm, "generate_visual_terms") as visual,
    ):
        assert tm.generate_terms("task", params, "Beans.") == ["beans"]

    legacy.assert_called_once_with(video_subject="Coffee", video_script="Beans.", amount=8, match_script_order=True)
    visual.assert_not_called()
    assert params.video_anchors is None


@pytest.mark.parametrize(
    ("params_style", "config_style"),
    [(VISUAL, "legacy"), (None, "visual")],
)
def test_task_generates_visual_terms_and_records_anchors(params_style, config_style):
    params = VideoParams(video_subject="İstanbul'da sonbahar", video_terms_style=params_style)
    with (
        patch.dict(config.app, {"video_terms_style": config_style}),
        patch.object(tm.llm, "generate_terms") as legacy,
        patch.object(tm.llm, "generate_visual_terms", return_value=(["ferry"], ["istanbul"])) as visual,
        patch.object(tm.twelvelabs, "rerank_terms_by_subject", side_effect=lambda **kwargs: kwargs["search_terms"]),
    ):
        assert tm.generate_terms("task", params, "Vapurlar.") == ["ferry"]

    visual.assert_called_once_with(video_subject="İstanbul'da sonbahar", video_script="Vapurlar.", amount=5,
                                   match_script_order=False)
    legacy.assert_not_called()
    assert params.video_anchors == ["istanbul"]
    assert params.model_dump()["video_anchors"] == ["istanbul"]


@pytest.mark.parametrize("user_anchors", ["bosphorus", ["bosphorus"]])
def test_user_anchors_win_over_generated_ones(user_anchors):
    params = VideoParams(video_subject="topic", video_terms_style=VISUAL, video_anchors=user_anchors)
    with (
        patch.object(tm.llm, "generate_visual_terms", return_value=(["ferry"], ["istanbul"])),
        patch.object(tm.twelvelabs, "rerank_terms_by_subject", side_effect=lambda **kwargs: kwargs["search_terms"]),
    ):
        tm.generate_terms("task", params, "Script.")

    assert params.video_anchors == user_anchors


def test_user_terms_without_anchors_use_no_anchors():
    params = VideoParams(video_subject="topic", video_terms="ferry", video_terms_style=VISUAL)
    with (
        patch.object(tm.llm, "generate_visual_terms") as visual,
        patch.object(tm.twelvelabs, "rerank_terms_by_subject", side_effect=lambda **kwargs: kwargs["search_terms"]),
    ):
        assert tm.generate_terms("task", params, "Script.") == ["ferry"]

    visual.assert_not_called()
    assert params.video_anchors is None


@pytest.mark.parametrize(
    ("relevance", "anchors", "forwarded"),
    [
        (RANK, "istanbul，turkey, ", ["istanbul", "turkey"]),
        (STRICT, ["istanbul"], ["istanbul"]),
        (RANK, None, None),
        (RANK, " , ", None),
        (OFF, "istanbul", None),
    ],
)
def test_task_forwards_anchors_only_with_relevance(relevance, anchors, forwarded):
    params = VideoParams(video_subject="test", video_source="pixabay", material_tag_relevance=relevance,
                         video_anchors=anchors)
    calls = []

    def fake_download_videos(**kwargs):
        calls.append(kwargs)
        return ["a.mp4"]

    with (
        patch.object(tm.sm, "state", MemoryState()),
        patch.object(tm.material, "download_videos", side_effect=fake_download_videos),
    ):
        tm.get_video_materials("anchor-forwarding", params, ["scene"], 10)

    assert calls[0].get("anchors") == forwarded


# ------------------------------------------------------------------ WebUI


def _widget(elements, key):
    return next(item for item in elements if str(getattr(item, "key", "")) == key)


def _selectbox(app, key):
    # stable_selectbox 的 key 由 localized_widget_key 加上语言后缀。
    return next(item for item in app.selectbox if str(item.key).startswith(key))


@contextmanager
def _webui(app_config, **llm_patches):
    ui_config = dict(config.ui, language="en", voice_mode="tts", tts_server="azure-tts-v1",
                     voice_name="en-US-JennyNeural-Female")
    with ExitStack() as stack:
        stack.enter_context(patch.object(config, "app", app_config))
        stack.enter_context(patch.object(config, "ui", ui_config))
        stack.enter_context(patch.object(config, "try_save_config", return_value=True))
        stack.enter_context(
            patch.object(voice, "get_all_azure_voices", return_value=["en-US-JennyNeural-Female"])
        )
        for name, options in llm_patches.items():
            stack.enter_context(patch.object(llm, name, **options))
        app = AppTest.from_file(str(ROOT_DIR / "webui" / "Main.py"), default_timeout=60)
        app.session_state["ui_language"] = "en"
        app.run()
        assert [str(item.value) for item in app.exception] == []
        yield app


WEBUI_CONFIG = {"llm_provider": "openai", "script_generation_backend": "local", "video_terms_style": "visual"}
GENERATED = {
    "generate_visual_terms": {"return_value": (["ferry"], ["istanbul", "turkey"])},
    "generate_terms": {"return_value": ["Istanbul ferry"]},
}


def test_webui_visual_keywords_fill_the_anchors():
    with _webui(dict(config.app, **WEBUI_CONFIG), **GENERATED) as app:
        assert _selectbox(app, "video_terms_style_select").value == "visual"
        _widget(app.text_area, "video_subject").set_value("Istanbul").run()
        _widget(app.text_area, "video_script").set_value("Ferries cross the Bosphorus.").run()
        _widget(app.button, "auto_generate_terms").click().run()

        assert (app.session_state["video_terms"], app.session_state["video_anchors"]) == ("ferry", "istanbul, turkey")
        assert llm.generate_terms.call_count == 0


def test_webui_script_and_visual_keywords_fill_the_anchors():
    patches = dict(GENERATED, generate_script={"return_value": "Ferries cross the Bosphorus."})
    with _webui(dict(config.app, **WEBUI_CONFIG), **patches) as app:
        _widget(app.text_area, "video_subject").set_value("Istanbul").run()
        _widget(app.button, "auto_generate_script").click().run()

        assert app.session_state["video_script"] == "Ferries cross the Bosphorus."
        assert (app.session_state["video_terms"], app.session_state["video_anchors"]) == ("ferry", "istanbul, turkey")
        assert llm.generate_visual_terms.call_args.kwargs["amount"] == 5


def test_webui_legacy_keywords_keep_the_anchors_and_save_the_style():
    app_config = dict(config.app, **WEBUI_CONFIG)
    with _webui(app_config, **GENERATED) as app:
        _selectbox(app, "video_terms_style_select").set_value("legacy").run()
        assert app_config["video_terms_style"] == "legacy"
        _widget(app.text_input, "video_anchors").set_value("bosphorus").run()
        _widget(app.text_area, "video_script").set_value("Ferries cross the Bosphorus.").run()
        _widget(app.button, "auto_generate_terms").click().run()

        assert (app.session_state["video_terms"], app.session_state["video_anchors"]) == ("Istanbul ferry", "bosphorus")
        assert llm.generate_visual_terms.call_count == 0

