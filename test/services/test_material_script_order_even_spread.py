"""按文案顺序匹配时，配音所需片段少于关键词数量的等间距选择。"""

from unittest.mock import patch

import pytest

from app.config import config
from app.models.schema import MaterialInfo, MaterialTagRelevance, VideoConcatMode
from app.services import material


def _clip(term, name, *, duration=5, landscape=False, tags=None):
    width, height = (3840, 2160) if landscape else (1080, 1920)
    return MaterialInfo(
        provider="pixabay",
        url=f"https://cdn.example/{name}.mp4",
        duration=duration,
        source_info={
            "provider": "pixabay", "search_term": term, "asset_id": name,
            "rendition": {"id": "large", "width": width, "height": height},
            "tags": tags if tags is not None else [term], "ai_generated": False,
        },
    )


def _terms(count, *, duration=5, per_term=2):
    return {
        f"t{index}": [_clip(f"t{index}", f"t{index}{'ab'[c]}", duration=duration) for c in range(per_term)]
        for index in range(count)
    }


def _run(results, audio_duration, *, failing=(), match_script_order=True, **kwargs):
    def fake_search(search_term, minimum_duration, video_aspect, **options):
        return results[search_term]

    def fake_save_video(video_url, save_dir=""):
        name = video_url.rsplit("/", 1)[-1].removesuffix(".mp4")
        return "" if name in failing else f"/tmp/{name}.mp4"

    with (
        patch.dict(config.app, {"material_directory": ""}),
        patch.object(material, "search_videos_pixabay", side_effect=fake_search),
        patch.object(material, "save_video", side_effect=fake_save_video),
        patch.object(material.material_cache, "load_material_search_cache", return_value=None),
        patch.object(material.material_cache, "save_material_search_cache"),
        patch.object(material.task_artifacts, "patch_script_data", return_value=True),
    ):
        paths = material.download_videos(
            task_id="even-spread", search_terms=list(results), source="pixabay",
            audio_duration=audio_duration, max_clip_duration=5,
            match_script_order=match_script_order, **kwargs,
        )
    return [path.rsplit("/", 1)[-1].removesuffix(".mp4") for path in paths]


@pytest.mark.parametrize(
    ("count", "needed", "expected"),
    [
        (8, 6, [0, 1, 3, 4, 6, 7]),
        (8, 2, [0, 7]),
        (8, 1, [0]),
        (7, 4, [0, 2, 4, 6]),
        (10, 4, [0, 3, 6, 9]),
        (6, 3, [0, 2, 5]),
        (5, 3, [0, 2, 4]),
        (2, 1, [0]),
        (8, 8, list(range(8))),
        (5, 9, list(range(5))),
        (1, 1, [0]),
    ],
)
def test_even_spread_indices(count, needed, expected):
    assert material._even_spread_indices(count, needed) == expected


@pytest.mark.parametrize(("count", "needed"), [(n, k) for n in range(2, 13) for k in range(2, n)])
def test_even_spread_keeps_first_and_last_and_order(count, needed):
    indices = material._even_spread_indices(count, needed)

    assert len(indices) == needed
    assert indices[0] == 0 and indices[-1] == count - 1
    assert indices == sorted(set(indices))


def test_fewer_clips_than_terms_uses_evenly_spaced_terms_first():
    # 26.9 秒配音、5 秒片段：需要 6 段，8 个关键词中等间距选 6 个，最后一个关键词保留。
    assert _run(_terms(8), 26.9) == ["t0a", "t1a", "t3a", "t4a", "t6a", "t7a"]


def test_one_clip_uses_the_first_term():
    assert _run(_terms(5), 4) == ["t0a"]


@pytest.mark.parametrize(
    ("count", "audio", "expected"),
    [
        # k > n：与原有轮询完全一致。
        (3, 26, ["t0a", "t1a", "t2a", "t0b", "t1b", "t2b"]),
        # k = n：同样不变；30 秒恰好被 6 段覆盖时仍按原规则多取一段。
        (6, 30, ["t0a", "t1a", "t2a", "t3a", "t4a", "t5a", "t0b"]),
    ],
)
def test_enough_clips_for_every_term_keeps_round_robin(count, audio, expected):
    with patch.object(material, "_even_spread_indices", wraps=material._even_spread_indices) as spread:
        assert _run(_terms(count), audio) == expected

    spread.assert_not_called()


def test_short_clips_use_reserves_in_their_order_before_a_second_round():
    # 12 秒、6 个关键词：k=3 → 选 t0、t2、t5；2 秒的短素材不够，第一轮按顺序接着用后备
    # t1、t3、t4，仍不足时才进入第二轮。
    assert _run(_terms(6, duration=2), 12) == ["t0a", "t2a", "t5a", "t1a", "t3a", "t4a", "t0b"]


def test_failed_download_is_replaced_by_the_first_unused_reserve():
    # 14 秒、5 个关键词：k=3 → t0、t2、t4。t2 下载失败后先用后备 t1，而不是 t0 的第二个候选。
    assert _run(_terms(5), 14, failing={"t2a"}) == ["t0a", "t4a", "t1a"]


def test_failed_single_clip_falls_back_to_the_next_term():
    assert _run(_terms(5), 4, failing={"t0a"}) == ["t1a"]


def test_pooled_mode_is_not_spread():
    with patch.object(material, "_even_spread_indices", wraps=material._even_spread_indices) as spread:
        paths = _run(_terms(8), 12, match_script_order=False, video_concat_mode=VideoConcatMode.sequential)

    assert paths == ["t0a", "t0b", "t1a"]
    spread.assert_not_called()


def test_spreading_keeps_cross_term_deduplication():
    shared = _clip("t0", "shared")
    results = _terms(5)
    results["t0"] = [shared, *results["t0"]]
    results["t4"] = [_clip("t4", "shared"), *results["t4"]]

    # k=3 → t0、t2、t4；t4 的首个候选已被 t0 选中，改用它自己的下一个候选。
    assert _run(results, 14) == ["shared", "t2a", "t4a"]


def test_spreading_keeps_native_orientation_first_and_tag_relevance():
    results = {
        f"t{index}": [
            _clip(f"t{index}", f"t{index}-landscape", landscape=True),
            _clip(f"t{index}", f"t{index}-off-topic", tags=["beach"]),
            _clip(f"t{index}", f"t{index}-native"),
        ]
        for index in range(5)
    }

    paths = _run(results, 14, crop_to_aspect=True, tag_relevance=MaterialTagRelevance.rank)

    # 相关度 > 方向 > 素材源顺序：每个选中关键词都先用标签匹配的竖屏素材。
    assert paths == ["t0-native", "t2-native", "t4-native"]
