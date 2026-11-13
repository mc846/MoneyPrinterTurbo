"""Subtitle publication retries a Windows "Access is denied" rename (a scanner holding the fresh file): the
edge subtitle writer and write_subtitle_file publish after a short retry; a lasting error still fails."""

import os

import pytest

from app.services import voice
from app.utils import subtitle_writer

SRT = ["1", "00:00:00,000 --> 00:00:01,500", "Merhaba.", ""]


@pytest.fixture
def flaky_replace(monkeypatch):
    """os.replace fails with PermissionError ``failures[0]`` times for .srt files, then works."""
    real = os.replace
    failures = [2]

    def replace(source, target):
        if str(target).endswith(".srt") and failures[0] > 0:
            failures[0] -= 1
            raise PermissionError(5, "Erişim engellendi")
        return real(source, target)

    monkeypatch.setattr(os, "replace", replace)
    monkeypatch.setattr(subtitle_writer, "REPLACE_DELAY_SECONDS", 0)
    return failures


def test_the_edge_subtitle_survives_a_short_lock(tmp_path, flaky_replace):
    target = tmp_path / "subtitle.srt"
    assert voice._write_subtitle_items(SRT, str(target))
    assert target.read_text(encoding="utf-8").startswith("1\n00:00:00,000") and flaky_replace[0] == 0
    assert not list(tmp_path.glob(".subtitle-*"))  # the staged file is gone


def test_write_subtitle_file_retries_and_a_lasting_lock_still_fails(tmp_path, flaky_replace):
    target = tmp_path / "altyazi.srt"
    assert subtitle_writer.write_subtitle_file(target, "\n".join(SRT))
    assert target.is_file()
    flaky_replace[0] = subtitle_writer.REPLACE_ATTEMPTS + 1
    with pytest.raises(PermissionError):
        subtitle_writer.write_subtitle_file(tmp_path / "ikinci.srt", "\n".join(SRT))
    assert not (tmp_path / "ikinci.srt").exists() and not list(tmp_path.glob(".subtitle-*"))
