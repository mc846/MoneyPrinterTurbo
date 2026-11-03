"""跨方向裁剪（crop_materials_to_aspect）：素材筛选、规格选择与搜索缓存。"""

import hashlib
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.config import config
from app.models.schema import MaterialInfo, VideoAspect, VideoConcatMode
from app.services import material, material_cache

PORTRAIT = VideoAspect.portrait
LANDSCAPE = VideoAspect.landscape
SQUARE = VideoAspect.square


@pytest.mark.parametrize(
    ("width", "height", "aspect", "crop_size", "accepted"),
    [
        # 9:16，目标 1080x1920
        (3840, 2160, PORTRAIT, (1215, 2160), True),
        (1920, 1080, PORTRAIT, (607, 1080), False),
        (3414, 1920, PORTRAIT, (1080, 1920), True),  # 恰好达到目标分辨率
        (3412, 1919, PORTRAIT, (1079, 1919), False),  # 低一个像素
        (1080, 1920, PORTRAIT, (1080, 1920), True),
        (1440, 1920, PORTRAIT, (1080, 1920), True),
        # 16:9，目标 1920x1080
        (2160, 3840, LANDSCAPE, (2160, 1215), True),
        (1080, 1920, LANDSCAPE, (1080, 607), False),
        (1920, 3414, LANDSCAPE, (1920, 1080), True),
        (1919, 3412, LANDSCAPE, (1919, 1079), False),
        # 1:1，目标 1080x1080
        (1920, 1080, SQUARE, (1080, 1080), True),
        (1080, 1920, SQUARE, (1080, 1080), True),
        (1079, 1920, SQUARE, (1079, 1079), False),
        (1920, 1079, SQUARE, (1079, 1079), False),
    ],
)
def test_center_crop_size_thresholds(width, height, aspect, crop_size, accepted):
    target_width, target_height = aspect.to_resolution()

    assert material._center_crop_size(width, height, aspect) == crop_size
    crop_width, crop_height = crop_size
    assert (crop_width >= target_width and crop_height >= target_height) is accepted


def _pixabay_hit(asset_id, **renditions):
    return {
        "id": asset_id,
        "duration": 8,
        "videos": {
            name: {
                "width": width,
                "height": height,
                "url": f"https://cdn.example/{asset_id}-{name}.mp4",
            }
            for name, (width, height) in renditions.items()
        },
    }


PIXABAY_HITS = [
    _pixabay_hit("landscape-4k", large=(3840, 2160), medium=(1920, 1080), small=(1280, 720)),
    _pixabay_hit("landscape-hd", large=(1920, 1080), medium=(1280, 720)),
    _pixabay_hit("portrait-4k", large=(2160, 3840), medium=(1080, 1920), small=(720, 1280)),
]


def _search_pixabay(**kwargs):
    response = SimpleNamespace(status_code=200, headers={}, json=lambda: {"hits": PIXABAY_HITS})
    with (
        patch.object(material, "get_api_key", return_value="fixture-key"),
        patch.object(material.requests, "get", return_value=response),
    ):
        return material.search_videos_pixabay("istanbul", 5, PORTRAIT, **kwargs)


def test_pixabay_accepts_croppable_footage_and_picks_smallest_rendition():
    items = _search_pixabay(crop_to_aspect=True)

    # 搜索函数本身保持 Pixabay 的结果顺序，"同方向优先"由 download_videos 负责。
    # 4K 横屏只有 large 规格裁剪后够用，竖屏素材选 medium（1080x1920）而不是 4K。
    assert [item.url for item in items] == [
        "https://cdn.example/landscape-4k-large.mp4",
        "https://cdn.example/portrait-4k-medium.mp4",
    ]
    assert [
        (item.source_info["rendition"]["width"], item.source_info["rendition"]["height"])
        for item in items
    ] == [(3840, 2160), (1080, 1920)]


def test_pixabay_without_crop_keeps_upstream_selection():
    items = _search_pixabay()

    assert [item.url for item in items] == ["https://cdn.example/portrait-4k-large.mp4"]


def _pexels_video(asset_id, *sizes):
    return {
        "id": asset_id,
        "duration": 8,
        "url": f"https://www.pexels.com/video/{asset_id}/",
        "user": {"id": 1, "name": "fixture"},
        "video_files": [
            {
                "id": f"{width}x{height}",
                "width": width,
                "height": height,
                "link": f"https://videos.example/{asset_id}-{width}x{height}.mp4",
            }
            for width, height in sizes
        ],
    }


PEXELS_VIDEOS = [
    _pexels_video("landscape-4k", (960, 540), (1920, 1080), (3840, 2160)),
    _pexels_video("landscape-hd", (1280, 720), (1920, 1080)),
    _pexels_video("portrait-4k", (720, 1280), (1080, 1920), (2160, 3840)),
]


def _search_pexels(**kwargs):
    requested_urls = []
    response = SimpleNamespace(status_code=200, headers={}, json=lambda: {"videos": PEXELS_VIDEOS})

    def fake_get(url, **_):
        requested_urls.append(url)
        return response

    with (
        patch.object(material, "get_api_key", return_value="fixture-key"),
        patch.object(material.requests, "get", side_effect=fake_get),
    ):
        items = material.search_videos_pexels("istanbul", 5, PORTRAIT, **kwargs)
    return items, requested_urls


def test_pexels_requests_all_orientations_and_picks_smallest_croppable_rendition():
    items, requested_urls = _search_pexels(crop_to_aspect=True)

    # 与 Pixabay 相同：搜索结果保持 Pexels 顺序，排序在 download_videos 中进行。
    assert "orientation=" not in requested_urls[0]
    assert [item.url for item in items] == [
        "https://videos.example/landscape-4k-3840x2160.mp4",
        "https://videos.example/portrait-4k-1080x1920.mp4",
    ]


def test_pexels_without_crop_keeps_upstream_query_and_selection():
    items, requested_urls = _search_pexels()

    assert "orientation=portrait" in requested_urls[0]
    assert [item.url for item in items] == ["https://videos.example/portrait-4k-1080x1920.mp4"]


def _cached_item(width, height):
    return MaterialInfo(
        provider="pixabay",
        url=f"https://cdn.example/{width}x{height}.mp4",
        duration=8,
        source_info={"provider": "pixabay", "rendition": {"id": "large", "width": width, "height": height}},
    )


def test_cached_items_are_filtered_with_the_same_crop_rule():
    items = [_cached_item(3840, 2160), _cached_item(1920, 1080), _cached_item(1080, 1920)]

    assert [item.url for item in material._filter_materials_by_aspect(items, PORTRAIT, crop_to_aspect=True)] == [
        "https://cdn.example/3840x2160.mp4",
        "https://cdn.example/1080x1920.mp4",
    ]
    assert [item.url for item in material._filter_materials_by_aspect(items, PORTRAIT)] == [
        "https://cdn.example/1080x1920.mp4",
    ]


def test_cache_key_is_unchanged_without_crop_and_separate_with_it():
    args = {"provider": "pixabay", "search_term": "istanbul", "minimum_duration": 5, "video_aspect": PORTRAIT}
    upstream_key = hashlib.sha256(
        json.dumps(
            {"provider": "pixabay", "search_term": "istanbul", "minimum_duration": 5, "video_aspect": "9:16"},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()

    assert material_cache._cache_key(**args) == upstream_key
    assert material_cache._cache_key(**args, crop_to_aspect=True) != upstream_key


def test_crop_results_are_cached_separately_and_reused(tmp_path):
    def fail_search(**_):
        raise AssertionError("cached crop results must be reused")

    plain_calls = []

    def plain_search(search_term, minimum_duration, video_aspect):
        plain_calls.append(search_term)
        return []

    common = {"provider": "pixabay", "search_term": "istanbul", "minimum_duration": 5, "video_aspect": PORTRAIT}
    with patch.object(material_cache.utils, "storage_dir", return_value=str(tmp_path)):
        assert material_cache.save_material_search_cache(
            **common, items=[_cached_item(3840, 2160)], crop_to_aspect=True
        )
        cached = material._search_videos_with_cache(search_videos=fail_search, crop_to_aspect=True, **common)
        # 关闭时使用原来的缓存键：没有命中，并按原来的三参数方式调用搜索。
        plain = material._search_videos_with_cache(search_videos=plain_search, **common)

    assert [item.url for item in cached] == ["https://cdn.example/3840x2160.mp4"]
    assert plain == [] and plain_calls == ["istanbul"]


def test_download_videos_ignores_crop_for_sources_without_support():
    seen_cache_kwargs = []

    def coverr_search(search_term, minimum_duration, video_aspect):
        return []

    def fake_load(**kwargs):
        seen_cache_kwargs.append(kwargs)

    with (
        patch.dict(config.app, {"material_directory": ""}),
        patch.object(material, "search_videos_coverr", side_effect=coverr_search),
        patch.object(material.material_cache, "load_material_search_cache", side_effect=fake_load),
        patch.object(material.task_artifacts, "patch_script_data", return_value=True),
    ):
        result = material.download_videos(
            task_id="coverr-crop", search_terms=["istanbul"], source="coverr",
            audio_duration=5, crop_to_aspect=True,
        )

    assert result == []
    assert seen_cache_kwargs and all("crop_to_aspect" not in kwargs for kwargs in seen_cache_kwargs)


# ---- 同方向优先：裁剪素材只在同方向素材不够用时使用 ----


def _clip(term, name, width, height):
    return MaterialInfo(
        provider="pixabay",
        url=f"https://cdn.example/{name}.mp4",
        duration=3,
        source_info={
            "provider": "pixabay", "search_term": term, "asset_id": name,
            "rendition": {"id": "large", "width": width, "height": height},
        },
    )


def native(term, name):
    return _clip(term, name, 1080, 1920)


def cropped(term, name):
    return _clip(term, name, 3840, 2160)


def _names(items):
    return [item.source_info["asset_id"] for item in items]


def test_split_keeps_provider_order_within_each_group():
    items = [cropped("t", "c1"), native("t", "n1"), cropped("t", "c2"), native("t", "n2")]

    native_items, cropped_items = material._split_by_native_orientation(items, PORTRAIT)

    assert (_names(native_items), _names(cropped_items)) == (["n1", "n2"], ["c1", "c2"])
    # 方形输出不区分方向，全部算作同方向，顺序不变。
    assert _names(material._split_by_native_orientation(items, SQUARE)[0]) == ["c1", "n1", "c2", "n2"]


def _attempted(results, audio_duration, *, crop=True, cached=None, **kwargs):
    attempted = []

    def fake_search(search_term, minimum_duration, video_aspect, **options):
        assert options == ({"crop_to_aspect": True} if crop else {})
        return results[search_term]

    def fake_save_video(video_url, save_dir=""):
        attempted.append(video_url.rsplit("/", 1)[-1].removesuffix(".mp4"))
        return f"/tmp/{video_url.rsplit('/', 1)[-1]}"

    with (
        patch.dict(config.app, {"material_directory": ""}),
        patch.object(material, "search_videos_pixabay", side_effect=fake_search),
        patch.object(material, "save_video", side_effect=fake_save_video),
        patch.object(material.material_cache, "load_material_search_cache", side_effect=lambda **_: cached),
        patch.object(material.material_cache, "save_material_search_cache"),
        patch.object(material.task_artifacts, "patch_script_data", return_value=True),
    ):
        material.download_videos(
            task_id="native-first", search_terms=list(results), source="pixabay",
            audio_duration=audio_duration, max_clip_duration=3, crop_to_aspect=crop, **kwargs,
        )
    return attempted


def test_script_order_uses_no_cropped_clip_when_native_candidates_suffice():
    results = {
        "bridge": [cropped("bridge", "c1"), native("bridge", "n1"), native("bridge", "n2")],
        "tower": [native("tower", "t1"), cropped("tower", "tc1")],
    }

    # 每段 3 秒、配音 8 秒需要 3 段：第 1 轮 n1、t1，第 2 轮 n2。按素材源顺序会先选 c1。
    assert _attempted(results, 8, match_script_order=True) == ["n1", "t1", "n2"]


def test_script_order_falls_back_to_cropped_clips_per_term():
    results = {
        "bridge": [cropped("bridge", "c1"), native("bridge", "n1"), cropped("bridge", "c2")],
        "tower": [cropped("tower", "tc1")],
    }

    # tower 没有同方向素材，第 1 轮直接用裁剪素材；bridge 用完 n1 后才轮到 c1。
    assert _attempted(results, 8, match_script_order=True) == ["n1", "tc1", "c1"]


def test_pool_uses_no_cropped_clip_when_native_candidates_suffice():
    results = {
        "bridge": [cropped("bridge", "c1"), native("bridge", "n1")],
        "tower": [cropped("tower", "tc1"), native("tower", "t1"), native("tower", "t2")],
    }

    assert _attempted(results, 8, video_concat_mode=VideoConcatMode.sequential) == ["n1", "t1", "t2"]


def test_pool_falls_back_to_cropped_clips_in_provider_order():
    results = {
        "bridge": [cropped("bridge", "c1"), native("bridge", "n1")],
        "tower": [cropped("tower", "tc1")],
    }

    # 整个候选池只有 3 秒同方向素材，不够 8 秒，再按原顺序使用裁剪素材。
    assert _attempted(results, 8, video_concat_mode=VideoConcatMode.sequential) == ["n1", "c1", "tc1"]


def test_random_pool_shuffles_each_group_separately():
    results = {
        "bridge": [cropped("bridge", "c1"), native("bridge", "n1")],
        "tower": [cropped("tower", "tc1"), native("tower", "t1"), native("tower", "t2")],
    }
    shuffled = []

    def fake_shuffle(items):
        shuffled.append(_names(items))
        items.reverse()

    with patch.object(material.random, "shuffle", side_effect=fake_shuffle):
        attempted = _attempted(results, 8, video_concat_mode=VideoConcatMode.random)

    assert shuffled == [["n1", "t1", "t2"], ["c1", "tc1"]]
    assert attempted == ["t2", "t1", "n1"]


def test_cached_provider_order_is_regrouped():
    # 旧缓存按素材源顺序保存，读出后同样要先排同方向素材。
    cached = [cropped("bridge", "c1"), native("bridge", "n1"), native("bridge", "n2")]

    assert _attempted({"bridge": []}, 5, cached=cached, match_script_order=True) == ["n1", "n2"]


def test_disabled_setting_keeps_provider_order_in_both_modes():
    results = {
        "bridge": [cropped("bridge", "c1"), native("bridge", "n1")],
        "tower": [cropped("tower", "tc1"), native("tower", "t1")],
    }
    shuffled = []

    def fake_shuffle(items):
        shuffled.append(_names(items))

    assert _attempted(results, 5, crop=False, match_script_order=True) == ["c1", "tc1"]
    with patch.object(material.random, "shuffle", side_effect=fake_shuffle):
        attempted = _attempted(results, 5, crop=False, video_concat_mode=VideoConcatMode.random)

    # 关闭时整个候选池只打乱一次，不分组。
    assert shuffled == [["c1", "n1", "tc1", "t1"]]
    assert attempted == ["c1", "n1"]
