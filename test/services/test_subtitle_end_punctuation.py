"""Videoya gömülü altyazıda cümle sonundaki "?" ve "!" (subtitle_keep_end_punctuation): metinden geri gelir, nokta ve
virgül gelmez, .srt dosyası değişmez; iki son render motorunda çizilen metin; CLI, reçeteler ve revizyon bayrağı."""

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from app.config import config
from app.models.schema import VideoAspect, VideoParams
from app.services import batch_production as bp
from app.services import task_revisions, video
from app.utils.subtitle_reader import moviepy_subtitles, restore_end_punctuation

ROOT = Path(__file__).resolve().parents[2]
FF = video.utils.get_ffmpeg_binary()
SCRIPT = ("Ya Ay'ın tozu, Dünya'nın geleceğini aydınlatacak bir yakıt deposu olsaydı? Helyum-3 bırakıyor; "
          "bunu Apollo doğruladı. Ne harika! \"Kim bilir?\" diye sordu. Peki, değer miydi?!")
CUES = [((0, 1), "Ya Ay'ın tozu"), ((1, 2), "Dünya'nın geleceğini aydınlatacak bir yakıt deposu olsaydı"),
        ((2, 3), "Helyum-3 bırakıyor"), ((3, 4), "bunu Apollo doğruladı"), ((4, 5), "Ne harika"),
        ((5, 6), "Kim bilir"), ((6, 7), "diye sordu"), ((7, 8), "Peki"), ((8, 9), "değer miydi")]


def test_closing_question_and_exclamation_marks_come_back():
    restored = [text for _timing, text in restore_end_punctuation(CUES, SCRIPT)]
    assert restored == ["Ya Ay'ın tozu", "Dünya'nın geleceğini aydınlatacak bir yakıt deposu olsaydı?",
                        "Helyum-3 bırakıyor", "bunu Apollo doğruladı", "Ne harika!", "Kim bilir?", "diye sordu",
                        "Peki", "değer miydi?!"]  # no comma, semicolon or full stop; ? after a closing quote
    assert CUES[1][1].endswith("olsaydı")  # the input is not changed
    assert restore_end_punctuation(CUES, "") == CUES
    # A cue that is not in the script stays as it is, and the next ones are still found.
    other = [((0, 1), "başka bir şey"), ((1, 2), "Ne harika")]
    assert [text for _t, text in restore_end_punctuation(other, SCRIPT)] == ["başka bir şey", "Ne harika!"]
    # Already there: not doubled.
    assert restore_end_punctuation([((0, 1), "Ne harika!")], SCRIPT)[0][1] == "Ne harika!"


SIZE = (180, 320)


@pytest.fixture(scope="module")
def inputs(tmp_path_factory):
    root = tmp_path_factory.mktemp("punctuation")
    subprocess.run([FF, "-y", "-f", "lavfi", "-i", f"color=c=gray:s={SIZE[0]}x{SIZE[1]}:r=30", "-t", "3",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(root / "base.mp4")], check=True, capture_output=True)
    subprocess.run([FF, "-y", "-f", "lavfi", "-i", "sine=f=440:d=2.9", "-ar", "44100", str(root / "voice.wav")],
                   check=True, capture_output=True)
    (root / "subtitles.srt").write_text("1\n00:00:00,200 --> 00:00:01,400\nYa olsaydı\n\n"
                                        "2\n00:00:01,500 --> 00:00:02,800\nNe harika\n\n", encoding="utf-8")
    return root


@pytest.mark.parametrize("engine", ["ffmpeg", "moviepy"])
@pytest.mark.parametrize("keep", [True, False])
def test_the_burned_subtitles_keep_the_marks_and_the_srt_stays(inputs, monkeypatch, engine, keep):
    drawn = []
    original = video.TextClip

    class Recording(original):
        def __init__(self, *args, **kwargs):
            drawn.append(kwargs.get("text"))
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(video, "TextClip", Recording)
    srt = (inputs / "subtitles.srt").read_bytes()
    params = VideoParams(video_subject="x", video_aspect="9:16", font_size=18, bgm_type="",
                         video_script="Ya olsaydı? Ne harika! Son.", subtitle_keep_end_punctuation=keep)
    with (patch.dict(config.app, {"final_render_engine": engine, "scene_labels_enabled": False}),
          patch.object(VideoAspect, "to_resolution", lambda self: SIZE)):
        video.generate_video(str(inputs / "base.mp4"), str(inputs / "voice.wav"), str(inputs / "subtitles.srt"),
                             str(inputs / f"out-{engine}-{keep}.mp4"), params)
    texts = " | ".join(str(text) for text in drawn if text)
    if keep:
        assert "olsaydı?" in texts and "harika!" in texts
    else:
        assert "olsaydı?" not in texts and "harika!" not in texts and "olsaydı" in texts
    assert (inputs / "subtitles.srt").read_bytes() == srt  # the .srt file does not change
    assert moviepy_subtitles(str(inputs / "subtitles.srt"))[0][1] == "Ya olsaydı"


def test_the_script_json_next_to_the_subtitles_is_the_fallback(tmp_path):
    (tmp_path / "script.json").write_text('{"script": "Ya olsaydı?"}', encoding="utf-8")
    assert video._subtitle_script(VideoParams(video_subject="x"), str(tmp_path / "subtitle.srt")) == "Ya olsaydı?"
    assert video._subtitle_script(VideoParams(video_subject="x", video_script="A?"), str(tmp_path / "s.srt")) == "A?"


def test_the_setting_in_the_cli_the_presets_and_revisions():
    import cli

    base = ["--video-subject", "x", "--video-source", "pexels", "--stop-at", "script"]
    params = cli.build_video_params(cli.parse_args([*base, "--subtitle-keep-end-punctuation"]))
    assert params.subtitle_keep_end_punctuation is True
    default = cli.build_video_params(cli.parse_args(base))
    assert default.subtitle_keep_end_punctuation is False  # upstream behavior
    for name in ("kurgu", "uzay"):
        assert bp.load_preset(ROOT / "presets" / f"{name}.toml").video["subtitle_keep_end_punctuation"] is True
    assert "subtitle_keep_end_punctuation" not in bp.load_preset(ROOT / "presets" / "gercek.toml").video
    args = task_revisions.parser().parse_args(["görev", "--altyazi-noktalama", "acik"])
    settings = task_revisions.build_settings(args)
    assert settings == {"subtitle_keep_end_punctuation": True}
    assert task_revisions.settings_arguments(settings) == ["--altyazi-noktalama=acik"]
    assert "subtitle_keep_end_punctuation" in task_revisions.EDITABLE
