"""NASA 文字检查结果缓存：命中时不下载预览图、不检测；预览地址、FILTER_VERSION、比例或检测参数
改变时重新检测；检测失败不缓存，损坏的缓存重新检测。不访问网络。"""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from app.models.schema import MaterialInfo, VideoAspect
from app.services import nasa

PORTRAIT = VideoAspect.portrait


def _item(name):
    return MaterialInfo(
        provider="nasa", url=f"https://images-assets.nasa.gov/image/{name}/{name}~orig.jpg", duration=5,
        source_info={"provider": "nasa", "asset_id": name,
                     "preview": f"https://images-assets.nasa.gov/image/{name}/{name}~small.jpg"},
    )


class _Detector:
    """记录下载和检测调用；flagged 里的候选在裁剪区域里有文字。"""

    def __init__(self, flagged=(), failing=()):
        self.flagged, self.failing = set(flagged), set(failing)
        self.downloads, self.checks = [], []

    def download(self, url, save_dir):
        self.downloads.append(url)
        return str(Path(save_dir) / (url.rsplit("/", 1)[-1].split("~")[0] + ".jpg"))

    def lines(self, path, aspect):
        name = Path(path).stem
        self.checks.append(name)
        if name in self.failing:
            return None
        return [{"score": 0.9, "height": 0.05}] if name in self.flagged else []


def _screen(cache_dir, items, detector, aspect=PORTRAIT):
    with (
        patch.object(nasa, "_download_image", side_effect=detector.download),
        patch.object(nasa, "crop_text_lines", side_effect=detector.lines),
        patch.object(nasa.utils, "storage_dir", return_value=str(cache_dir)),
    ):
        kept = nasa.screen_text(items, aspect, "zombie star")
    return [item.source_info["asset_id"] for item in kept]


def test_cached_results_skip_the_preview_download_and_the_detector(tmp_path):
    items = [_item("text"), _item("clean")]
    first = _Detector(flagged={"text"})
    assert _screen(tmp_path, items, first) == ["clean"]
    assert first.checks == ["text", "clean"] and len(first.downloads) == 2
    assert len(list(tmp_path.glob("nasa-text-*.json"))) == 2
    second = _Detector()  # 检测器换成“没有文字”，命中缓存时不会被调用
    assert _screen(tmp_path, items, second) == ["clean"]
    assert second.downloads == [] and second.checks == []
    assert not list(tmp_path.glob(".nasa-text-*"))


def test_cache_file_holds_only_the_text_lines(tmp_path):
    _screen(tmp_path, [_item("text")], _Detector(flagged={"text"}))
    (cached,) = tmp_path.glob("nasa-text-*.json")
    assert json.loads(cached.read_text(encoding="utf-8")) == {"lines": [{"score": 0.9, "height": 0.05}]}


@pytest.mark.parametrize(
    "name, value",
    [
        ("FILTER_VERSION", nasa.FILTER_VERSION + 1),
        ("TEXT_SCORE_THRESHOLD", 0.8),
        ("TEXT_MIN_ASPECT", 2.0),
        ("TEXT_INPUT_LONG_EDGE", 1024),
        ("TEXT_DB_BINARY_THRESHOLD", 0.4),
        ("TEXT_DB_POLYGON_THRESHOLD", 0.6),
        ("TEXT_DB_UNCLIP_RATIO", 1.5),
        ("TEXT_DB_MAX_CANDIDATES", 100),
        ("TEXT_MODEL_FILE", "text_detection_other.onnx"),
    ],
)
def test_filter_version_and_every_rule_parameter_change_the_cache_key(tmp_path, name, value):
    preview = nasa._preview_url(_item("a"))
    reference = nasa._text_result_path(preview, PORTRAIT, str(tmp_path))
    with patch.object(nasa, name, value):
        assert nasa._text_result_path(preview, PORTRAIT, str(tmp_path)) != reference


def test_preview_url_and_aspect_change_the_cache_key(tmp_path):
    first, second = nasa._preview_url(_item("a")), nasa._preview_url(_item("b"))
    paths = {
        nasa._text_result_path(first, PORTRAIT, str(tmp_path)),
        nasa._text_result_path(second, PORTRAIT, str(tmp_path)),
        nasa._text_result_path(first, VideoAspect.landscape, str(tmp_path)),
        nasa._text_result_path(first, VideoAspect.square, str(tmp_path)),
    }
    assert len(paths) == 4
    assert all(Path(path).parent == tmp_path for path in paths)


def test_changed_rule_parameter_runs_the_detector_again(tmp_path):
    items = [_item("clean")]
    _screen(tmp_path, items, _Detector())
    with patch.object(nasa, "TEXT_SCORE_THRESHOLD", 0.5):
        detector = _Detector(flagged={"clean"})
        assert _screen(tmp_path, items, detector) == []
    assert detector.checks == ["clean"]


def test_other_aspect_is_checked_separately(tmp_path):
    items = [_item("clean")]
    _screen(tmp_path, items, _Detector())
    detector = _Detector()
    _screen(tmp_path, items, detector, aspect=VideoAspect.landscape)
    assert detector.checks == ["clean"]


def test_failed_checks_are_not_cached(tmp_path):
    items = [_item("broken")]
    assert _screen(tmp_path, items, _Detector(failing={"broken"})) == ["broken"]
    assert not list(tmp_path.glob("nasa-text-*.json"))
    detector = _Detector(flagged={"broken"})
    assert _screen(tmp_path, items, detector) == []
    assert detector.checks == ["broken"]


def test_failed_preview_download_is_not_cached(tmp_path):
    items = [_item("offline")]
    detector = _Detector()
    with (
        patch.object(nasa, "_download_image", return_value=""),
        patch.object(nasa, "crop_text_lines", side_effect=detector.lines),
        patch.object(nasa.utils, "storage_dir", return_value=str(tmp_path)),
    ):
        assert [i.source_info["asset_id"] for i in nasa.screen_text(items, PORTRAIT)] == ["offline"]
    assert detector.checks == [] and not list(tmp_path.glob("nasa-text-*.json"))


@pytest.mark.parametrize("content", ["{not json", '{"lines": "none"}', '{"lines": [{"score": "high"}]}', "{}"])
def test_invalid_cache_files_are_checked_again_and_replaced(tmp_path, content):
    items = [_item("text")]
    path = Path(nasa._text_result_path(nasa._preview_url(items[0]), PORTRAIT, str(tmp_path)))
    path.write_text(content, encoding="utf-8")
    detector = _Detector(flagged={"text"})
    assert _screen(tmp_path, items, detector) == []
    assert detector.checks == ["text"]
    assert json.loads(path.read_text(encoding="utf-8"))["lines"] == [{"score": 0.9, "height": 0.05}]


def test_cache_write_failure_does_not_change_the_result(tmp_path):
    items = [_item("text"), _item("clean")]
    with patch.object(nasa.os, "replace", side_effect=PermissionError("locked")):
        assert _screen(tmp_path, items, _Detector(flagged={"text"})) == ["clean"]
    assert not list(tmp_path.glob("nasa-text-*")) and not list(tmp_path.glob(".nasa-text-*"))
