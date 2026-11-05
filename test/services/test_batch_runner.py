"""batch_runner：进程启动参数、锁与过期锁、停止、继续和状态读取。真实的 batch.py 进程用假 Popen 代替。"""

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from app import paths
from app.config import config
from app.services import batch_production as bp
from app.services import batch_runner as br

PRESET = Path(__file__).resolve().parents[2] / "presets" / "uzay.toml"
FAKE_PID = 424242
FAKE_START = 133_000_000_000_000_000


@pytest.fixture(autouse=True)
def batches(tmp_path, monkeypatch):
    """Her test kendi storage/batches'ını kullanır; run_batch reçetenin [app] bölümünü config.app'e
    yazdığı için config.app sonra geri alınır (yoksa sonraki testler eşzamanlılığı 3 görür)."""
    folder = tmp_path / "batches"
    folder.mkdir()
    monkeypatch.setattr(bp, "batches_dir", lambda: folder)
    br._children.clear()
    with patch.dict(config.app, {}):
        yield folder


class Processes:
    """process_start_time 的替身：alive 里的 PID 视为存活。"""

    def __init__(self, monkeypatch, *alive):
        self.alive = {pid: FAKE_START for pid in alive}
        monkeypatch.setattr(br, "process_start_time", lambda pid: self.alive.get(pid))


class FakePopen:
    calls = []

    def __init__(self, args, **kwargs):
        FakePopen.calls.append((args, kwargs))
        self.pid = FAKE_PID
        self.killed = False

    def poll(self):
        return None

    def kill(self):
        self.killed = True


def write_lock(pid=FAKE_PID, created=FAKE_START, batch_dir="x"):
    br.lock_path().write_text(json.dumps({"pid": pid, "created": created, "batch_dir": str(batch_dir),
                                          "started": "2026-11-05T10:00:00"}), encoding="utf-8")


def make_batch(folder: Path, subjects=("A", "B")) -> tuple[Path, bp.BatchState]:
    folder.mkdir(parents=True, exist_ok=True)
    source = folder / "konular.csv"
    source.write_text("konu\n" + "\n".join(subjects) + "\n", encoding="utf-8")
    state = bp.new_state(source, bp.load_preset(PRESET), bp.read_inputs(source))
    state.save(folder)
    return source, state


# ---------------------------------------------------------------- süreç ve komut

def test_process_start_time_of_live_and_finished_processes():
    assert br.process_start_time(os.getpid()) is not None
    finished = subprocess.Popen([sys.executable, "-c", "pass"])
    finished.wait()
    assert br.process_start_time(finished.pid) is None
    assert br.process_start_time(0) is None and br.process_start_time("12") is None


def test_command_runs_batch_py_with_fixed_arguments(tmp_path):
    command = br.batch_command(tmp_path / "k.csv", PRESET, tmp_path / "out")
    assert command == [sys.executable, "-X", "utf8", str(paths.SOURCE_ROOT / "batch.py"), str(tmp_path / "k.csv"),
                       "--preset", str(PRESET), "--output", str(tmp_path / "out"), "--gunluk-stderr"]


@pytest.mark.parametrize("translations, tail", [
    (None, ["--gunluk-stderr"]),
    (["en", "de"], ["--gunluk-stderr", "--altyazi-cevirileri", "en,de"]),
    ([], ["--gunluk-stderr", "--altyazi-cevirileri", ""]),
])
def test_command_passes_the_batch_translations(tmp_path, translations, tail):
    command = br.batch_command(tmp_path / "k.csv", PRESET, tmp_path, translations)
    assert command[-len(tail):] == tail


def test_installed_package_runs_the_module(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "is_source_checkout", lambda: False)
    assert br.batch_command(tmp_path / "k.csv", PRESET, tmp_path)[3:5] == ["-m", "app.services.batch_production"]


def test_start_spawns_a_new_process_group_logging_to_batch_log(tmp_path, monkeypatch):
    Processes(monkeypatch, FAKE_PID)
    FakePopen.calls.clear()
    monkeypatch.setattr(br.subprocess, "Popen", FakePopen)
    folder = tmp_path / "batch"
    assert br.start_batch(tmp_path / "k.csv", PRESET, folder) == FAKE_PID
    (args, kwargs), = FakePopen.calls
    assert args == br.batch_command(tmp_path / "k.csv", PRESET, folder) and "shell" not in kwargs
    assert kwargs["stdin"] is subprocess.DEVNULL and kwargs["stderr"] is subprocess.STDOUT
    assert Path(kwargs["stdout"].name) == folder / bp.LOG_FILE and kwargs["stdout"].closed
    assert kwargs["env"]["PYTHONIOENCODING"] == "utf-8" and kwargs["cwd"] == str(paths.SOURCE_ROOT)
    if sys.platform.startswith("win"):
        assert kwargs["creationflags"] & subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        assert kwargs["start_new_session"] is True
    lock = json.loads(br.lock_path().read_text(encoding="utf-8"))
    assert (lock["pid"], lock["created"], lock["batch_dir"]) == (FAKE_PID, FAKE_START, str(folder.resolve()))
    assert lock["command"] == args


# ---------------------------------------------------------------- kilit

def test_live_lock_refuses_a_second_batch(tmp_path, monkeypatch):
    Processes(monkeypatch, FAKE_PID)
    write_lock(batch_dir=tmp_path / "running")
    with patch.object(br.subprocess, "Popen") as popen, pytest.raises(br.BatchBusy) as busy:
        br.start_batch(tmp_path / "k.csv", PRESET, tmp_path / "other")
    popen.assert_not_called()
    assert busy.value.lock["batch_dir"] == str(tmp_path / "running")


@pytest.mark.parametrize("lock", [
    {"pid": FAKE_PID, "created": FAKE_START},  # süreç ölmüş
    {"pid": 7, "created": FAKE_START - 1},  # PID başka bir sürece geçmiş (başlangıç zamanı farklı)
])
def test_stale_lock_is_removed(monkeypatch, lock):
    Processes(monkeypatch, 7)
    br.lock_path().write_text(json.dumps(lock), encoding="utf-8")
    assert br.current_lock() is None and not br.lock_path().exists()


def test_unreadable_lock_is_removed(monkeypatch):
    Processes(monkeypatch)
    br.lock_path().write_text("{yarım", encoding="utf-8")
    assert br.current_lock() is None and not br.lock_path().exists()


def test_real_dead_process_lock_is_stale():
    finished = subprocess.Popen([sys.executable, "-c", "pass"])
    finished.wait()
    write_lock(pid=finished.pid, created=0)
    assert br.current_lock() is None


def test_batch_process_accepts_the_lock_written_for_it_and_refuses_others(tmp_path, monkeypatch):
    Processes(monkeypatch, FAKE_PID, 99)
    write_lock(pid=FAKE_PID)
    assert br.acquire_lock(tmp_path, pid=FAKE_PID)["pid"] == FAKE_PID
    with pytest.raises(br.BatchBusy):
        br.acquire_lock(tmp_path, pid=99)
    br.release_lock(pid=99)  # başkasının kilidine dokunmaz
    assert br.lock_path().exists()
    br.release_lock(pid=FAKE_PID)
    assert not br.lock_path().exists()


def test_lock_written_for_the_launcher_is_accepted_by_its_interpreter(tmp_path, monkeypatch):
    """Windows'ta .venv\\Scripts\\python.exe başlatıcıdır: WebUI kilide onun PID'ini yazar."""
    Processes(monkeypatch, os.getppid(), os.getpid())
    write_lock(pid=os.getppid())
    assert br.acquire_lock(tmp_path)["pid"] == os.getppid()
    br.release_lock()
    assert not br.lock_path().exists()


def test_real_child_process_accepts_the_lock_written_with_its_popen_pid(tmp_path, batches):
    """WebUI'nin akışı: Popen, kilide Popen PID'i, sonra alt süreçteki batch.py kilidi alır."""
    go = tmp_path / "go"
    code = (
        "import json, sys, time\n"
        "from pathlib import Path\n"
        "from app.services import batch_production as bp, batch_runner as br\n"
        f"bp.batches_dir = lambda: Path({str(batches)!r})\n"
        f"while not Path({str(go)!r}).exists(): time.sleep(0.05)\n"
        "try:\n"
        f"    print(json.dumps(br.acquire_lock(Path({str(tmp_path)!r}))))\n"
        "    br.release_lock()\n"
        "except br.BatchBusy as exc:\n"
        "    print('busy', exc)\n"
    )
    child = subprocess.Popen([sys.executable, "-c", code], cwd=str(paths.SOURCE_ROOT), stdout=subprocess.PIPE,
                             text=True, encoding="utf-8", errors="replace",
                             env=dict(os.environ, PYTHONIOENCODING="utf-8"))
    write_lock(pid=child.pid, created=br.process_start_time(child.pid))
    go.write_text("", encoding="utf-8")
    output, _ = child.communicate(timeout=120)
    assert json.loads(output.strip().splitlines()[-1])["pid"] == child.pid, output
    assert not br.lock_path().exists()


def test_batch_process_replaces_a_stale_lock(tmp_path, monkeypatch):
    Processes(monkeypatch, 99)
    write_lock(pid=FAKE_PID)
    assert br.acquire_lock(tmp_path, pid=99)["pid"] == 99


def test_run_batch_holds_the_lock_while_running(tmp_path):
    source = tmp_path / "k.txt"
    source.write_text("A\n", encoding="utf-8")
    seen = []

    def runner(argv, task_id):
        seen.append(json.loads(br.lock_path().read_text(encoding="utf-8"))["pid"])
        return {"videos": ["x.mp4"]}

    bp.run_batch(source, PRESET, output=tmp_path / "out", task_runner=runner, hold_lock=True, log_file=False,
                 packager=lambda **kwargs: {}, reporter=lambda state, folder: None)
    assert seen == [os.getpid()] and not br.lock_path().exists()
    assert not (tmp_path / "out" / bp.LOG_FILE).exists()  # günlük stderr'den batch.log'a gider


def test_main_refuses_while_another_batch_runs(tmp_path, monkeypatch):
    Processes(monkeypatch, FAKE_PID, os.getpid())
    write_lock(pid=FAKE_PID)
    source = tmp_path / "k.txt"
    source.write_text("A\n", encoding="utf-8")
    with patch("app.utils.logging_utils.configure_terminal_logger"):
        assert bp.main([str(source), "--preset", str(PRESET), "--output", str(tmp_path / "out")]) == 3
    assert json.loads(br.lock_path().read_text(encoding="utf-8"))["pid"] == FAKE_PID


def test_main_passes_the_webui_log_option(tmp_path):
    source = tmp_path / "k.txt"
    source.write_text("A\n", encoding="utf-8")
    with patch.object(bp, "run_batch", return_value=0) as run, \
            patch("app.utils.logging_utils.configure_terminal_logger"):
        bp.main([str(source), "--preset", str(PRESET), "--gunluk-stderr"])
    assert run.call_args.kwargs["hold_lock"] is True and run.call_args.kwargs["log_file"] is False


# ---------------------------------------------------------------- durdurma

def test_stop_without_a_batch():
    assert br.stop_batch() == "not_running"


def test_stop_sends_ctrl_break_and_waits_for_a_clean_exit(tmp_path, monkeypatch):
    processes = Processes(monkeypatch, FAKE_PID)
    write_lock(batch_dir=tmp_path)
    sent = []

    def send(pid):
        sent.append(pid)
        processes.alive.clear()  # süreç Ctrl+Break ile çıkar
        return True

    monkeypatch.setattr(br, "_send_break", send)
    monkeypatch.setattr(br, "_terminate_tree", lambda pid: pytest.fail("sonlandırılmamalı"))
    assert br.stop_batch(timeout=1) == "stopped"
    assert sent == [FAKE_PID] and not br.lock_path().exists()


@pytest.mark.parametrize("break_delivered", [True, False])
def test_stop_terminates_when_the_batch_does_not_exit_and_keeps_state_resumable(tmp_path, monkeypatch,
                                                                                break_delivered):
    processes = Processes(monkeypatch, FAKE_PID)
    folder = tmp_path / "batch"
    _, state = make_batch(folder)
    state.items[0].status, state.items[1].status = "done", "running"
    state.save(folder)
    write_lock(batch_dir=folder)
    terminated = []
    monkeypatch.setattr(br, "STOP_POLL_SECONDS", 0.01)
    monkeypatch.setattr(br, "_send_break", lambda pid: break_delivered)
    monkeypatch.setattr(br, "_terminate_tree", lambda pid: (terminated.append(pid), processes.alive.clear()))
    assert br.stop_batch(timeout=0.05) == "terminated"
    assert terminated == [FAKE_PID] and not br.lock_path().exists()
    assert [item.status for item in bp.BatchState.load(folder).items] == ["done", "pending"]


# ---------------------------------------------------------------- devam

def test_resume_uses_the_same_input_preset_and_folder(tmp_path, monkeypatch):
    folder = tmp_path / "batch"
    source, _ = make_batch(folder)
    with patch.object(br, "start_batch", return_value=FAKE_PID) as start:
        assert br.resume_batch(folder) == FAKE_PID
    start.assert_called_once_with(source.resolve(), PRESET.resolve(), folder)


@pytest.mark.parametrize("change, reason", [
    (lambda source: source.unlink(), "missing_input"),
    (lambda source: source.write_text("konu\nC\n", encoding="utf-8"), "changed_input"),
])
def test_resume_refuses_a_missing_or_changed_input(tmp_path, change, reason):
    folder = tmp_path / "batch"
    source, _ = make_batch(folder)
    change(source)
    with patch.object(br, "start_batch") as start, pytest.raises(br.ResumeError) as error:
        br.resume_batch(folder)
    start.assert_not_called()
    assert error.value.reason == reason and error.value.path == str(source.resolve())


def test_resume_refuses_a_changed_preset(tmp_path):
    folder = tmp_path / "batch"
    make_batch(folder)
    state = bp.BatchState.load(folder)
    state.preset_sha256 = "0" * 64
    state.save(folder)
    with pytest.raises(br.ResumeError) as error:
        br.resume_batch(folder)
    assert error.value.reason == "changed_preset"


def test_resume_refuses_an_unreadable_state(tmp_path):
    (tmp_path / "batch").mkdir()
    (tmp_path / "batch" / bp.STATE_FILE).write_text("{", encoding="utf-8")
    with pytest.raises(br.ResumeError) as error:
        br.resume_batch(tmp_path / "batch")
    assert error.value.reason == "state"


# ---------------------------------------------------------------- durum

NOW = datetime(2026, 11, 5, 12, 0, 0)


def status_batch(folder: Path) -> Path:
    _, state = make_batch(folder, subjects=("Bitti", "Kontrol", "Hatalı", "Çalışan", "Bekleyen"))
    done, review, failed, running, _pending = state.items
    done.status, done.seconds, done.attempts = "done", 200.0, 1
    review.status, review.seconds, review.quality = "done", 300.0, {"kontrol_gerekli": True,
                                                                    "nedenler": ["doğruluk: 1 Y (yanıltıcı)"]}
    failed.status, failed.seconds, failed.error, failed.attempts = "failed", 100.0, "ses yok", 2
    running.status, running.seconds, running.attempts = "running", 20.0, 2
    running.started = (NOW - timedelta(seconds=90)).isoformat(timespec="seconds")
    state.run_seconds = 600.0
    state.save(folder)
    (folder / bp.LOG_FILE).write_text("\n".join([
        "2026-11-05 11:58:00 | INFO | \"./app/services/batch_production.py:461\": _process - toplu üretim [4/5] başladı: Çalışan",
        "2026-11-05 11:58:10 | INFO | \"./app/services/generation_progress.py:45\": publish - generation activity: phase=script, provider=, remote_id=, elapsed=0s",
        "MoviePy - Writing video  45%|####      |",
        "2026-11-05 11:59:00 | INFO | \"./app/services/generation_progress.py:45\": publish - generation activity: phase=downloading, provider=, remote_id=, elapsed=3s",
    ]) + "\n", encoding="utf-8")
    return folder


def test_status_of_every_subject_while_running(tmp_path, monkeypatch):
    Processes(monkeypatch, FAKE_PID)
    folder = status_batch(tmp_path / "batch")
    lock = {"pid": FAKE_PID, "created": FAKE_START, "batch_dir": str(folder.resolve()),
            "started": (NOW - timedelta(seconds=300)).isoformat(timespec="seconds")}
    status = br.batch_status(folder, lock=lock, now=NOW)
    assert status.running and not status.unfinished
    assert [(item.status, item.stage) for item in status.items] == [
        ("done", ""), ("review", ""), ("failed", ""), ("running", "materials"), ("pending", "")]
    assert status.items[1].note == "doğruluk: 1 Y (yanıltıcı)" and status.items[2].note == "ses yok"
    assert status.items[3].seconds == 110.0  # önceki deneme 20 sn + şimdiki 90 sn
    assert status.elapsed == 900.0  # önceki çalıştırmalar 600 sn + bu çalıştırma 300 sn
    # Ortalama (200 + 300 + 100) / 3 = 200 sn: bekleyen 1 konu + çalışanın kalan 110 sn'si.
    assert status.remaining == 310.0
    assert all(line.startswith("2026-11-05") for line in status.log_tail) and len(status.log_tail) == 3


def test_status_after_the_process_died_shows_the_cut_subject_as_waiting(tmp_path, monkeypatch):
    Processes(monkeypatch)
    folder = status_batch(tmp_path / "batch")
    status = br.batch_status(folder, now=NOW)
    assert not status.running and status.unfinished and status.remaining is None
    assert [item.status for item in status.items][3:] == ["pending", "pending"]
    assert status.elapsed == 600.0 and status.log_tail == []


def test_current_stage_follows_only_the_subjects_own_lines():
    lines = [
        "x toplu üretim [1/2] başladı: A", "x generation activity: phase=rendering",
        "x toplu üretim [1] paket hazırlanıyor", "x toplu üretim [2/2] başladı: B",
        "x generation activity: phase=terms", "x generation activity: phase=bilinmeyen",
    ]
    assert br.current_stage(lines, 1) == "package"
    assert br.current_stage(lines, 2) == "terms"
    assert br.current_stage(lines, 3) == ""


def test_crash_line_reports_a_traceback_of_the_last_run_only(tmp_path):
    folder = tmp_path / "batch"
    folder.mkdir()
    start = "2026-11-05 20:39:35 | INFO | x: _run_in_folder - toplu üretim: 2 konu, 0 tanesi daha önce bitti, klasör x"
    crash = ["Traceback (most recent call last):", '  File "batch.py", line 9, in <module>',
             "PermissionError: [WinError 5] Erişim engellendi: 'state.json'"]
    (folder / bp.LOG_FILE).write_text("\n".join([start, *crash]) + "\n", encoding="utf-8")
    assert br.crash_line(folder) == "PermissionError: [WinError 5] Erişim engellendi: 'state.json'"
    # Devam ettirilen çalıştırmada eski Traceback sayılmaz.
    (folder / bp.LOG_FILE).write_text("\n".join([start, *crash, start, "2026-11-05 20:41:00 | INFO | x: y - z"]),
                                      encoding="utf-8")
    assert br.crash_line(folder) == ""


def test_list_batches_newest_first_and_skips_unreadable(batches, monkeypatch):
    Processes(monkeypatch)
    for name, created in (("eski", "2026-11-01T10:00:00"), ("yeni", "2026-11-05T10:00:00")):
        make_batch(batches / name)
        state = bp.BatchState.load(batches / name)
        state.created = created
        state.save(batches / name)
    (batches / "bozuk").mkdir()
    (batches / "bozuk" / bp.STATE_FILE).write_text("{", encoding="utf-8")
    assert [status.directory.name for status in br.list_batches()] == ["yeni", "eski"]
