"""Toplu üretim (batch.py)：输入解析、预设校验、目录名、断点续跑与失败重试。任务函数用假实现替代，不生成真实视频。"""

import json
import signal
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from loguru import logger

from app import launcher
from app.config import config
from app.services import batch_production as bp

PRESET = Path(__file__).resolve().parents[2] / "presets" / "uzay.toml"


@pytest.fixture(autouse=True)
def isolated_batches(tmp_path, monkeypatch):
    """每个测试使用自己的 storage/batches，并在结束后恢复 config.app（[app] 段落会改写它）。"""
    monkeypatch.setattr(bp, "batches_dir", lambda: tmp_path / "batches")
    with patch.dict(config.app, {}):
        yield


def write(path: Path, text: str, encoding: str = "utf-8") -> Path:
    path.write_bytes(text.encode(encoding))
    return path


# ---------------------------------------------------------------- girdi

def test_csv_with_bom_semicolon_and_turkish_letters(tmp_path):
    source = write(tmp_path / "konular.csv",
                   "konu;talimat;ses\nNötron yıldızları;Pulsarlardan söz et.;\n"
                   "Satürn'ün halkaları neden kayboluyor?;;tr-TR-AhmetNeural-Male\n", "utf-8-sig")
    assert bp.read_inputs(source) == [
        bp.BatchInput("Nötron yıldızları", "Pulsarlardan söz et.", ""),
        bp.BatchInput("Satürn'ün halkaları neden kayboluyor?", "", "tr-TR-AhmetNeural-Male"),
    ]


def test_csv_comma_header_in_any_order_with_quotes(tmp_path):
    source = write(tmp_path / "k.csv", 'ses,konu\n,"Zombi yıldızlar, gerçekten var mı?"\n')
    assert bp.read_inputs(source) == [bp.BatchInput("Zombi yıldızlar, gerçekten var mı?")]


def test_csv_without_header_reads_columns_by_position(tmp_path):
    source = write(tmp_path / "k.csv", "Kara delikler;Kısa tut\nGalaksiler\n")
    assert bp.read_inputs(source) == [bp.BatchInput("Kara delikler", "Kısa tut"), bp.BatchInput("Galaksiler")]


def test_csv_skips_empty_comment_and_blank_subject_rows(tmp_path):
    source = write(tmp_path / "k.csv", "konu,talimat\n\n# taslak,yok\n , boş konu\nIşık hızı,\n,\n")
    assert bp.read_inputs(source) == [bp.BatchInput("Işık hızı")]


def test_csv_unknown_column_is_an_error(tmp_path):
    source = write(tmp_path / "k.csv", "konu,sure\nIşık,30\n")
    with pytest.raises(bp.BatchInputError, match="bilinmeyen CSV sütunu: sure"):
        bp.read_inputs(source)


def test_txt_skips_empty_and_comment_lines(tmp_path):
    source = write(tmp_path / "k.txt", "﻿# uzay listesi\nNötron yıldızları\n\n   \n  # sonra\n  Çift yıldızlar  \n")
    assert bp.read_inputs(source) == [bp.BatchInput("Nötron yıldızları"), bp.BatchInput("Çift yıldızlar")]


@pytest.mark.parametrize("name, data, message", [
    ("k.json", b"[]", ".csv ya da .txt"),
    ("k.txt", b"# yalniz yorum\n\n", "konu yok"),
    ("k.txt", "Işık".encode("cp1254"), "UTF-8 değil"),
])
def test_bad_inputs(tmp_path, name, data, message):
    (tmp_path / name).write_bytes(data)
    with pytest.raises(bp.BatchInputError, match=message):
        bp.read_inputs(tmp_path / name)


@pytest.mark.parametrize("text, slug", [
    ("Satürn'ün halkaları neden kayboluyor?", "saturn-un-halkalari-neden-kayboluyor"),
    ("İĞNE ŞİŞE Ölçü ÇAĞ", "igne-sise-olcu-cag"),
    ("Café — déjà vu", "cafe-deja-vu"),
    ("???", "video"),
    ("a" * 80, "a" * 60),
])
def test_slugify(text, slug):
    assert bp.slugify(text) == slug


# ---------------------------------------------------------------- reçete

def test_space_preset_loads():
    preset = bp.load_preset(PRESET)
    assert preset.video["video_source"] == "nasa" and preset.video["llm_provider"] == "claude_code"
    assert preset.target_words == (90, 100) and preset.target_duration == (35.0, 60.0) and preset.audit
    assert preset.app["material_concurrency"] == 3
    prompt = preset.script_prompt("Nötron yıldızları", "Pulsarlardan söz et.")
    assert prompt.startswith("En az 90, en çok 100 kelime yaz; 90 kelimenin altına inme.")
    assert prompt.endswith("\nPulsarlardan söz et.") and "{" not in prompt


def preset_file(tmp_path, text):
    return write(tmp_path / "r.toml", text)


@pytest.mark.parametrize("text, message", [
    ("[videos]\nvideo_source = 'nasa'\n", r"bilinmeyen bölüm: \[videos\]"),
    ("[video]\nvideo_sorce = 'nasa'\n", "bilinmeyen anahtar: video_sorce .*video_source"),
    ("[video]\nvideo_subject = 'x'\n", "video_subject reçetede kullanılamaz"),
    ("[video]\nvideo_source = 'nasaa'\n", "invalid choice: 'nasaa'"),
    ("[video]\nvideo_clip_duration = 'uzun'\n", "video-clip-duration"),
    ("[app]\nmaterial_concurency = 3\n", "bilinmeyen anahtar: material_concurency .*material_concurrency"),
    ("[app]\nmaterial_concurrency = '3'\n", "material_concurrency int olmalı"),
    ("[app]\nnasa_face_filter = 1\n", "nasa_face_filter bool olmalı"),
    ("[script]\nhedef = 1\n", r"\[script\] bilinmeyen anahtar: hedef"),
    ("[script]\nprompt = '{kelime} kelime'\n", r"bilinmeyen yer tutucu içeriyor: \{kelime\}"),
    ("[script]\nprompt = '{kelime_min} kelime'\n", "target_words yok"),
    ("[script]\ntarget_words = [100, 90]\n", "aralık olmalı"),
    ("[quality]\ntarget_duration = 45\n", "aralık olmalı"),
    ("[quality]\naudit = 'evet'\n", "audit true ya da false"),
    ("[video\n", "TOML olarak okunamadı"),
])
def test_preset_errors(tmp_path, text, message):
    with pytest.raises(bp.PresetError, match=message):
        bp.load_preset(preset_file(tmp_path, text))


def test_preset_integer_accepted_for_float_option(tmp_path):
    preset = bp.load_preset(preset_file(tmp_path, "[app]\nvideo_source_fallback_min_relevance = 1\n"))
    assert preset.app == {"video_source_fallback_min_relevance": 1}


def test_video_options_become_cli_flags():
    argv = bp.video_option_argv({
        "match_materials_to_script": False,  # BooleanOptionalAction -> --no-...
        "nasa_credit_overlay": True,
        "confirm_wavespeed_charge": False,  # store_true: kapalıysa bayrak yok
        "video_language": "tr-TR",
        "stroke_width": 3,
    })
    assert argv == ["--no-match-materials-to-script", "--nasa-credit-overlay", "--video-language", "tr-TR",
                    "--stroke-width", "3"]


def test_task_argv_adds_prompt_and_row_voice():
    preset = bp.load_preset(PRESET)
    item = bp.ItemState(index=1, subject="Nötron yıldızları", instruction="Kısa tut.", voice="tr-TR-AhmetNeural-Male")
    argv = bp.build_task_argv(preset, item)
    assert argv[:2] == ["--video-subject", "Nötron yıldızları"]
    assert argv[argv.index("--video-script-prompt") + 1].endswith("\nKısa tut.")
    # Satırın sesi reçetedekinden sonra gelir; argparse sonuncuyu kullanır.
    assert bp._parse_cli(argv).voice_name == "tr-TR-AhmetNeural-Male"


# ---------------------------------------------------------------- çalıştırıcı

class FakeRunner:
    """Konuya göre başarılı ya da başarısız olan sahte görev; çağrı sırasını tutar."""

    def __init__(self, fail=(), fail_once=(), interrupt_at=None):
        self.calls, self.fail, self.fail_once, self.interrupt_at = [], set(fail), set(fail_once), interrupt_at

    def __call__(self, argv, task_id):
        subject = argv[argv.index("--video-subject") + 1]
        self.calls.append(subject)
        if self.interrupt_at == len(self.calls):
            raise KeyboardInterrupt
        logger.info(f"sahte görev günlüğü: {subject}")
        if subject in self.fail or (subject in self.fail_once and self.calls.count(subject) == 1):
            raise bp.TaskFailed(f"{subject} için ses yok", "audio")
        return {"videos": [f"{task_id}.mp4"]}


def fake_packager(calls=None):
    def package(item, result, preset, folder, log_lines):
        folder.mkdir(parents=True, exist_ok=True)
        if calls is not None:
            calls.append((item.subject, list(log_lines)))
        return {"kontrol_gerekli": item.subject.startswith("K"), "nedenler": []}
    return package


def run(source, runner, packager=None, **kwargs):
    return bp.run_batch(source, PRESET, task_runner=runner, packager=packager or fake_packager(),
                        reporter=lambda state, batch_dir: None, **kwargs)


def only_batch(tmp_path) -> Path:
    batches = list((tmp_path / "batches").iterdir())
    assert len(batches) == 1
    return batches[0]


def test_all_subjects_succeed(tmp_path):
    source = write(tmp_path / "Uzay listesi.txt", "Nötron yıldızları\nKara delikler\n")
    calls = []
    assert run(source, FakeRunner(), fake_packager(calls)) == 0
    batch = only_batch(tmp_path)
    assert batch.name.endswith("_uzay-listesi")
    state = bp.BatchState.load(batch)
    assert [(i.folder, i.status, i.attempts) for i in state.items] == [
        ("01-notron-yildizlari", "done", 1), ("02-kara-delikler", "done", 1)]
    assert state.finished and state.items[1].quality["kontrol_gerekli"]
    # Paketleyici yalnız kendi görevinin günlüğünü alır.
    assert calls[0][1] == ["sahte görev günlüğü: Nötron yıldızları"]
    assert (batch / bp.LOG_FILE).read_text(encoding="utf-8").count("sahte görev günlüğü") == 2


def test_preset_app_section_applies_to_this_process_only(tmp_path):
    source = write(tmp_path / "k.txt", "Işık\n")
    seen = {}

    def runner(argv, task_id):
        seen.update({key: config.app.get(key) for key in ("material_concurrency", "nasa_face_filter")})
        return {"videos": ["x.mp4"]}

    run(source, runner)
    assert seen == {"material_concurrency": 3, "nasa_face_filter": True}


def test_failed_subject_is_skipped_then_retried_once_at_the_end(tmp_path):
    source = write(tmp_path / "k.txt", "A\nB\nC\n")
    runner = FakeRunner(fail_once={"B"})
    assert run(source, runner) == 0
    assert runner.calls == ["A", "B", "C", "B"]
    item = bp.BatchState.load(only_batch(tmp_path)).items[1]
    assert (item.status, item.attempts, item.failures, item.error) == ("done", 2, 1, "")


def test_subject_failing_twice_stays_failed_and_rerun_tries_again(tmp_path):
    source = write(tmp_path / "k.txt", "A\nB\nC\n")
    runner = FakeRunner(fail={"B"})
    assert run(source, runner) == 1
    assert runner.calls == ["A", "B", "C", "B"]
    state = bp.BatchState.load(only_batch(tmp_path))
    item = state.items[1]
    assert (item.status, item.attempts, item.failures) == ("failed", 2, 2)
    assert (item.error, item.failed_stage) == ("B için ses yok", "audio")
    assert state.finished
    # Aynı komut: bitenler atlanır, başarısız konu yeniden denenir.
    again = FakeRunner()
    assert run(source, again) == 0
    assert again.calls == ["B"]
    assert bp.BatchState.load(only_batch(tmp_path)).items[1].status == "done"


def test_ctrl_c_stops_cleanly_and_same_command_resumes(tmp_path):
    source = write(tmp_path / "k.csv", "konu\nA\nB\nC\nD\n")
    first = FakeRunner(interrupt_at=3)
    assert run(source, first) == 130
    batch = only_batch(tmp_path)
    state = bp.BatchState.load(batch)
    assert [i.status for i in state.items] == ["done", "done", "pending", "pending"]
    assert not state.finished
    second = FakeRunner()
    assert run(source, second) == 0
    assert second.calls == ["C", "D"]
    assert only_batch(tmp_path) == batch
    assert all(i.status == "done" for i in bp.BatchState.load(batch).items)


def test_interrupted_attempt_counts_its_time(tmp_path, monkeypatch):
    ticks = iter(range(0, 10_000, 5))
    monkeypatch.setattr(bp, "time", SimpleNamespace(monotonic=lambda: next(ticks)))
    source = write(tmp_path / "k.txt", "A\n")
    assert run(source, FakeRunner(interrupt_at=1)) == 130
    item = bp.BatchState.load(only_batch(tmp_path)).items[0]
    assert (item.status, item.attempts) == ("pending", 1) and item.seconds > 0


def test_killed_run_leaves_running_item_which_resumes(tmp_path):
    source = write(tmp_path / "k.txt", "A\nB\n")
    run(source, FakeRunner(interrupt_at=1))
    batch = only_batch(tmp_path)
    state = bp.BatchState.load(batch)
    state.items[0].status = "running"  # süreç öldürüldüğünde dosyada kalan durum
    state.save(batch)
    runner = FakeRunner()
    assert run(source, runner) == 0
    assert runner.calls == ["A", "B"]


def test_packaging_error_marks_video_for_review(tmp_path):
    source = write(tmp_path / "k.txt", "A\n")

    def broken(**kwargs):
        raise OSError("disk dolu")

    assert run(source, FakeRunner(), broken) == 0
    item = bp.BatchState.load(only_batch(tmp_path)).items[0]
    assert item.status == "done" and item.quality == {"kontrol_gerekli": True,
                                                      "nedenler": ["paket hazırlanamadı: disk dolu"]}


def test_changed_input_starts_a_new_batch_and_yeni_forces_one(tmp_path):
    source = write(tmp_path / "k.txt", "A\n")
    run(source, FakeRunner())
    write(source, "A\nB\n")
    run(source, FakeRunner())
    assert len(list((tmp_path / "batches").iterdir())) == 2
    with patch.object(bp, "datetime") as clock:
        clock.now.return_value = datetime(2030, 1, 1, 12, 0, 0)
        run(source, FakeRunner(), new=True)
        run(source, FakeRunner(), new=True)  # aynı saniye: ayrı klasör
    assert (tmp_path / "batches" / "20300101-120000_k").is_dir()
    assert (tmp_path / "batches" / "20300101-120000_k-2").is_dir()


def test_output_folder_of_another_input_is_refused(tmp_path):
    output = tmp_path / "cikti"
    run(write(tmp_path / "a.txt", "A\n"), FakeRunner(), output=output)
    with pytest.raises(bp.BatchInputError, match="başka bir girdi"):
        run(write(tmp_path / "b.txt", "B\n"), FakeRunner(), output=output)


def test_reporter_runs_after_each_subject_and_at_the_end(tmp_path):
    source = write(tmp_path / "k.txt", "A\nB\n")
    reports = []
    bp.run_batch(source, PRESET, task_runner=FakeRunner(), packager=fake_packager(),
                 reporter=lambda state, batch_dir: reports.append([i.status for i in state.items]))
    assert reports == [["done", "pending"], ["done", "done"], ["done", "done"]]


def test_main_reports_preset_errors(tmp_path):
    source = write(tmp_path / "k.txt", "A\n")
    bad = preset_file(tmp_path, "[video]\nvideo_sorce = 'nasa'\n")
    messages = []
    handler = logger.add(lambda message: messages.append(message.record["message"]), level="ERROR")
    try:
        with patch("app.utils.logging_utils.configure_terminal_logger"):
            assert bp.main([str(source), "--preset", str(bad)]) == 2
    finally:
        logger.remove(handler)
    assert any("video_sorce" in message for message in messages)


@pytest.mark.skipif(not hasattr(signal, "SIGBREAK"), reason="Ctrl+Break yalnız Windows'ta")
def test_ctrl_break_stops_like_ctrl_c_and_handler_is_restored(tmp_path):
    source = write(tmp_path / "k.txt", "A\n")
    before = signal.getsignal(signal.SIGBREAK)
    seen = []

    def fake_run_batch(*args, **kwargs):
        seen.append(signal.getsignal(signal.SIGBREAK))
        return 130

    with patch.object(bp, "run_batch", fake_run_batch), patch("app.utils.logging_utils.configure_terminal_logger"):
        assert bp.main([str(source), "--preset", str(PRESET)]) == 130
    assert seen == [signal.default_int_handler]
    assert signal.getsignal(signal.SIGBREAK) == before


def test_launcher_dispatches_batch():
    with patch.object(bp, "main", return_value=0) as main, patch("app.utils.logging_utils.force_utf8_console"):
        assert launcher.main(["batch", "k.txt", "--preset", "presets/uzay.toml"]) == 0
    main.assert_called_once_with(["k.txt", "--preset", "presets/uzay.toml"], prog="mpt batch")


def test_state_file_is_readable_json(tmp_path):
    source = write(tmp_path / "k.txt", "Nötron yıldızları\n")
    run(source, FakeRunner())
    data = json.loads((only_batch(tmp_path) / bp.STATE_FILE).read_text(encoding="utf-8"))
    assert data["items"][0]["subject"] == "Nötron yıldızları" and data["version"] == bp.STATE_VERSION
