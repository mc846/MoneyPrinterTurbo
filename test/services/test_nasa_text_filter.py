"""NASA 图片的文字检查（nasa_text_filter）：裁剪区域内外的文字、天文纹理、按排序检查前几个候选、
opencv 缺失、设置开关、缓存键和各层传递。不访问网络。"""

import random
import sys
import tomllib
from pathlib import Path
from unittest.mock import patch

import pytest
from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT_DIR = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT_DIR))

import cli  # noqa: E402
from app.config import config  # noqa: E402
from app.models.schema import MaterialInfo, MaterialTagRelevance, VideoAspect  # noqa: E402
from app.services import material, material_cache, nasa  # noqa: E402

PORTRAIT = VideoAspect.portrait
FONT = str(ROOT_DIR / "resource" / "fonts" / "STHeitiMedium.ttc")


def _preview(path, text_at=None, size=(640, 360)):
    """640×360 的预览图；9:16 裁剪区域是 x 219..421。text_at 是文字行左上角。"""
    image = Image.new("RGB", size, (12, 14, 30))
    if text_at:
        draw = ImageDraw.Draw(image)
        draw.text(text_at, "HUBBLE TELESCOPE", font=ImageFont.truetype(FONT, 22), fill=(240, 240, 240))
    image.save(path, "JPEG", quality=92)
    return str(path)


def _nebula(path, size=(640, 360)):
    """随机星点和模糊的星云团，代表天文图片的纹理。"""
    rng = random.Random(11)
    image = Image.new("L", size, 6)
    draw = ImageDraw.Draw(image)
    for _ in range(8):
        x, y, r = rng.randrange(size[0]), rng.randrange(size[1]), rng.randrange(30, 120)
        draw.ellipse([x - r, y - r, x + r, y + r], fill=rng.randrange(40, 140))
    image = image.filter(ImageFilter.GaussianBlur(25))
    draw = ImageDraw.Draw(image)
    for _ in range(1500):
        x, y, r = rng.randrange(size[0]), rng.randrange(size[1]), rng.choice((0, 0, 1, 2))
        draw.ellipse([x - r, y - r, x + r, y + r], fill=rng.randrange(120, 256))
    image.convert("RGB").save(path, "JPEG", quality=92)
    return str(path)


def _item(name, tags=("black hole",), preview=True):
    return MaterialInfo(
        provider="nasa", url=f"https://images-assets.nasa.gov/image/{name}/{name}~orig.jpg", duration=5,
        source_info={"provider": "nasa", "search_term": "black hole", "asset_id": name,
                     "preview": f"https://images-assets.nasa.gov/image/{name}/{name}~small.jpg" if preview else "",
                     "rendition": {"id": "orig", "width": 2160, "height": 3840},
                     "tags": list(tags), "ai_generated": False},
    )


# ------------------------------------------------------------------ 检测


def test_text_inside_the_crop_is_found_and_text_outside_is_not(tmp_path):
    pytest.importorskip("cv2")
    inside = nasa.crop_text_lines(_preview(tmp_path / "inside.jpg", text_at=(230, 300)), PORTRAIT)
    outside = nasa.crop_text_lines(_preview(tmp_path / "outside.jpg", text_at=(10, 300)), PORTRAIT)
    clean = nasa.crop_text_lines(_preview(tmp_path / "clean.jpg"), PORTRAIT)

    assert len(inside) >= 1 and all(line["score"] >= nasa.TEXT_SCORE_THRESHOLD for line in inside)
    assert outside == [] and clean == []
    # 同一张横图做横屏视频时裁剪区域就是整张图，左边的文字也在画面里
    assert nasa.crop_text_lines(str(tmp_path / "outside.jpg"), VideoAspect.landscape)


def test_star_field_and_nebula_texture_are_not_text(tmp_path):
    pytest.importorskip("cv2")

    assert nasa.crop_text_lines(_nebula(tmp_path / "nebula.jpg"), PORTRAIT) == []


def test_text_lines_need_the_aspect_ratio(tmp_path, monkeypatch):
    pytest.importorskip("cv2")
    path = _preview(tmp_path / "inside.jpg", text_at=(230, 300))
    monkeypatch.setattr(nasa, "_text_lines", {})
    monkeypatch.setattr(nasa, "TEXT_MIN_ASPECT", 50.0)

    assert nasa.crop_text_lines(path, PORTRAIT) == []


# ------------------------------------------------------------------ 按排序检查前几个候选


def _screen(items, flagged, *, failed_downloads=()):
    checked = []

    def download(url, save_dir):
        name = url.rsplit("/", 1)[-1].split("~")[0]
        return "" if name in failed_downloads else f"/tmp/{name}.jpg"

    def lines(path, aspect):
        name = Path(path).stem
        checked.append(name)
        return [{"score": 0.9, "height": 0.05}] if name in flagged else []

    with (
        patch.object(nasa, "_download_image", side_effect=download),
        patch.object(nasa, "crop_text_lines", side_effect=lines),
        patch.object(nasa.utils, "storage_dir", return_value=str(Path.cwd())),
    ):
        kept = nasa.screen_text(items, PORTRAIT, "black hole")
    return [i.source_info["asset_id"] for i in kept], checked


def test_candidates_are_checked_in_order_until_three_pass():
    items = [_item(f"n{i}") for i in range(7)]

    kept, checked = _screen(items, flagged={"n0", "n2"})

    # n0 和 n2 被淘汰；n1、n3、n4 通过后停止，n5、n6 不检查、原样保留
    assert kept == ["n1", "n3", "n4", "n5", "n6"]
    assert checked == ["n0", "n1", "n2", "n3", "n4"]
    assert nasa.TEXT_CHECKED_CANDIDATES == 3


def test_every_candidate_with_text_is_dropped_when_few_pass():
    items = [_item(f"n{i}") for i in range(4)]

    kept, checked = _screen(items, flagged={"n0", "n1", "n2"})

    assert kept == ["n3"] and checked == ["n0", "n1", "n2", "n3"]


def test_unknown_candidates_are_kept():
    # 预览图下载失败或检测失败时保留候选
    items = [_item("a"), _item("b"), _item("c", preview=False)]
    kept, checked = _screen(items, flagged={"b"}, failed_downloads={"a"})

    assert kept == ["a", "c"]
    # 没有预览地址的候选用 asset_id 推出 ~small.jpg
    assert checked == ["b", "c"]
    assert nasa._preview_url(_item("c", preview=False)).endswith("/image/c/c~small.jpg")


def test_skip_reason_is_logged():
    with patch.object(nasa.logger, "info") as info:
        _screen([_item("PIA10207"), _item("PIA1")], flagged={"PIA10207"})

    message = info.call_args.args[0]
    assert "nasa text filter: skipped PIA10207 for 'black hole'" in message
    assert "text lines in the crop area=1" in message and "5.0% of the crop height" in message


# ------------------------------------------------------------------ 与素材搜索的集成


def _search(items, *, enabled, active=True, relevance=MaterialTagRelevance.rank):
    with (
        patch.dict(config.app, {"nasa_text_filter": enabled}),
        patch.object(nasa, "_text_detector", return_value=object() if active else None),
        patch.object(material, "_search_videos_with_cache", return_value=items),
        patch.object(nasa, "screen_text", side_effect=lambda found, aspect, term: found[1:]) as screen,
    ):
        search, _ = material._stock_source_search(
            "nasa", crop_to_aspect=True, tag_relevance=relevance, exclude_ai_generated=False, anchors=[])
        found = search(search_term="black hole", minimum_duration=5, video_aspect=PORTRAIT)
    return [i.source_info["asset_id"] for i in found], screen


def test_search_checks_text_after_ranking_when_enabled():
    items = [_item("off", tags=["galaxy"]), _item("top"), _item("second")]

    found, screen = _search(items, enabled=True)

    # 相关度排序后 "top" 在最前；screen_text 拿到的是排序后的列表
    assert [i.source_info["asset_id"] for i in screen.call_args.args[0]] == ["top", "second", "off"]
    assert found == ["second", "off"]


@pytest.mark.parametrize(("enabled", "active"), [(False, True), (True, False)])
def test_search_is_unchanged_when_disabled_or_without_opencv(enabled, active):
    items = [_item("a"), _item("b")]

    found, screen = _search(items, enabled=enabled, active=active)

    assert found == ["a", "b"]
    screen.assert_not_called()


def test_text_filter_without_opencv_warns_once(monkeypatch):
    monkeypatch.setattr(nasa, "_text_detector_state", {})
    monkeypatch.setitem(sys.modules, "cv2", None)

    with patch.dict(config.app, {"nasa_text_filter": True}), patch.object(nasa.logger, "warning") as warning:
        assert nasa.text_filter_active() is False
        assert nasa.crop_text_lines("missing.jpg", PORTRAIT) is None
        assert nasa.text_filter_active() is False
    assert warning.call_count == 1 and "uv sync --extra face" in warning.call_args.args[0]


def test_pixabay_candidates_are_never_text_checked():
    with (
        patch.dict(config.app, {"nasa_text_filter": True}),
        patch.object(nasa, "_text_detector", return_value=object()),
        patch.object(material, "_search_videos_with_cache", return_value=[_item("p")]),
        patch.object(nasa, "screen_text") as screen,
    ):
        search, _ = material._stock_source_search(
            "pixabay", crop_to_aspect=True, tag_relevance=MaterialTagRelevance.rank,
            exclude_ai_generated=False, anchors=[])
        search(search_term="black hole", minimum_duration=5, video_aspect=PORTRAIT)
    screen.assert_not_called()


# ------------------------------------------------------------------ 缓存键和设置


def test_cache_key_carries_the_filter_version_and_the_setting():
    with patch.dict(config.app, {"nasa_text_filter": False}):
        off = nasa.search_cache_variant()
    with patch.dict(config.app, {"nasa_text_filter": True}):
        on = nasa.search_cache_variant()

    assert nasa.FILTER_VERSION >= 4
    assert off["text_filter"] is False and on["text_filter"] is True
    assert material_cache._cache_key("nasa", "black hole", 5, PORTRAIT, variant=off) != material_cache._cache_key(
        "nasa", "black hole", 5, PORTRAIT, variant=on)


def test_search_cache_keeps_the_preview_url():
    item = _item("PIA1")
    cached = material_cache._cached_source_info(item)

    assert cached["preview"] == "https://images-assets.nasa.gov/image/PIA1/PIA1~small.jpg"


def test_detailed_candidate_records_the_small_preview():
    files = [f"http://images-assets.nasa.gov/image/A/A~{v}.jpg" for v in ("orig", "large", "medium", "small")]
    files.append("http://images-assets.nasa.gov/image/A/metadata.json")

    def fake_json(url, params=None):
        if url.endswith("/asset/A"):
            return {"collection": {"items": [{"href": f} for f in files]}}
        return {"File:ImageWidth": 6000, "File:ImageHeight": 4000}

    with patch.object(nasa, "_get_json", side_effect=fake_json):
        item, reason = nasa._detailed_candidate(
            {"nasa_id": "A", "title": "Nebula", "keywords": ["nebula"]}, "nebula", 5, PORTRAIT, True, 1.15)

    assert reason == "" and item.source_info["preview"] == "https://images-assets.nasa.gov/image/A/A~small.jpg"


def test_example_config_documents_the_setting():
    app = tomllib.loads((ROOT_DIR / "config.example.toml").read_text(encoding="utf-8"))["app"]

    assert app["nasa_text_filter"] is nasa.TEXT_FILTER_DEFAULT


def test_filter_is_on_by_default():
    # conftest 在测试里关闭它；这里检查没有配置项时的默认值
    with patch.object(config, "app", {}):
        assert nasa.text_filter_enabled() is True is nasa.TEXT_FILTER_DEFAULT


@pytest.mark.parametrize("value", ["0", "false", "off", "", False, 0])
def test_false_like_values_turn_the_filter_off(value):
    with patch.dict(config.app, {"nasa_text_filter": value}):
        assert nasa.text_filter_enabled() is False


@pytest.mark.parametrize(("flags", "expected"), [(["--nasa-text-filter"], True), (["--no-nasa-text-filter"], False)])
def test_cli_flag_overrides_the_setting_for_this_process_only(flags, expected):
    before = (ROOT_DIR / "config.example.toml").read_bytes()
    with patch.dict(config.app, {"nasa_text_filter": not expected}):
        cli.apply_nasa_overrides(cli.parse_args(["--video-subject", "t", "--video-source", "nasa", *flags]))
        assert config.app["nasa_text_filter"] is expected
    with patch.dict(config.app, {"nasa_text_filter": "kept"}):
        cli.apply_nasa_overrides(cli.parse_args(["--video-subject", "t"]))
        assert config.app["nasa_text_filter"] == "kept"
    assert (ROOT_DIR / "config.example.toml").read_bytes() == before


def test_model_file_and_license_ship_with_the_package():
    model = Path(nasa.TEXT_MODEL_FILE)
    license_text = (model.parent / "LICENSE").read_text(encoding="utf-8")

    assert model.is_file() and model.stat().st_size > 1_000_000
    assert "Apache License" in license_text and "03f550c6b406fda8" in license_text
