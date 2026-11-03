"""脚本顺序素材匹配的跨关键词去重：只排除已被选中下载的素材。"""

from unittest.mock import patch

from app.config import config
from app.models.schema import VideoConcatMode
from app.services import material


def _item(term, name, duration=3):
    return material.MaterialInfo(
        provider="pexels",
        url=f"https://v.example/{name}.mp4",
        duration=duration,
        source_info={"provider": "pexels", "search_term": term, "asset_id": name},
    )


def _download(search_results, audio_duration, **kwargs):
    attempted_urls = []

    def fake_search(search_term, minimum_duration, video_aspect):
        return search_results[search_term]

    def fake_save_video(video_url, save_dir=""):
        attempted_urls.append(video_url)
        return f"/tmp/{video_url.rsplit('/', 1)[-1]}"

    with (
        patch.dict(config.app, {"material_directory": ""}),
        patch.object(material, "search_videos_pexels", side_effect=fake_search),
        patch.object(material, "save_video", side_effect=fake_save_video),
        patch.object(
            material.material_cache, "load_material_search_cache", return_value=None
        ),
        patch.object(material.material_cache, "save_material_search_cache"),
        patch.object(
            material.task_artifacts, "patch_script_data", return_value=True
        ) as patch_script,
    ):
        result = material.download_videos(
            task_id="script-order-dedup",
            search_terms=list(search_results),
            source="pexels",
            audio_duration=audio_duration,
            max_clip_duration=3,
            **kwargs,
        )
    sources = patch_script.call_args.kwargs["material_sources"]
    return result, attempted_urls, sources


def test_later_term_keeps_candidate_an_earlier_term_only_listed():
    """
    回归：前一个关键词只是在候选列表里出现过某个素材、并未选中它时，
    后续关键词仍然可以使用它。修复前整份候选列表都会进入去重集合，
    "tower" 的最佳匹配 shared 被提前剔除，只能退而使用 t2。
    """
    result, _, sources = _download(
        {
            "city": [_item("city", "a1"), _item("city", "shared"), _item("city", "a3")],
            "tower": [_item("tower", "shared"), _item("tower", "t2")],
        },
        audio_duration=8,
        match_script_order=True,
    )

    assert result == ["/tmp/a1.mp4", "/tmp/shared.mp4", "/tmp/a3.mp4"]
    assert [source["search_term"] for source in sources] == ["city", "tower", "city"]


def test_script_order_never_downloads_the_same_url_twice():
    """同一轮里两个关键词的当前候选相同时，后一个关键词改用自己的下一个候选。"""
    result, attempted_urls, sources = _download(
        {
            "city": [_item("city", "shared"), _item("city", "a2")],
            "tower": [_item("tower", "shared"), _item("tower", "t2")],
        },
        audio_duration=8,
        match_script_order=True,
    )

    assert len(attempted_urls) == len(set(attempted_urls))
    assert result == ["/tmp/shared.mp4", "/tmp/t2.mp4", "/tmp/a2.mp4"]
    assert [source["search_term"] for source in sources] == ["city", "tower", "city"]


def test_default_pool_still_dedupes_across_all_terms():
    """默认的合并候选池不受影响：重复素材只保留第一次出现的位置。"""
    result, attempted_urls, _ = _download(
        {
            "city": [_item("city", "a1"), _item("city", "shared")],
            "tower": [_item("tower", "shared"), _item("tower", "t2")],
        },
        audio_duration=20,
        video_concat_mode=VideoConcatMode.sequential,
    )

    assert attempted_urls == [
        "https://v.example/a1.mp4",
        "https://v.example/shared.mp4",
        "https://v.example/t2.mp4",
    ]
    assert result == ["/tmp/a1.mp4", "/tmp/shared.mp4", "/tmp/t2.mp4"]
