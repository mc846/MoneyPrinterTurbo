"""缓存清理也按同一条按修改时间的规则处理图片片段和无元数据副本；新的临时文件和未知文件不动。"""

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from app.services import cache_manager

NOW = 2_000_000_000.0
DAY = 86400
CLIP = f"{'a' * 64}.zoom-{'b' * 16}.mp4"
STILL = f"{'c' * 64}.static-{'d' * 16}.mp4"
SANITIZED = f"{'e' * 64}.sanitized.png"


@pytest.fixture
def cache_dir(tmp_path):
    with (
        patch.object(cache_manager.utils, "storage_dir", return_value=str(tmp_path)),
        patch.object(cache_manager.time, "time", return_value=NOW),
    ):
        yield tmp_path


def _file(directory: Path, name: str, age_days: float, size: int = 10) -> Path:
    path = directory / name
    path.write_bytes(b"x" * size)
    os.utime(path, (NOW - age_days * DAY, NOW - age_days * DAY))
    return path


def test_image_clips_and_sanitized_copies_follow_the_age_rule(cache_dir):
    old = [_file(cache_dir, name, 40) for name in (CLIP, STILL, SANITIZED, f"vid-{'f' * 32}.mp4")]
    recent = _file(cache_dir, f"{'1' * 64}.zoom-{'2' * 16}.mp4", 2)
    stats = cache_manager.get_video_cache_stats(max_age_days=30)
    assert stats.file_count == 4 and stats.total_size == 40
    assert cache_manager.get_video_cache_stats().file_count == 5
    result = cache_manager.clean_video_cache(max_age_days=30)
    assert result.deleted_count == 4 and result.failed_count == 0
    assert not any(path.exists() for path in old)
    assert recent.exists()


def test_unpublished_temporary_files_are_reclaimed_only_when_stale(cache_dir):
    stale = [_file(cache_dir, ".image-zoom-abc_12x9.mp4", 3), _file(cache_dir, ".image-sanitize-zz9_ab12.png", 3)]
    active = [_file(cache_dir, ".image-zoom-k2l3m4n5.mp4", 0.1), _file(cache_dir, ".image-sanitize-q1w2e3r4.png", 0.1)]
    result = cache_manager.clean_video_cache()
    assert result.deleted_count == 2
    assert not any(path.exists() for path in stale)
    assert all(path.exists() for path in active)


@pytest.mark.parametrize(
    "name",
    [
        "holiday.mp4",
        f"{'a' * 63}.zoom-{'b' * 16}.mp4",
        f"{'a' * 64}.zoom-{'b' * 15}.mp4",
        f"{'a' * 64}.pan-{'b' * 16}.mp4",
        f"{'A' * 64}.zoom-{'b' * 16}.mp4",
        f"{'e' * 64}.sanitized.jpg",
        "image-zoom-abcdefgh.mp4",
    ],
)
def test_user_and_unknown_files_are_never_touched(cache_dir, name):
    path = _file(cache_dir, name, 400)
    assert cache_manager.get_video_cache_stats().file_count == 0
    assert cache_manager.clean_video_cache().deleted_count == 0
    assert path.exists()
