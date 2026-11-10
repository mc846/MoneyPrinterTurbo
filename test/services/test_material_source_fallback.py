"""按关键词的后备库存素材源（video_source_fallback）：判定、来源不混用、缓存、署名和各层传递。不访问网络。"""

import os
import sys
import time
import tomllib
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

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
from app.services import material, material_cache, nasa  # noqa: E402
from app.services import task as tm  # noqa: E402
from app.services.state import MemoryState  # noqa: E402

PORTRAIT = VideoAspect.portrait
RANK = MaterialTagRelevance.rank


def _nasa(term, name, tags):
    return MaterialInfo(
        provider="nasa", url=f"https://images-assets.nasa.gov/image/{name}/{name}~orig.jpg", duration=5,
        source_info={"provider": "nasa", "search_term": term, "asset_id": name,
                     "source_page": f"https://images.nasa.gov/details/{name}", "credit": f"NASA/{name}",
                     "rendition": {"id": "orig", "width": 2160, "height": 3840},
                     "tags": tags, "ai_generated": False},
    )


def _pixabay(term, name, tags, *, ai=False):
    return MaterialInfo(
        provider="pixabay", url=f"https://cdn.example/{name}.mp4", duration=5,
        source_info={"provider": "pixabay", "search_term": term, "asset_id": name,
                     "source_page": f"https://pixabay.com/videos/{name}/",
                     "creator": {"id": "7", "name": f"maker-{name}"},
                     "rendition": {"id": "large", "width": 1080, "height": 1920},
                     "tags": tags, "ai_generated": ai},
    )


def _run(terms, nasa_results, pixabay_results, audio_duration, *, settings=None, faces=(), cache=False,
         nasa_error=False, pixabay_error=False, **kwargs):
    """以 NASA 为主素材源运行 download_videos；返回下载顺序、各素材源的搜索记录和来源记录。"""
    calls = {"nasa": [], "pixabay": []}
    rendered, saved, screened, sources = [], [], [], []

    def nasa_search(search_term, minimum_duration, video_aspect, **options):
        calls["nasa"].append(search_term)
        if nasa_error:
            raise RuntimeError("nasa is down")
        return list(nasa_results.get(search_term, []))

    def pixabay_search(search_term, minimum_duration, video_aspect, **options):
        calls["pixabay"].append(search_term)
        if pixabay_error:
            raise RuntimeError("pixabay is down")
        return list(pixabay_results.get(search_term, []))

    def fake_clip(item, save_dir="", **options):
        rendered.append(item.source_info["asset_id"])
        return f"/tmp/{item.source_info['asset_id']}.mp4"

    def fake_save_video(video_url, save_dir=""):
        name = video_url.rsplit("/", 1)[-1].removesuffix(".mp4")
        saved.append(name)
        return f"/tmp/{name}.mp4"

    def fake_screen(item, save_dir="", **options):
        screened.append(item.source_info["asset_id"])
        return item.source_info["asset_id"] not in faces

    real_load = material_cache.load_material_search_cache
    values = {"material_directory": "", "pixabay_api_keys": ["test-key"], **(settings or {})}
    with ExitStack() as stack:
        stack.enter_context(patch.dict(config.app, values))
        stack.enter_context(patch.object(nasa, "search_images", side_effect=nasa_search))
        stack.enter_context(patch.object(material, "search_videos_pixabay", side_effect=pixabay_search))
        stack.enter_context(patch.object(nasa, "save_image_clip", side_effect=fake_clip))
        stack.enter_context(patch.object(nasa, "face_filter_active", return_value=bool(faces)))
        stack.enter_context(patch.object(nasa, "screen_image", side_effect=fake_screen))
        stack.enter_context(patch.object(material, "save_video", side_effect=fake_save_video))
        stack.enter_context(patch.object(
            material.task_artifacts, "patch_script_data",
            side_effect=lambda task_id, material_sources: sources.extend(material_sources) or True))
        if cache:
            # Windows 上刚写入的缓存文件 mtime 可能略晚于当前时间，读取时稍往后取时间。
            stack.enter_context(patch.object(
                material.material_cache, "load_material_search_cache",
                side_effect=lambda **kw: real_load(**kw, now=time.time() + 5)))
        else:
            stack.enter_context(patch.object(material.material_cache, "load_material_search_cache",
                                             return_value=None))
            stack.enter_context(patch.object(material.material_cache, "save_material_search_cache"))
        paths = material.download_videos(
            task_id="fallback", search_terms=list(terms), source="nasa", audio_duration=audio_duration,
            max_clip_duration=5, **kwargs,
        )
    by_term = {}
    for record in sources:
        by_term.setdefault(record["search_term"], set()).add(record["provider"])
    return SimpleNamespace(paths=[os.path.basename(p).removesuffix(".mp4") for p in paths], calls=calls,
                           rendered=rendered, saved=saved, screened=screened, sources=sources, by_term=by_term)


ORDERED = {"match_script_order": True, "tag_relevance": RANK, "fallback_source": "pixabay"}


# ------------------------------------------------------------------ 判定


@pytest.mark.parametrize(
    ("tags", "threshold", "expected"),
    [
        (["black hole"], 0.5, 1),
        (["black"], 0.5, 1),  # 命中比例恰好等于阈值也算合格
        (["black"], 0.75, 0),
        (["galaxy"], 0.5, 0),
        (["black"], 0.0, 1),
    ],
)
def test_qualified_count_uses_the_relevance_threshold_inclusively(tags, threshold, expected):
    items = [_nasa("black hole", "a", tags)]

    assert material._qualified_candidate_count("black hole", items, RANK, threshold) == expected


def test_qualified_count_is_the_candidate_count_without_relevance_or_tags():
    unrelated = [_nasa("black hole", "a", ["galaxy"]), _nasa("black hole", "b", ["moon"])]
    untagged = [MaterialInfo(provider="pexels", url="https://cdn.example/p.mp4", duration=5,
                             source_info={"search_term": "black hole"})]

    assert material._qualified_candidate_count("black hole", unrelated, MaterialTagRelevance.off, 0.5) == 2
    assert material._qualified_candidate_count("black hole", untagged, RANK, 0.5) == 1
    assert material._qualified_candidate_count("black hole", [], RANK, 0.5) == 0


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        ({}, (0.5, 1)),
        ({"video_source_fallback_min_relevance": 0.75, "video_source_fallback_min_candidates": 3}, (0.75, 3)),
        ({"video_source_fallback_min_relevance": "0.25", "video_source_fallback_min_candidates": "2"}, (0.25, 2)),
        ({"video_source_fallback_min_relevance": 1.5, "video_source_fallback_min_candidates": 0}, (1.0, 1)),
        ({"video_source_fallback_min_relevance": -1, "video_source_fallback_min_candidates": 500}, (0.0, 50)),
        ({"video_source_fallback_min_relevance": "abc", "video_source_fallback_min_candidates": True}, (0.5, 1)),
        ({"video_source_fallback_min_relevance": float("nan"), "video_source_fallback_min_candidates": ""},
         (0.5, 1)),
    ],
)
def test_thresholds_from_config_fall_back_to_defaults_and_are_clamped(values, expected):
    keys = ("video_source_fallback_min_relevance", "video_source_fallback_min_candidates")
    base = {key: value for key, value in config.app.items() if key not in keys}
    with patch.object(config, "app", {**base, **values}):
        assert material.fallback_thresholds() == expected


def test_defaults_match_the_example_config():
    app = tomllib.loads((ROOT_DIR / "config.example.toml").read_text(encoding="utf-8"))["app"]

    assert app["video_source_fallback"] == ""
    assert app["video_source_fallback_min_relevance"] == material.DEFAULT_FALLBACK_MIN_RELEVANCE == 0.5
    assert app["video_source_fallback_min_candidates"] == material.DEFAULT_FALLBACK_MIN_CANDIDATES == 1


@pytest.mark.parametrize(
    ("source", "fallback", "keys", "expected"),
    [
        ("nasa", "pixabay", ["k"], "pixabay"),
        ("nasa", " Pixabay ", ["k"], "pixabay"),
        ("nasa", "", ["k"], ""),
        ("nasa", None, ["k"], ""),
        ("nasa", "none", ["k"], ""),
        ("nasa", "nasa", ["k"], ""),
        ("pixabay", "nasa", [], "nasa"),  # NASA 不需要 Key
        ("local", "pixabay", ["k"], ""),
        ("wavespeed", "pixabay", ["k"], ""),
        ("nasa", "wavespeed", ["k"], ""),
        ("nasa", "pixabay", [], ""),
        ("nasa", "pixabay", ["", " "], ""),
        ("nasa", "pixabay", "k", "pixabay"),
    ],
)
def test_resolve_fallback_source(source, fallback, keys, expected):
    with patch.dict(config.app, {"pixabay_api_keys": keys}):
        assert material.resolve_fallback_source(source, fallback) == expected


# ------------------------------------------------------------------ 按关键词切换，来源不混用


def test_term_with_enough_nasa_candidates_does_not_search_pixabay():
    result = _run(["black hole"], {"black hole": [_nasa("black hole", "bh", ["black hole"])]},
                  {"black hole": [_pixabay("black hole", "px", ["black hole"])]}, 4, **ORDERED)

    assert result.paths == ["bh"]
    assert result.calls["pixabay"] == []
    assert result.saved == []


def test_weak_terms_take_all_of_their_clips_from_pixabay():
    terms = ["black hole", "night sky timelapse", "pocket watch"]
    nasa_results = {
        "black hole": [_nasa("black hole", "bh", ["black hole"])],
        "pocket watch": [_nasa("pocket watch", "pw-nasa", ["clock tower"])],
    }
    pixabay_results = {
        "night sky timelapse": [_pixabay("night sky timelapse", "ns", ["night", "sky", "timelapse"])],
        "pocket watch": [_pixabay("pocket watch", "pw1", ["pocket watch"]),
                         _pixabay("pocket watch", "pw2", ["pocket", "watch"])],
    }

    # 24 秒需要 5 段，比全部候选还多：用完以后也不会拿 NASA 的 pw-nasa 补位
    result = _run(terms, nasa_results, pixabay_results, 24, **ORDERED)

    assert result.paths == ["bh", "ns", "pw1", "pw2"]
    assert result.calls["pixabay"] == ["night sky timelapse", "pocket watch"]
    assert result.rendered == ["bh"] and result.saved == ["ns", "pw1", "pw2"]
    assert result.by_term == {"black hole": {"nasa"}, "night sky timelapse": {"pixabay"},
                              "pocket watch": {"pixabay"}}


@pytest.mark.parametrize(
    ("nasa_tags", "settings", "uses_pixabay"),
    [
        # 最少 2 个合格候选：正好 2 个不切换，1 个切换
        ([["galaxy"], ["galaxy"]], {"video_source_fallback_min_candidates": 2}, False),
        ([["galaxy"], ["moon"]], {"video_source_fallback_min_candidates": 2}, True),
        # "spiral galaxy" 只命中 galaxy（0.5）：阈值 0.5 合格，0.75 不合格
        ([["galaxy"]], {"video_source_fallback_min_relevance": 0.5}, False),
        ([["galaxy"]], {"video_source_fallback_min_relevance": 0.75}, True),
    ],
)
def test_switch_boundaries(nasa_tags, settings, uses_pixabay):
    term = "galaxy" if settings.get("video_source_fallback_min_candidates") else "spiral galaxy"
    nasa_results = {term: [_nasa(term, f"n{i}", tags) for i, tags in enumerate(nasa_tags)]}
    pixabay_results = {term: [_pixabay(term, "p1", [term]), _pixabay(term, "p2", [term])]}

    result = _run([term], nasa_results, pixabay_results, 4, settings=settings, **ORDERED)

    assert result.by_term[term] == ({"pixabay"} if uses_pixabay else {"nasa"})


def test_pixabay_is_used_only_when_it_has_more_qualified_candidates():
    # NASA 有一张对题的白矮星图，Pixabay 只命中 "white"（0.5）：最少 2 个时两边都只有 1 个，保留 NASA
    nasa_results = {"white dwarf": [_nasa("white dwarf", "wd", ["white dwarf"])],
                    "stellar core": [_nasa("stellar core", "c1", ["galaxy"]), _nasa("stellar core", "c2", ["moon"])]}
    pixabay_results = {"white dwarf": [_pixabay("white dwarf", "bird", ["white", "bird"])],
                       "stellar core": [_pixabay("stellar core", "x1", ["fire"]),
                                        _pixabay("stellar core", "x2", ["smoke"])]}

    result = _run(["white dwarf", "stellar core"], nasa_results, pixabay_results, 9,
                  settings={"video_source_fallback_min_candidates": 2}, **ORDERED)

    # 两个关键词都到 Pixabay 搜索过，但 Pixabay 合格候选不比 NASA 多：都保留 NASA
    assert result.calls["pixabay"] == ["white dwarf", "stellar core"]
    assert result.by_term == {"white dwarf": {"nasa"}, "stellar core": {"nasa"}}


def test_relevance_off_counts_only_candidates():
    nasa_results = {"star": [_nasa("star", "s", ["moon"])]}
    pixabay_results = {"star": [_pixabay("star", "p", ["star"])], "comet": [_pixabay("comet", "c", ["comet"])]}

    result = _run(["star", "comet"], nasa_results, pixabay_results, 9,
                  match_script_order=True, fallback_source="pixabay")

    # 相关度关闭：NASA 的 "star" 候选虽然标签不对，也算 1 个；"comet" 在 NASA 没有候选
    assert result.by_term == {"star": {"nasa"}, "comet": {"pixabay"}}
    assert result.calls["pixabay"] == ["comet"]


@pytest.mark.parametrize("keys", [[], [""]])
def test_missing_pixabay_key_disables_the_fallback_with_one_warning(keys):
    terms = ["night sky timelapse", "pocket watch", "black hole"]
    with patch.object(material.logger, "warning") as warning:
        result = _run(terms, {"black hole": [_nasa("black hole", "bh", ["black hole"])]},
                      {"pocket watch": [_pixabay("pocket watch", "pw", ["pocket watch"])]}, 9,
                      settings={"pixabay_api_keys": keys}, **ORDERED)

    assert result.paths == ["bh"] and result.calls["pixabay"] == []
    messages = [str(call.args[0]) for call in warning.call_args_list]
    assert sum("video_source_fallback" in message for message in messages) == 1
    assert any("pixabay_api_keys is not set" in message for message in messages)


def test_even_spread_still_picks_terms_and_keeps_their_positions():
    terms = [f"t{i}" for i in range(5)]
    nasa_results = {term: [_nasa(term, f"{term}-nasa", [term])] for term in terms[:4]}
    pixabay_results = {"t4": [_pixabay("t4", "t4-px", ["t4"])]}

    # 9 秒需要 2 段：等间距选第一个和最后一个关键词；最后一个来自 Pixabay，仍在最后
    result = _run(terms, nasa_results, pixabay_results, 9, **ORDERED)

    assert result.paths == ["t0-nasa", "t4-px"]
    assert result.calls["pixabay"] == ["t4"]


def test_pooled_mode_merges_terms_from_both_sources_but_never_within_a_term():
    nasa_results = {"black hole": [_nasa("black hole", "bh1", ["black hole"]), _nasa("black hole", "bh2", ["hole"])],
                    "pocket watch": [_nasa("pocket watch", "pw-nasa", ["clock"])]}
    pixabay_results = {"pocket watch": [_pixabay("pocket watch", "pw1", ["pocket watch"])]}

    result = _run(["black hole", "pocket watch"], nasa_results, pixabay_results, 30,
                  video_concat_mode=VideoConcatMode.sequential, tag_relevance=RANK, fallback_source="pixabay")

    # 合并候选池统一按相关度排序（bh1、pw1 全部命中，bh2 一半），没有 pw-nasa
    assert result.paths == ["bh1", "pw1", "bh2"]
    assert result.rendered == ["bh1", "bh2"] and result.saved == ["pw1"]
    assert result.by_term == {"black hole": {"nasa"}, "pocket watch": {"pixabay"}}


def test_pixabay_clips_are_not_face_screened_and_nasa_images_still_are():
    nasa_results = {"galaxy": [_nasa("galaxy", "face", ["galaxy"]), _nasa("galaxy", "g", ["galaxy"])]}
    pixabay_results = {"comet": [_pixabay("comet", "c", ["comet"])]}

    result = _run(["galaxy", "comet"], nasa_results, pixabay_results, 9, faces={"face"}, **ORDERED)

    assert result.paths == ["g", "c"]
    assert result.screened == ["face", "g"]


def test_pixabay_uses_its_own_ai_setting():
    pixabay_results = {"comet": [_pixabay("comet", "ai", ["comet"], ai=True), _pixabay("comet", "real", ["comet"])]}

    result = _run(["comet"], {}, pixabay_results, 4, exclude_ai_generated=True, **ORDERED)

    assert result.paths == ["real"]


def test_search_errors_fall_through_to_the_other_source():
    pixabay_results = {"comet": [_pixabay("comet", "c", ["comet"])]}
    assert _run(["comet"], {}, pixabay_results, 4, nasa_error=True, **ORDERED).paths == ["c"]

    nasa_results = {"comet": [_nasa("comet", "n", ["moon"])]}
    assert _run(["comet"], nasa_results, {}, 4, pixabay_error=True, **ORDERED).paths == ["n"]


@pytest.mark.parametrize("options", [{}, {"fallback_source": ""}, {"fallback_source": "none"}])
def test_without_a_fallback_pixabay_is_never_searched(options):
    nasa_results = {"pocket watch": [_nasa("pocket watch", "pw-nasa", ["clock"])]}

    result = _run(["pocket watch", "comet"], nasa_results, {"comet": [_pixabay("comet", "c", ["comet"])]}, 9,
                  match_script_order=True, tag_relevance=RANK, **options)

    assert result.paths == ["pw-nasa"] and result.calls["pixabay"] == []


def test_fallback_decision_is_the_same_from_the_cache():
    # 关键词只在这个测试里使用，避免与其它测试共用缓存条目
    terms = ["fallback cache strong", "fallback cache weak"]
    nasa_results = {"fallback cache strong": [_nasa("fallback cache strong", "strong", ["fallback cache strong"])],
                    "fallback cache weak": [_nasa("fallback cache weak", "weak-nasa", ["moon"])]}
    pixabay_results = {"fallback cache weak": [_pixabay("fallback cache weak", "weak-px", ["fallback cache weak"])]}

    first = _run(terms, nasa_results, pixabay_results, 9, cache=True, **ORDERED)
    second = _run(terms, nasa_results, pixabay_results, 9, cache=True, **ORDERED)

    assert first.paths == second.paths == ["strong", "weak-px"]
    assert first.calls == {"nasa": terms, "pixabay": ["fallback cache weak"]}
    # 第二次两个素材源都从各自的缓存读取，判定相同
    assert second.calls == {"nasa": [], "pixabay": []}
    later = time.time() + 5
    pixabay_cached = material_cache.load_material_search_cache(
        "pixabay", "fallback cache weak", 5, PORTRAIT, now=later)
    nasa_cached = material_cache.load_material_search_cache(
        "nasa", "fallback cache weak", 5, PORTRAIT, variant=nasa.search_cache_variant(), now=later)
    assert [item.provider for item in pixabay_cached] == ["pixabay"]
    assert [item.provider for item in nasa_cached] == ["nasa"]


# ------------------------------------------------------------------ 署名


SOURCES = [
    {"provider": "nasa", "asset_id": "PIA15427", "search_term": "galaxy", "credit": "NASA/JPL-Caltech",
     "source_page": "https://images.nasa.gov/details/PIA15427"},
    {"provider": "pixabay", "asset_id": "381127", "search_term": "pocket watch",
     "source_page": "https://pixabay.com/videos/pocket-watch-381127/", "creator": {"id": "7", "name": "Kanenori"}},
    {"provider": "pixabay", "asset_id": "381127", "search_term": "pocket watch",
     "source_page": "https://pixabay.com/videos/pocket-watch-381127/", "creator": {"id": "7", "name": "Kanenori"}},
    {"provider": "pixabay", "asset_id": "5", "search_term": "comet"},
]


def test_credits_name_the_source_of_every_clip_and_link_pixabay_pages():
    lines = nasa.credits_text(SOURCES).splitlines()

    assert lines[3:8] == [
        "1. [NASA] NASA/JPL-Caltech",
        "   https://images.nasa.gov/details/PIA15427",
        "2. [Pixabay] Kanenori",
        "   https://pixabay.com/videos/pocket-watch-381127/",
        "3. [Pixabay] Pixabay",
    ]
    assert "4." not in "\n".join(lines)
    assert "Görüntüler: NASA ve ortakları, Pixabay / Images: NASA and partners, Pixabay" in lines


def test_pixabay_only_tasks_keep_writing_no_credits_file(tmp_path):
    pixabay_only = SOURCES[1:]

    assert nasa.credits_text(pixabay_only) == ""
    assert nasa.write_credits_file(str(tmp_path), pixabay_only) == ""
    assert not (tmp_path / "credits.txt").exists()


@pytest.mark.parametrize(
    ("source", "fallback", "sources", "expected"),
    [
        ("nasa", "pixabay", SOURCES, "Görüntüler: NASA, JPL"),
        # 所有关键词都改用了 Pixabay：没有 NASA 图片就不显示
        ("nasa", "pixabay", SOURCES[1:], None),
        ("pixabay", "nasa", SOURCES, "Görüntüler: NASA, JPL"),
        ("pixabay", "", SOURCES, None),
    ],
)
def test_credit_overlay_appears_only_with_nasa_images(source, fallback, sources, expected):
    params = VideoParams(video_subject="t", video_source=source, video_source_fallback=fallback,
                         video_language="tr-TR", nasa_credit_overlay=True)
    with (
        patch.object(tm, "_load_material_sources", return_value=sources),
        patch.object(tm.video, "combine_videos"),
        patch.object(tm.video, "generate_video", return_value=True) as generate_video,
        patch.object(tm.sm, "state", MemoryState()),
    ):
        tm.generate_final_videos(task_id="fallback-credit", params=params, downloaded_videos=["a.mp4"],
                                 audio_file="audio.mp3", subtitle_path="", audio_duration=5)

    assert generate_video.call_args.kwargs.get("credit_text") == expected


# ------------------------------------------------------------------ 任务层


def _materials_call(params):
    with (
        patch.object(tm.material, "download_videos", return_value=["a.mp4"]) as download,
        patch.object(tm.sm, "state", MemoryState()),
    ):
        assert tm.get_video_materials("fallback-task", params, ["comet"], 5) == ["a.mp4"]
    return download.call_args.kwargs


def test_task_passes_the_fallback_source_only_when_set():
    with_fallback = _materials_call(VideoParams(video_subject="t", video_source="nasa",
                                                video_source_fallback="pixabay"))
    without = _materials_call(VideoParams(video_subject="t", video_source="nasa"))

    # The task passes the chain as a list (one source or several, in order).
    assert with_fallback["fallback_source"] == ["pixabay"]
    assert "fallback_source" not in without


def test_task_helpers_tolerate_params_without_the_field():
    assert tm._video_source_fallback(SimpleNamespace(video_source="nasa")) == []
    assert tm._may_use_nasa(VideoParams(video_subject="t", video_source="pixabay", video_source_fallback="nasa"))
    assert not tm._may_use_nasa(VideoParams(video_subject="t", video_source="pixabay"))


@pytest.mark.parametrize(("fallback", "forwarded"), [("nasa", True), ("", False)])
def test_nasa_fallback_also_gets_the_script_attribution_rule(fallback, forwarded):
    params = VideoParams(video_subject="Kara delikler", video_source="pixabay", video_source_fallback=fallback)
    with patch.object(tm.llm, "generate_script", return_value="Metin.") as generate:
        tm.generate_script("task", params)

    assert ("video_source" in generate.call_args.kwargs) is forwarded


@pytest.mark.parametrize(
    ("source", "fallback", "expected"),
    [("coverr", "pixabay", True), ("coverr", "", False), ("nasa", "coverr", True), ("coverr", "pexels", True)],
)
def test_cross_orientation_crop_is_requested_when_the_fallback_can_crop(source, fallback, expected):
    params = VideoParams(video_subject="t", video_source=source, video_source_fallback=fallback,
                         video_fit_mode="contain", crop_materials_to_aspect=True)
    with (
        patch.object(tm.video, "combine_videos") as combine_videos,
        patch.object(tm.video, "generate_video"),
        patch.object(tm.sm, "state", MemoryState()),
    ):
        tm.generate_final_videos(task_id="fallback-crop", params=params, downloaded_videos=["a.mp4"],
                                 audio_file="audio.mp3", subtitle_path="", audio_duration=5)

    assert combine_videos.call_args.kwargs.get("crop_cross_orientation_clips", False) is expected


def test_preflight_rejects_an_unknown_fallback_source():
    params = TaskVideoRequest(video_subject="Coffee", video_source="nasa", video_source_fallback="flickr",
                              subtitle_enabled=False)
    with (
        patch.object(tm.sm, "state", MemoryState()),
        patch.object(tm.utils, "check_ffmpeg_ready", return_value=True),
        patch.object(tm, "generate_script") as script,
    ):
        result = tm.start("fallback-preflight", params)

    assert result["failed_stage"] == "preflight" and "fallback" in result["error"]
    script.assert_not_called()


def test_api_model_accepts_the_fallback_and_defaults_to_none():
    assert TaskVideoRequest(video_subject="t").video_source_fallback == ""
    body = TaskVideoRequest.model_validate({"video_subject": "t", "video_source": "nasa",
                                            "video_source_fallback": "pixabay"})
    assert body.model_dump()["video_source_fallback"] == "pixabay"


# ------------------------------------------------------------------ CLI 和 Skill


def _cli_params(*flags):
    with patch.dict(config.ui, {}, clear=True):
        return cli.build_video_params(cli.parse_args(["--video-subject", "t", "--video-source", "nasa", *flags]))


@pytest.mark.parametrize(
    ("flags", "expected"),
    [([], ""), (["--video-source-fallback", "pixabay"], "pixabay"), (["--video-source-fallback", "none"], "")],
)
def test_cli_flag_sets_the_fallback(flags, expected):
    assert _cli_params(*flags).video_source_fallback == expected


@pytest.mark.parametrize(
    "argv",
    [
        ["--video-source", "nasa", "--video-source-fallback", "nasa"],
        ["--video-source", "local", "--video-materials", "a.mp4", "--video-source-fallback", "pixabay"],
    ],
)
def test_cli_rejects_invalid_fallback_combinations(argv):
    with patch.dict(config.ui, {}, clear=True), patch("app.services.task.start") as start:
        assert cli.run_cli(["--video-subject", "t", *argv]) == 2
    start.assert_not_called()


def test_cli_batch_validation_rejects_the_same_source_as_fallback():
    params = VideoParams(video_subject="t", video_source="pixabay", video_source_fallback="pixabay")

    with pytest.raises(ValueError, match="differ"):
        cli._validate_batch_task_params(
            params, stop_at="video", custom_position_is_explicit=False, wavespeed_charge_confirmed=False,
            seedance_charge_confirmed=False, ofox_charge_confirmed=False,
            metaso_minimax_charge_confirmed=False, muapi_charge_confirmed=False,
        )


def test_cli_help_and_source_lists_stay_aligned(capsys):
    with pytest.raises(SystemExit):
        cli.parse_args(["--help"])

    assert "--video-source-fallback" in capsys.readouterr().out
    assert cli._FALLBACK_VIDEO_SOURCES == material.FALLBACK_VIDEO_SOURCES


def _agent():
    import importlib.util

    spec = importlib.util.spec_from_file_location("mpt_agent_fallback", ROOT_DIR / "docs" / "skill" / "mpt_agent.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_agent_reports_a_missing_fallback_key_without_blocking(tmp_path, capsys):
    agent = _agent()
    config_path = tmp_path / "config.toml"
    config_path.write_text('llm_provider = "ollama"\npixabay_api_keys = []\n', encoding="utf-8")

    provider, missing = agent.missing_config(
        config_path, ["--video-source", "nasa", "--video-source-fallback", "pixabay"])

    assert (provider, missing) == ("ollama", [])
    assert "pixabay_api_keys is not set" in capsys.readouterr().out
    assert agent.FALLBACK_SOURCES == set(material.FALLBACK_VIDEO_SOURCES)
    with pytest.raises(agent.SkillError):
        agent.missing_config(config_path, ["--video-source", "nasa", "--video-source-fallback", "nasa"])
    with pytest.raises(agent.SkillError):
        agent.missing_config(config_path, ["--video-source", "nasa", "--video-source-fallback=wavespeed"])
