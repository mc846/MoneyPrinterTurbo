"""缓存清理按同一条按修改时间的规则处理 NASA 原图、预览图、处理后的图片和文字检查结果；
文件名与 nasa 模块实际写出的名字一致。"""

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from app.models.schema import VideoAspect
from app.services import cache_manager, nasa
from app.utils import utils

NOW = 2_000_000_000.0
DAY = 86400
URL = "https://images-assets.nasa.gov/image/PIA1/PIA1~orig.jpg"
PREVIEW = "https://images-assets.nasa.gov/image/PIA1/PIA1~small.jpg"


@pytest.fixture
def cache_dir(tmp_path):
    with (
        patch.object(cache_manager.utils, "storage_dir", return_value=str(tmp_path)),
        patch.object(cache_manager.time, "time", return_value=NOW),
    ):
        yield tmp_path


def _file(path: Path, age_days: float, size: int = 10) -> Path:
    path.write_bytes(b"x" * size)
    os.utime(path, (NOW - age_days * DAY, NOW - age_days * DAY))
    return path


def _nasa_names(directory: Path):
    """nasa 模块为同一张图片写出的文件名。"""
    original = f"nasa-{utils.md5(URL)}.jpg"
    preview = f"nasa-{utils.md5(PREVIEW)}.jpg"
    prepared = [f"nasa-{utils.md5(URL)}-{aspect.name}-1242x2208.jpg" for aspect in VideoAspect]
    text = [Path(nasa._text_result_path(PREVIEW, aspect, str(directory))).name for aspect in VideoAspect]
    return [original, preview, f"nasa-{utils.md5(URL)}.png", *prepared, *text]


def test_nasa_files_follow_the_age_rule(cache_dir):
    old = [_file(cache_dir / name, 40) for name in _nasa_names(cache_dir)]
    recent = _file(cache_dir / f"nasa-{'f' * 32}.jpg", 2)
    assert cache_manager.get_video_cache_stats(max_age_days=30).file_count == len(old)
    result = cache_manager.clean_video_cache(max_age_days=30)
    assert result.deleted_count == len(old) and result.failed_count == 0
    assert not any(path.exists() for path in old) and recent.exists()


def test_nasa_temporary_files_are_reclaimed_only_when_stale(cache_dir):
    prepared_part = f"nasa-{utils.md5(URL)}-portrait-1242x2208.jpg.part"
    stale = [_file(cache_dir / name, 3) for name in (".nasa-k3j_x9a2.jpg", ".nasa-text-ab12cd34.json", prepared_part)]
    active = [_file(cache_dir / name, 0.1) for name in (".nasa-q1w2e3r4.png", ".nasa-text-zz99yy88.json")]
    assert cache_manager.clean_video_cache().deleted_count == len(stale)
    assert not any(path.exists() for path in stale) and all(path.exists() for path in active)


@pytest.mark.parametrize(
    "name",
    ["nasa-logo.png", f"nasa-{'a' * 31}.jpg", f"nasa-{'a' * 32}-portrait.jpg", "nasa-text-notes.json",
     f"nasa-{'a' * 32}-diagonal-10x10.jpg", "credits.txt.part"],
)
def test_other_nasa_like_names_are_never_touched(cache_dir, name):
    path = _file(cache_dir / name, 400)
    assert cache_manager.clean_video_cache().deleted_count == 0 and path.exists()
