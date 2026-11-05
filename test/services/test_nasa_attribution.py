"""NASA 素材的缓存键、署名（credits.txt、屏幕署名、发布文案）和文案署名规则。不访问网络。"""

import json
import sys
import time
import tomllib
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT_DIR = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT_DIR))

import cli  # noqa: E402
from app.config import config  # noqa: E402
from app.controllers.v1 import llm as llm_controller  # noqa: E402
from app.models.schema import MaterialInfo, VideoAspect, VideoParams, VideoScriptRequest  # noqa: E402
from app.services import llm, material, material_cache, nasa, video  # noqa: E402
from app.services import task as tm  # noqa: E402
from app.services.state import MemoryState  # noqa: E402
from app.utils import utils  # noqa: E402

PORTRAIT = VideoAspect.portrait
SOURCES = [
    {"provider": "nasa", "asset_id": "PIA14415", "search_term": "nebula cloud",
     "credit": "X-ray: NASA/CXC/PSU/L.Townsley et al.; Optical: NASA/STScI",
     "source_page": "https://images.nasa.gov/details/PIA14415"},
    {"provider": "nasa", "asset_id": "GSFC_20171208_Archive_e002154", "search_term": "spiral galaxy",
     "credit": "NASA, ESA, and The Hubble Heritage Team (STScI/AURA)",
     "source_page": "https://images.nasa.gov/details/GSFC_20171208_Archive_e002154"},
    {"provider": "nasa", "asset_id": "PIA15427", "search_term": "elliptical galaxy", "credit": "NASA/JPL-Caltech",
     "source_page": "https://images.nasa.gov/details/PIA15427"},
    # 同一张图片第二轮又用了一次：署名只列一次
    {"provider": "nasa", "asset_id": "PIA15427", "search_term": "elliptical galaxy", "credit": "NASA/JPL-Caltech",
     "source_page": "https://images.nasa.gov/details/PIA15427"},
]
# 署名文字不能暗示 NASA 认可或参与制作（结尾的否认声明除外）
ENDORSEMENT_WORDING = ("presents", "presented by", "in partnership", "sunar", "ile birlikte", "with nasa")


# ------------------------------------------------------------------ 缓存键


def test_nasa_cache_key_always_carries_the_filter_version_and_settings():
    variant = nasa.search_cache_variant()
    assert variant == {"filters": nasa.FILTER_VERSION, "face_filter": True,
                       "text_filter": nasa.text_filter_enabled(), "media_types": ["image"]}

    def key(**kwargs):
        return material_cache._cache_key("nasa", "black hole", 5, PORTRAIT, crop_to_aspect=True, **kwargs)

    # 即使设置都是默认值也和加入版本之前的键不同，旧规则的缓存不会被继续使用
    assert key(variant=variant) != key()
    with patch.dict(config.app, {"nasa_face_filter": False}):
        assert key(variant=nasa.search_cache_variant()) != key(variant=variant)
    with patch.object(nasa, "FILTER_VERSION", nasa.FILTER_VERSION + 1):
        assert key(variant=nasa.search_cache_variant()) != key(variant=variant)
    # 没有 variant 时其它来源的键格式不变
    assert material_cache._cache_key("pixabay", "sky", 5, PORTRAIT) == material_cache._cache_key(
        "pixabay", "sky", 5, PORTRAIT, variant=None)


def _search_with_cache(provider, remote):
    return material._search_videos_with_cache(
        provider=provider, search_videos=remote, search_term="black hole", minimum_duration=5,
        video_aspect=PORTRAIT, crop_to_aspect=True,
    )


def test_cache_from_before_the_filter_version_is_not_reused():
    old = MaterialInfo(provider="nasa", url="https://images-assets.nasa.gov/image/old/old~orig.jpg", duration=5,
                       source_info={"provider": "nasa", "asset_id": "old", "tags": ["black hole"]})
    new = MaterialInfo(provider="nasa", url="https://images-assets.nasa.gov/image/new/new~orig.jpg", duration=5,
                       source_info={"provider": "nasa", "asset_id": "new", "tags": ["black hole"]})
    # 旧版本没有 variant 的缓存条目
    material_cache.save_material_search_cache("nasa", "black hole", 5, PORTRAIT, [old], crop_to_aspect=True)
    calls = []

    def remote(search_term, minimum_duration, video_aspect, **options):
        calls.append(search_term)
        return [new]

    assert [i.url for i in _search_with_cache("nasa", remote)] == [new.url]
    assert calls == ["black hole"]
    # 新结果按带版本的键保存。Windows 上刚写入的文件 mtime 可能略晚于当前时间，读取时稍往后取时间。
    later = time.time() + 5
    cached = material_cache.load_material_search_cache(
        "nasa", "black hole", 5, PORTRAIT, crop_to_aspect=True, variant=nasa.search_cache_variant(), now=later)
    assert [i.url for i in cached] == [new.url]
    old_format = material_cache.load_material_search_cache(
        "nasa", "black hole", 5, PORTRAIT, crop_to_aspect=True, now=later)
    assert [i.url for i in old_format] == [old.url]


def test_only_nasa_passes_a_cache_variant():
    seen = []

    def load(**kwargs):
        seen.append((kwargs["provider"], kwargs.get("variant")))
        return [MaterialInfo(provider=kwargs["provider"], url="https://cdn.example/a.mp4", duration=5)]

    with patch.object(material.material_cache, "load_material_search_cache", side_effect=load):
        _search_with_cache("pixabay", lambda *a, **k: [])
        _search_with_cache("nasa", lambda *a, **k: [])

    # 每次搜索在加锁前后各读一次缓存
    assert dict(seen) == {"pixabay": None, "nasa": nasa.search_cache_variant()}


# ------------------------------------------------------------------ credits.txt


def test_credits_text_lists_each_image_with_its_source_and_page():
    text = nasa.credits_text(SOURCES)

    assert text.splitlines()[:3] == ["Görsel kaynakları / Image credits", "=" * 33, ""]
    assert "1. [NASA] X-ray: NASA/CXC/PSU/L.Townsley et al.; Optical: NASA/STScI" in text
    assert "   https://images.nasa.gov/details/GSFC_20171208_Archive_e002154" in text
    assert text.count("PIA15427") == 1 and "3. [NASA] NASA/JPL-Caltech" in text and "4." not in text
    assert "Görüntüler: NASA ve ortakları / Images: NASA and partners" in text
    assert "not endorsed by NASA" in text
    assert not any(phrase in text.lower() for phrase in ENDORSEMENT_WORDING)


def test_credits_file_only_exists_when_nasa_images_were_used(tmp_path):
    path = nasa.write_credits_file(str(tmp_path), SOURCES)
    assert path == str(tmp_path / "credits.txt")
    assert (tmp_path / "credits.txt").read_text(encoding="utf-8") == nasa.credits_text(SOURCES)

    # 同一目录后来只用了 Pixabay：旧文件删除
    assert nasa.write_credits_file(str(tmp_path), [{"provider": "pixabay", "asset_id": "1"}]) == ""
    assert not (tmp_path / "credits.txt").exists()
    assert nasa.write_credits_file(str(tmp_path), []) == "" and not list(tmp_path.iterdir())


@pytest.mark.parametrize(("sources", "expected"), [(SOURCES, True), ([{"provider": "pexels"}], False)])
def test_task_writes_credits_from_the_material_manifest(tmp_path, sources, expected):
    (tmp_path / "script.json").write_text(json.dumps({"material_sources": sources}), encoding="utf-8")
    with patch.object(utils, "task_dir", return_value=str(tmp_path)):
        tm._write_material_credits("task")

    assert (tmp_path / "credits.txt").exists() is expected


# ------------------------------------------------------------------ 屏幕署名


@pytest.mark.parametrize(
    ("sources", "language", "expected"),
    [
        (SOURCES, "tr-TR", "Görüntüler: NASA, STScI ve diğerleri"),
        (SOURCES, "en-US", "Images: NASA, STScI and others"),
        (SOURCES[2:3], "en", "Images: NASA, JPL"),
        ([{"provider": "nasa", "credit": "NASA/Chris Williams"}], "tr", "Görüntüler: NASA"),
        # 署名里没写 NASA（"JPL"）也把 NASA 放在第一位
        ([{"provider": "nasa", "credit": "JPL"}], "tr", "Görüntüler: NASA, JPL"),
        ([{"provider": "pixabay"}], "tr", ""),
    ],
)
def test_overlay_credit_text_is_short(sources, language, expected):
    assert nasa.overlay_credit_text(sources, language) == expected


def _font():
    return str(Path(utils.font_dir()) / "STHeitiMedium.ttc")


@pytest.mark.parametrize(
    ("position", "custom", "at_top"),
    [("bottom", 70, True), ("center", 70, True), ("top", 70, False), ("two_thirds", 70, False),
     ("custom", 20, False), ("custom", 80, True)],
)
def test_credit_overlay_sits_opposite_the_subtitles_for_the_last_two_seconds(position, custom, at_top):
    params = VideoParams(video_subject="t", subtitle_position=position, custom_position=custom)

    clip = video.credit_overlay_clip("Görüntüler: NASA, ESA ve diğerleri", _font(), 1080, 1920, 34.85, params)

    assert clip.start == pytest.approx(34.85 - 2) and clip.duration == pytest.approx(2)
    _, y = clip.pos(0)
    assert (y < 1920 / 2) is at_top
    assert clip.size[0] <= 1080 * 0.9 and 30 <= clip.size[1] <= 80
    assert clip.mask is not None  # 半透明底，只盖住文字附近


def test_credit_overlay_shrinks_long_text_and_handles_short_videos():
    params = VideoParams(video_subject="t")
    long_text = "Görüntüler: " + ", ".join(["NASA", "ESA", "JPL", "STScI", "CXC", "AURA"] * 4)

    clip = video.credit_overlay_clip(long_text, _font(), 1080, 1920, 1.5, params)

    assert clip.size[0] <= 1080 * 0.9 or clip.size[1] < 40
    assert clip.start == 0 and clip.duration == pytest.approx(1.5)
    assert video.credit_overlay_clip("", _font(), 1080, 1920, 10, params) is None


@pytest.mark.parametrize(
    ("source", "overlay", "expected"),
    [("nasa", True, "Görüntüler: NASA, STScI ve diğerleri"), ("nasa", False, None), ("pixabay", True, None)],
)
def test_final_video_gets_the_credit_line_only_when_enabled(source, overlay, expected):
    params = VideoParams(video_subject="t", video_source=source, video_language="tr-TR", nasa_credit_overlay=overlay)
    with (
        patch.object(tm, "_load_material_sources", return_value=SOURCES),
        patch.object(tm.video, "combine_videos"),
        patch.object(tm.video, "generate_video", return_value=True) as generate_video,
        patch.object(tm.sm, "state", MemoryState()),
    ):
        tm.generate_final_videos(task_id="credit", params=params, downloaded_videos=["a.mp4"],
                                 audio_file="audio.mp3", subtitle_path="", audio_duration=5)

    assert generate_video.call_args.kwargs.get("credit_text") == expected


@pytest.mark.parametrize(("flags", "expected"), [([], False), (["--nasa-credit-overlay"], True)])
def test_cli_flag_sets_the_credit_overlay(flags, expected):
    with patch.dict(config.ui, {}, clear=True):
        params = cli.build_video_params(cli.parse_args(["--video-subject", "t", "--video-source", "nasa", *flags]))

    assert params.nasa_credit_overlay is expected


def test_example_config_documents_the_credit_overlay():
    app = tomllib.loads((ROOT_DIR / "config.example.toml").read_text(encoding="utf-8"))["app"]

    assert app["nasa_credit_overlay"] is False


@pytest.mark.parametrize(("language", "expected"), [
    ("tr-TR", "Görüntüler: NASA ve ortakları (images.nasa.gov)"),
    ("en-US", "Images: NASA and partners (images.nasa.gov)"),
])
def test_social_attribution_line(language, expected):
    assert nasa.social_attribution(SOURCES, language) == expected
    assert nasa.social_attribution([{"provider": "pexels"}], language) == ""


# ------------------------------------------------------------------ 文案署名规则

RULE = llm.SOURCE_ATTRIBUTION_RULES["nasa"][0]


def _rules_part(**kwargs):
    return llm.build_script_prompt("Kara delikler", **kwargs).split("# Initialization:")[0]


def test_nasa_rule_is_numbered_after_the_constraints():
    prompt = _rules_part(video_source="nasa")

    assert f"9. {RULE}" in prompt
    assert '"according to NASA"' in RULE and '"NASA says"' in RULE


def test_nasa_rule_follows_the_accuracy_rules():
    prompt = _rules_part(video_source="nasa", script_accuracy_rules=True)

    assert f"11. {llm.SCRIPT_ACCURACY_RULES[2]}" in prompt
    assert prompt.rstrip().endswith(f"12. {RULE}")


@pytest.mark.parametrize("source", ["pixabay", "pexels", "", None])
def test_other_sources_leave_the_prompt_unchanged(source):
    for accuracy in (False, True):
        assert llm.build_script_prompt("X", script_accuracy_rules=accuracy, video_source=source) == \
            llm.build_script_prompt("X", script_accuracy_rules=accuracy)


def test_custom_system_prompt_gets_an_attribution_section_once():
    custom = "You write short science scripts.\n1. Be brief."

    prompt = _rules_part(custom_system_prompt=custom, video_source="nasa", script_accuracy_rules=True)

    assert prompt.index("## Scientific Accuracy:") < prompt.index("## Attribution:")
    assert f"## Attribution:\n1. {RULE}" in prompt
    # 自定义 prompt 已经包含这条规则时不重复追加
    already = f"{custom}\n- {RULE}"
    assert _rules_part(custom_system_prompt=already, video_source="nasa").strip() == already


@pytest.mark.parametrize(("source", "forwarded"), [("nasa", True), ("pixabay", False)])
def test_task_and_api_forward_the_source_only_for_nasa(source, forwarded):
    params = VideoParams(video_subject="Kara delikler", video_source=source)
    with patch.object(tm.llm, "generate_script", return_value="Metin.") as generate:
        tm.generate_script("task", params)
    assert ("video_source" in generate.call_args.kwargs) is forwarded

    body = VideoScriptRequest(video_subject="Kara delikler", video_source=source)
    with patch.object(llm_controller.llm, "generate_script", return_value="Metin.") as generate:
        llm_controller.generate_video_script(None, body)
    assert ("video_source" in generate.call_args.kwargs) is forwarded


# ------------------------------------------------------------------ 第三方署名


@pytest.mark.parametrize(
    ("credit", "allowed"),
    [
        ("SpaceX", False),
        ("Lockheed Martin", False),
        ("Boeing/John Grant", False),
        # ESA 官网一般图片：商用需要书面授权
        ("European Space Agency (ESA) - D. Ducros", False),
        ("ESA", False),
        ("ESA/Hubble", True),
        ("ESA/Webb", True),
        ("NASA, ESA, CSA, STScI", True),
        ("JPL", True),
        ("X-ray: NASA/CXC/SAO; Optical: NASA/STScI", True),
        ("NASA_Jim Grossmann", True),
        ("NASA's Goddard Space Flight Center/Scott Wiessinger", True),
        # 与 NASA 合署的 ESA 署名保留
        ("ESA/Herschel/PACS/MESS Key Programme Supernova Remnant Team; NASA, ESA and Allison Loll", True),
        ("NASA", True),
        # NASA 中心名称算作 NASA（NASA 元数据里漏写的 "ASA"）
        ("ASA/GSFC/Solar Dynamics Observatory", True),
        ("SDSS", False),
        ("ESA and the Planck Collaboration", False),
        ("ESA/XMM-Newton/Younes et al. 2016", False),
    ],
)
def test_free_use_credit(credit, allowed):
    assert nasa.free_use_credit(credit) is allowed


@pytest.mark.parametrize(
    ("data", "expected", "allowed"),
    [
        # 摄影师字段写的是宇航员或 NASA 摄影师的名字
        ({"photographer": "Alex Gerst"}, "NASA/Alex Gerst", True),
        ({"photographer": "EMMETT GIVEN"}, "NASA/EMMETT GIVEN", True),
        ({"photographer": "NASA/Kim Shiflett"}, "NASA/Kim Shiflett", True),
        # 写的是机构名
        ({"photographer": "SpaceX"}, "SpaceX", False),
        ({"secondary_creator": "SDSS"}, "SDSS", False),
    ],
)
def test_photographer_names_are_nasa_photos_but_companies_are_not(data, expected, allowed):
    credit = nasa.extract_credit(data)

    assert credit == expected and nasa.free_use_credit(credit) is allowed


@pytest.mark.parametrize(
    ("description", "expected"),
    [
        ("Credit: ESA/Hubble & NASA #nasagoddard #space #science #Hubble #star", "ESA/Hubble & NASA"),
        ("Credit: NASA/JPL-Caltech @NASAJPL Follow us on Twitter", "NASA/JPL-Caltech"),
        ("Photo credit: NASA/Bill Ingalls https://www.flickr.com/photos/nasahqphoto", "NASA/Bill Ingalls"),
        ("Credit: NASA/Goddard. Read more: the mission page", "NASA/Goddard"),
    ],
)
def test_credit_drops_hashtags_and_social_media_tails(description, expected):
    assert nasa.extract_credit({"description": description}) == expected


def _detail(description, metadata=None):
    def fake_get_json(url, params=None):
        if "/asset/" in url:
            return {"collection": {"items": [
                {"href": "https://images-assets.nasa.gov/image/x/x~orig.jpg"},
                {"href": "https://images-assets.nasa.gov/image/x/metadata.json"},
            ]}}
        return {"File:ImageWidth": 4000, "File:ImageHeight": 6000, **(metadata or {})}

    data = {"nasa_id": "x", "title": "Launch", "description": description}
    with patch.object(nasa, "_get_json", side_effect=fake_get_json):
        return nasa._detailed_candidate(data, "rocket", 5, PORTRAIT, True, nasa.zoom_margin(5))


@pytest.mark.parametrize(
    ("description", "metadata", "reason"),
    [
        ("A Falcon 9 lifts off. Photo credit: SpaceX", None, "third_party_credit"),
        ("An artist's impression.", {"XMP:Credit": "European Space Agency (ESA) - D. Ducros"}, "third_party_credit"),
        ("A Falcon 9 lifts off. Photo credit: NASA/Kim Shiflett", None, ""),
        ("Hubble view. Credit: ESA/Hubble & NASA #nasagoddard", None, ""),
    ],
)
def test_candidates_without_a_free_use_credit_are_dropped(description, metadata, reason):
    item, why = _detail(description, metadata)

    assert why == reason
    if not reason:
        assert "#" not in item.source_info["credit"]


def test_filter_version_was_raised_for_the_credit_rules():
    assert nasa.FILTER_VERSION >= 3
