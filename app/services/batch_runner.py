"""
Toplu üretimi arka planda çalıştırır ve izler（WebUI 用）。

- start_batch：batch.py 作为独立进程启动（不经过 shell，参数固定）；Windows 上建新进程组，
  这样可以用 Ctrl+Break 停止；stdout/stderr 写入批次文件夹的 batch.log。
- 锁文件 storage/batches/.lock：同一时间只运行一个批次。锁里记录进程 PID 和创建时间；
  进程已经不存在（或 PID 被别的进程复用）时锁视为过期并清除。batch.py 自己也获取同一把锁，
  所以命令行启动的批次同样受保护。
- stop_batch：先发 Ctrl+Break，等待一段时间仍未退出就结束整个进程树，并把 state.json 里
  “running”的主题改回“pending”，保证可以继续。
- resume_batch：用同一条命令（同一输入、预设和输出文件夹）继续未完成的批次。
- batch_status / list_batches：从 state.json 和 batch.log 读出每个主题的状态（含当前阶段）、
  用时和剩余时间估计。
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from loguru import logger

from app import paths
from app.services import batch_production as bp

LOCK_FILE = ".lock"
STOP_TIMEOUT_SECONDS = 30.0
STOP_POLL_SECONDS = 0.5
# 还没有完成的主题时用于估计剩余时间的单个主题时长（秒；uzay 预设的实测约 2.5–5 分钟）。
DEFAULT_ITEM_SECONDS = 240.0
LOG_TAIL_BYTES = 256 * 1024
_WINDOWS = sys.platform.startswith("win")
_STILL_ACTIVE = 259
_ITEM_STARTED = re.compile(r"toplu üretim \[(\d+)/\d+\] başladı")
_ITEM_PACKAGE = re.compile(r"toplu üretim \[(\d+)\] paket hazırlanıyor")
_PHASE = re.compile(r"generation activity: phase=(\w+)")
# 任务日志里的阶段 → 显示用阶段（WebUI 用 batch.stage.<阶段> 翻译）。
STAGES = {
    "preparing": "preparing", "script": "script", "terms": "terms", "audio": "audio", "subtitle": "subtitle",
    "materials": "materials", "downloading": "materials", "verifying": "materials", "rendering": "rendering",
    "package": "package",
}


class BatchBusy(RuntimeError):
    """Başka bir toplu üretim çalışıyor."""

    def __init__(self, lock: dict):
        super().__init__(f"başka bir toplu üretim çalışıyor: pid={lock.get('pid')}, {lock.get('batch_dir')}")
        self.lock = lock


class ResumeError(ValueError):
    """Toplu üretim sürdürülemiyor; reason WebUI çevirisinin anahtarıdır."""

    def __init__(self, reason: str, path: str = ""):
        super().__init__(f"{reason}: {path}" if path else reason)
        self.reason = reason
        self.path = path


# ---------------------------------------------------------------- süreç

def _windows_process_start(pid: int) -> int | None:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        # 无权限查询的进程仍然存在，只是拿不到创建时间。
        return 0 if ctypes.get_last_error() == 5 else None
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value != _STILL_ACTIVE:
            return None
        times = [wintypes.FILETIME() for _ in range(4)]
        if not kernel32.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
            return 0
        return (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
    finally:
        kernel32.CloseHandle(handle)


def process_start_time(pid: int) -> int | None:
    """Süreç yaşıyorsa başlangıç zamanı (bilinmiyorsa 0), yaşamıyorsa None."""
    if not isinstance(pid, int) or pid <= 0:
        return None
    if _WINDOWS:
        return _windows_process_start(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None
    except PermissionError:
        return 0
    try:
        return int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19])
    except (OSError, IndexError, ValueError):
        return 0


def _lock_alive(lock: dict) -> bool:
    started = process_start_time(lock.get("pid"))
    if started is None:
        return False
    recorded = lock.get("created") or 0
    # 两边都知道创建时间时必须一致，否则 PID 已被别的进程复用。
    return not (started and recorded and started != recorded)


# ---------------------------------------------------------------- kilit

def lock_path() -> Path:
    return bp.batches_dir() / LOCK_FILE


def _read_lock_file() -> dict | None:
    try:
        data = json.loads(lock_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _remove_lock() -> None:
    try:
        lock_path().unlink()
    except FileNotFoundError:
        pass


def current_lock() -> dict | None:
    """Çalışan toplu üretimin kilidi; kilit bayatsa (süreç ölmüş) silinir ve None döner."""
    lock = _read_lock_file()
    if lock is None:
        return None
    if lock and _lock_alive(lock):
        return lock
    logger.info(f"bayat toplu üretim kilidi temizlendi: {lock}")
    _remove_lock()
    return None


def _write_lock(lock: dict) -> bool:
    """Kilidi yalnız yoksa oluşturur (O_EXCL); oluşturduysa True."""
    try:
        descriptor = os.open(lock_path(), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(lock, handle, ensure_ascii=False)
    return True


def _lock_record(pid: int, batch_dir: Path, command: list[str] | None = None) -> dict:
    return {
        "pid": pid, "created": process_start_time(pid) or 0, "batch_dir": str(Path(batch_dir).resolve()),
        "started": datetime.now().isoformat(timespec="seconds"), "command": command or [],
    }


def _own_pids(pid: int | None) -> set[int]:
    """
    Bu sürecin kilitte görünebilecek PID'leri. Windows'ta .venv\\Scripts\\python.exe bir başlatıcıdır
    ve asıl yorumlayıcıyı alt süreç olarak açar: WebUI kilide başlatıcının PID'ini yazar (Ctrl+Break o
    süreç grubuna gider), batch.py ise yorumlayıcıda, yani başlatıcının alt sürecinde çalışır.
    """
    return {pid} if pid else {os.getpid(), os.getppid()}


def acquire_lock(batch_dir: Path, pid: int | None = None) -> dict:
    """
    Toplu üretim sürecinin kendi kilidi. Kilit bu süreç için yazılmışsa (WebUI başlattıysa) kabul
    edilir; başka bir canlı süreç tutuyorsa BatchBusy, bayatsa silinip yeniden alınır.
    """
    owners = _own_pids(pid)
    pid = pid or os.getpid()
    bp.batches_dir().mkdir(parents=True, exist_ok=True)
    for _ in range(3):
        record = _lock_record(pid, batch_dir)
        if _write_lock(record):
            return record
        existing = _read_lock_file()
        if existing and existing.get("pid") in owners and _lock_alive(existing):
            return existing
        if existing and _lock_alive(existing):
            raise BatchBusy(existing)
        _remove_lock()
    raise BatchBusy(_read_lock_file() or {})


def release_lock(pid: int | None = None) -> None:
    """Kilit bu sürecinse (ya da onu başlatan başlatıcınınsa) siler."""
    lock = _read_lock_file()
    if lock and lock.get("pid") in _own_pids(pid):
        _remove_lock()


# ---------------------------------------------------------------- başlatma

def batch_command(input_path: Path, preset_path: Path, batch_dir: Path,
                  translations: list[str] | tuple[str, ...] | None = None) -> list[str]:
    """
    Sabit argümanlı komut; günlük çağıranın açtığı batch.log'a stderr üzerinden gider. translations
    verilirse (boş liste = çeviri yok) reçetedeki çeviri dillerinin yerine geçer; state.json'a yazılır.
    """
    if paths.is_source_checkout():
        entry = [str(paths.SOURCE_ROOT / "batch.py")]
    else:
        entry = ["-m", "app.services.batch_production"]
    command = [sys.executable, "-X", "utf8", *entry, str(Path(input_path).resolve()), "--preset",
               str(Path(preset_path).resolve()), "--output", str(Path(batch_dir).resolve()), "--gunluk-stderr"]
    if translations is not None:
        command += ["--altyazi-cevirileri", ",".join(translations)]
    return command


_children: dict[int, subprocess.Popen] = {}


def start_batch(input_path: Path, preset_path: Path, batch_dir: Path,
                translations: list[str] | tuple[str, ...] | None = None) -> int:
    """batch.py'yi ayrı süreçte başlatır ve kilidi onun PID'iyle yazar; çalışan varsa BatchBusy."""
    lock = current_lock()
    if lock:
        raise BatchBusy(lock)
    batch_dir = Path(batch_dir)
    batch_dir.mkdir(parents=True, exist_ok=True)
    command = batch_command(input_path, preset_path, batch_dir, translations)
    options = {}
    if _WINDOWS:
        options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        options["start_new_session"] = True
    environment = dict(os.environ, PYTHONIOENCODING="utf-8")
    with open(batch_dir / bp.LOG_FILE, "a", encoding="utf-8") as log:
        process = subprocess.Popen(
            command, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
            cwd=str(paths.SOURCE_ROOT if paths.is_source_checkout() else paths.runtime_root()),
            env=environment, **options,
        )
    _children[process.pid] = process
    if not _write_lock(_lock_record(process.pid, batch_dir, command)):
        existing = _read_lock_file() or {}
        if existing.get("pid") != process.pid:
            # Aynı anda başka bir oturum da başlattı: bu süreç geri çekilir.
            process.kill()
            raise BatchBusy(existing)
    logger.info(f"toplu üretim başlatıldı: pid={process.pid}, {batch_dir}")
    return process.pid


def resume_batch(batch_dir: Path) -> int:
    """Yarım kalmış bir toplu üretimi aynı girdi, reçete ve klasörle sürdürür."""
    batch_dir = Path(batch_dir)
    try:
        state = bp.BatchState.load(batch_dir)
    except (OSError, ValueError, TypeError) as exc:
        raise ResumeError("state", str(batch_dir)) from exc
    input_path, preset_path = Path(state.input), Path(state.preset)
    for path, reason in ((input_path, "missing_input"), (preset_path, "missing_preset")):
        if not path.is_file():
            raise ResumeError(reason, str(path))
    if bp._file_sha256(input_path) != state.input_sha256:
        raise ResumeError("changed_input", str(input_path))
    if bp._file_sha256(preset_path) != state.preset_sha256:
        raise ResumeError("changed_preset", str(preset_path))
    return start_batch(input_path, preset_path, batch_dir)


# ---------------------------------------------------------------- durdurma

def _send_break(pid: int) -> bool:
    try:
        if _WINDOWS:
            os.kill(pid, signal.CTRL_BREAK_EVENT)
        else:
            os.killpg(pid, signal.SIGINT)
        return True
    except OSError as exc:
        # Windows'ta Ctrl+Break yalnız aynı konsolu paylaşan süreçlere gider (ör. WebUI başka
        # bir konsolda yeniden başlatıldıysa); o zaman süreç ağacı sonlandırılır.
        logger.warning(f"toplu üretime Ctrl+Break gönderilemedi: pid={pid}, {exc}")
        return False


def _terminate_tree(pid: int) -> None:
    if _WINDOWS:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, check=False, timeout=60)
    else:
        try:
            os.killpg(pid, signal.SIGKILL)
        except OSError:
            pass


def _wait_exit(lock: dict, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        child = _children.get(lock.get("pid"))
        if child is not None:
            child.poll()
        if not _lock_alive(lock):
            return True
        time.sleep(STOP_POLL_SECONDS)
    return not _lock_alive(lock)


def _settle_state(batch_dir: Path) -> None:
    """Sonlandırılan sürecin yarıda bıraktığı konuyu bekliyor'a çevirir (devam ettirilince baştan üretilir)."""
    try:
        state = bp.BatchState.load(batch_dir)
    except (OSError, ValueError, TypeError):
        return
    changed = False
    for item in state.items:
        if item.status == "running":
            item.status, changed = "pending", True
    if changed:
        state.finished = ""
        state.save(batch_dir)


def stop_batch(timeout: float = STOP_TIMEOUT_SECONDS) -> str:
    """
    Çalışan toplu üretimi durdurur: Ctrl+Break, süre içinde kapanmazsa süreç ağacını sonlandırır.
    Dönen değer: "not_running", "stopped" (temiz durdu) ya da "terminated".
    """
    lock = current_lock()
    if not lock:
        return "not_running"
    pid, batch_dir = lock["pid"], Path(lock.get("batch_dir") or "")
    if _send_break(pid) and _wait_exit(lock, timeout):
        outcome = "stopped"
    else:
        _terminate_tree(pid)
        _wait_exit(lock, 10)
        outcome = "terminated"
    if batch_dir.is_dir():
        _settle_state(batch_dir)
    if _read_lock_file() is not None and not _lock_alive(lock):
        _remove_lock()
    _children.pop(pid, None)
    logger.info(f"toplu üretim durduruldu ({outcome}): pid={pid}, {batch_dir}")
    return outcome


# ---------------------------------------------------------------- durum

@dataclass
class ItemStatus:
    index: int
    subject: str
    status: str  # pending | running | done | review | failed
    stage: str = ""
    seconds: float = 0.0
    attempts: int = 0
    note: str = ""
    folder: str = ""


@dataclass
class BatchStatus:
    directory: Path
    created: str
    finished: str
    running: bool
    items: list[ItemStatus] = field(default_factory=list)
    elapsed: float = 0.0
    remaining: float | None = None
    log_tail: list[str] = field(default_factory=list)

    def count(self, status: str) -> int:
        return sum(item.status == status for item in self.items)

    @property
    def unfinished(self) -> bool:
        return not self.finished and not self.running


def log_tail(batch_dir: Path, timestamped: bool = True) -> list[str]:
    try:
        with open(batch_dir / bp.LOG_FILE, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - LOG_TAIL_BYTES))
            data = handle.read()
    except OSError:
        return []
    lines = data.decode("utf-8", errors="replace").replace("\r", "\n").split("\n")
    if size > LOG_TAIL_BYTES:
        lines = lines[1:]  # ilk satır yarım olabilir
    if not timestamped:
        return [line for line in lines if line.strip()]
    # İlerleme çubukları ve Traceback gibi zaman damgasız satırlar atlanır.
    return [line for line in lines if re.match(r"\d{4}-\d{2}-\d{2} ", line)]


def crash_line(batch_dir: Path) -> str:
    """Günlükte son çalıştırmanın başlangıcından sonra Traceback varsa istisnanın son satırı."""
    lines = log_tail(batch_dir, timestamped=False)
    starts = [index for index, line in enumerate(lines) if "toplu üretim: " in line and " konu, " in line]
    tail = lines[starts[-1]:] if starts else lines
    if not any(line.startswith("Traceback") for line in tail):
        return ""
    return tail[-1].strip()


def current_stage(log_lines: list[str], index: int) -> str:
    """Günlükte konunun son başlangıcından sonraki son aşama; başka bir konu başlayınca izleme durur."""
    stage, active = "", False
    for line in log_lines:
        if match := _ITEM_STARTED.search(line):
            active = int(match[1]) == index
            if active:
                stage = "preparing"
        elif not active:
            continue
        elif (match := _ITEM_PACKAGE.search(line)) and int(match[1]) == index:
            stage = "package"
        elif match := _PHASE.search(line):
            stage = STAGES.get(match[1], stage)
    return stage


def _seconds_since(timestamp: str, now: datetime) -> float:
    try:
        return max(0.0, (now - datetime.fromisoformat(timestamp)).total_seconds())
    except (TypeError, ValueError):
        return 0.0


def batch_status(batch_dir: Path, lock: dict | None = None, now: datetime | None = None) -> BatchStatus:
    """state.json ve batch.log'dan konu başına durum, geçen süre ve kalan tahmini."""
    batch_dir = Path(batch_dir)
    now = now or datetime.now()
    state = bp.BatchState.load(batch_dir)
    lock = current_lock() if lock is None else lock
    running = bool(lock) and Path(lock.get("batch_dir") or "").resolve() == batch_dir.resolve()
    log_lines = log_tail(batch_dir) if running else []
    items = []
    current_extra = 0.0
    for item in state.items:
        note = item.error if item.status == "failed" else "; ".join(item.quality.get("nedenler", []))
        seconds = item.seconds
        if item.status == "done":
            status = "review" if item.quality.get("kontrol_gerekli") else "done"
        elif item.status == "failed":
            status = "failed"
        elif item.status == "running" and running:
            status = "running"
            current_extra = _seconds_since(getattr(item, "started", ""), now)
            seconds = item.seconds + current_extra
        else:
            status = "pending"  # süreç öldüyse yarıda kalan konu devam ettirilince baştan üretilir
        items.append(ItemStatus(item.index, item.subject, status, current_stage(log_lines, item.index)
                                if status == "running" else "", round(seconds, 1), item.attempts, note, item.folder))
    elapsed = state.run_seconds
    if running:
        elapsed += _seconds_since(lock.get("started", ""), now)
    remaining = None
    if running:
        finished = [item.seconds for item in state.items if item.status in ("done", "failed") and item.seconds]
        average = sum(finished) / len(finished) if finished else DEFAULT_ITEM_SECONDS
        waiting = sum(item.status == "pending" for item in items)
        current = max(0.0, average - current_extra) if any(item.status == "running" for item in items) else 0.0
        remaining = round(average * waiting + current, 0)
    return BatchStatus(batch_dir, state.created, state.finished, running, items, round(elapsed, 1), remaining,
                       log_lines[-40:])


def list_batches(limit: int = 20) -> list[BatchStatus]:
    """storage/batches altındaki toplu üretimler, en yeni önce."""
    lock = current_lock()
    statuses = []
    for state_file in bp.batches_dir().glob(f"*/{bp.STATE_FILE}"):
        try:
            statuses.append(batch_status(state_file.parent, lock=lock or {}))
        except (OSError, ValueError, TypeError) as exc:
            logger.warning(f"toplu üretim durumu okunamadı: {state_file.parent}, {exc}")
    statuses.sort(key=lambda status: status.created, reverse=True)
    return statuses[:limit]
