"""
Toplu Üretim bölümü: konu listesi, reçete, başlatma/durdurma, canlı durum ve geçmiş.

Bölüm config.toml'a hiçbir şey yazmaz. Konu listesinin taslağı storage/batches/.taslak.json'da
durur (sayfa yenilenince kaybolmaz); toplu iş batch_runner ile ayrı bir süreçte çalışır.
"""

import csv
import io
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import webbrowser
from pathlib import Path

import pandas as pd
import streamlit as st
from loguru import logger

from app import paths
from app.config import config
from app.services import batch_production as bp
from app.services import batch_runner, voice

VIEW_PARAM = "view"
VIEW_BATCH = "batch"
REFRESH_SECONDS = 3
DRAFT_FILE = ".taslak.json"
COLUMNS = ("konu", "talimat", "ses")
DEFAULT_PRESET = "uzay.toml"
HISTORY_LIMIT = 10
LOG_LINES = 12
_LOOPBACK = {"127.0.0.1", "localhost", "::1"}
_LOG_LINE = re.compile(r"\d{4}-\d{2}-\d{2} (\d{2}:\d{2}:\d{2})\S* \| (\w+)\s*\| .*?: \S+ - (.*)")


# ---------------------------------------------------------------- liste

def _cell(value) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return ""
    return str(value).strip()


def normalize_rows(rows) -> list[dict]:
    """Düzenleyiciden ya da taslaktan gelen satırlar: üç sütun, boşluklar kırpılmış metin."""
    return [{column: _cell(row.get(column)) for column in COLUMNS} for row in rows or []]


def validate_rows(rows: list[dict], voices: list[str]) -> tuple[list[dict], list[tuple[str, dict]]]:
    """
    Tamamen boş satırlar atlanır. Hatalar (anahtar, alanlar) olarak döner: konusu boş satır,
    tekrar eden konu (büyük/küçük harf duyarsız), listede olmayan ses, hiç konu yok.
    """
    items, errors, seen = [], [], {}
    known = set(voices)
    for number, row in enumerate(normalize_rows(rows), start=1):
        if not any(row.values()):
            continue
        subject = row["konu"]
        if not subject:
            errors.append(("batch.error.empty_subject", {"row": number}))
            continue
        key = subject.casefold()
        if key in seen:
            errors.append(("batch.error.duplicate", {"row": number, "subject": subject, "first": seen[key]}))
            continue
        seen[key] = number
        if row["ses"] and row["ses"] not in known:
            errors.append(("batch.error.voice", {"row": number, "voice": row["ses"]}))
            continue
        items.append(row)
    if not items and not errors:
        errors.append(("batch.error.empty_list", {}))
    return items, errors


def rows_csv(rows: list[dict]) -> bytes:
    """batch.py'nin okuduğu biçim: UTF-8-BOM, ';' ayırıcı, konu;talimat;ses başlığı."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";", lineterminator="\r\n")
    writer.writerow(COLUMNS)
    for row in normalize_rows(rows):
        if any(row.values()):
            writer.writerow([row[column] for column in COLUMNS])
    return buffer.getvalue().encode("utf-8-sig")


def write_input(rows: list[dict], batch_dir: Path) -> Path:
    batch_dir.mkdir(parents=True, exist_ok=True)
    path = batch_dir / "konular.csv"
    path.write_bytes(rows_csv(rows))
    return path


def import_rows(name: str, data: bytes) -> list[dict]:
    """Yüklenen .csv/.txt dosyası batch.py'nin ayrıştırıcısıyla okunur (aynı kurallar)."""
    suffix = Path(name).suffix.lower()
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / f"girdi{suffix}"
        path.write_bytes(data)
        items = bp.read_inputs(path)
    return [{"konu": item.subject, "talimat": item.instruction, "ses": item.voice} for item in items]


def draft_path() -> Path:
    return bp.batches_dir() / DRAFT_FILE


def load_draft() -> dict:
    try:
        data = json.loads(draft_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_draft(rows: list[dict], preset: str) -> None:
    path = draft_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps({"rows": normalize_rows(rows), "preset": preset}, ensure_ascii=False), encoding="utf-8")
    os.replace(temp, path)


# ---------------------------------------------------------------- reçete ve sesler

def presets_dir() -> Path:
    root = paths.SOURCE_ROOT if paths.is_source_checkout() else paths.runtime_root()
    return Path(root) / "presets"


def preset_files() -> list[Path]:
    return sorted(presets_dir().glob("*.toml"))


def voices_for(language: str) -> list[str]:
    return voice.get_all_azure_voices(filter_locals=[language] if language else None)


def preset_summary(preset: bp.Preset, tr) -> list[tuple[str, str]]:
    video = preset.video
    default = tr("batch.preset.default")
    source = video.get("video_source") or default
    if video.get("video_source_fallback"):
        source = f"{source} → {video['video_source_fallback']}"
    words = f"{preset.target_words[0]}–{preset.target_words[1]}" if preset.target_words else default
    llm = video.get("llm_provider") or default
    if video.get("llm_model"):
        llm = f"{llm} / {video['llm_model']}"
    return [
        (tr("batch.preset.source"), source),
        (tr("batch.preset.voice"), video.get("voice_name") or default),
        (tr("batch.preset.aspect"), video.get("video_aspect") or default),
        (tr("batch.preset.words"), words),
        (tr("batch.preset.llm"), llm),
    ]


# ---------------------------------------------------------------- yerel açma

def local_access() -> bool:
    """Rapor ve klasör sunucu makinesinde açılır; yalnız masaüstü olan ve döngü adresinde dinleyen kurulumda."""
    if not (sys.platform.startswith("win") or sys.platform == "darwin"
            or os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return False
    address = str(st.get_option("server.address") or config.listen_host or "").strip()
    if address not in _LOOPBACK:
        return False
    client = getattr(st.context, "ip_address", None)
    return client is None or client in _LOOPBACK


def _inside_batches(path: Path) -> bool:
    root = bp.batches_dir().resolve()
    return path.resolve() == root or root in path.resolve().parents


def open_report(path: Path) -> None:
    if _inside_batches(path) and path.is_file():
        webbrowser.open(path.resolve().as_uri())


def open_folder(path: Path) -> None:
    if not (_inside_batches(path) and path.is_dir()):
        return
    if sys.platform.startswith("win"):
        os.startfile(str(path))  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


# ---------------------------------------------------------------- biçim

def format_duration(seconds: float | None) -> str:
    seconds = int(round(seconds or 0))
    hours, rest = divmod(seconds, 3600)
    minutes, seconds = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"


def _status_label(item: batch_runner.ItemStatus, tr) -> str:
    if item.status == "running":
        stage = tr(f"batch.stage.{item.stage}") if item.stage else tr("batch.stage.preparing")
        return tr("batch.status.running").format(stage=stage)
    return tr(f"batch.status.{item.status}")


def status_table(status: batch_runner.BatchStatus, tr) -> pd.DataFrame:
    return pd.DataFrame([
        {
            "#": item.index,
            tr("batch.col.subject"): item.subject,
            tr("batch.col.status"): _status_label(item, tr),
            tr("batch.col.time"): format_duration(item.seconds) if item.seconds else "",
            tr("batch.col.attempts"): item.attempts,
            tr("batch.col.note"): item.note,
        }
        for item in status.items
    ])


def short_log_line(line: str) -> str:
    match = _LOG_LINE.match(line)
    return f"{match[1]} {match[2]:<7} {match[3]}" if match else line


def _flash(kind: str, message: str) -> None:
    st.session_state["batch_flash"] = (kind, message)


# ---------------------------------------------------------------- bölümler

def _init_session() -> None:
    if "batch_rows" in st.session_state:
        return
    draft = load_draft()
    st.session_state["batch_rows"] = normalize_rows(draft.get("rows"))
    st.session_state["batch_saved_rows"] = st.session_state["batch_rows"]
    st.session_state["batch_editor_version"] = 0
    st.session_state["batch_upload_version"] = 0
    names = [path.name for path in preset_files()]
    preferred = draft.get("preset") if draft.get("preset") in names else DEFAULT_PRESET
    if preferred in names:
        st.session_state["batch_preset_select"] = preferred


def _replace_rows(rows: list[dict]) -> None:
    """Düzenleyicinin temel verisini değiştirir; anahtar değişir, böylece eski düzenlemeler uygulanmaz."""
    st.session_state["batch_rows"] = normalize_rows(rows)
    st.session_state["batch_editor_version"] += 1


def _last_error_line(batch_dir: Path) -> str:
    lines = batch_runner.log_tail(batch_dir)
    errors = [line for line in lines if re.search(r"\| (ERROR|CRITICAL)\s*\|", line)]
    return short_log_line((errors or lines or [""])[-1])


def ended_message(batch_dir: Path, tr) -> tuple[str, str]:
    """Süreç bitince sonucu klasörden okur: bitti, yarım kaldı ya da hiç başlayamadı (son hata satırıyla)."""
    try:
        state = bp.BatchState.load(batch_dir)
    except (OSError, ValueError, TypeError):
        return "error", tr("batch.error.exited").format(line=_last_error_line(batch_dir) or batch_dir.name)
    if state.finished:
        return "success", tr("batch.finished").format(name=batch_dir.name)
    return "warning", tr("batch.stop.done")


@st.fragment(run_every=REFRESH_SECONDS)
def _render_running(tr) -> None:
    lock = batch_runner.current_lock()
    if not lock:
        st.rerun(scope="app")  # render_batch_page sonucu gösterir
        return
    batch_dir = Path(lock.get("batch_dir") or "")
    st.session_state["batch_running_dir"] = str(batch_dir)
    with st.container(border=True):
        st.markdown(f"**{tr('batch.running.header').format(name=batch_dir.name)}**")
        try:
            status = batch_runner.batch_status(batch_dir, lock=lock)
        except (OSError, ValueError, TypeError):
            st.info(tr("batch.running.starting"))
            status = None
        if status is not None:
            total = len(status.items) or 1
            finished = status.count("done") + status.count("review") + status.count("failed")
            fields = {"done": finished, "total": len(status.items), "elapsed": format_duration(status.elapsed),
                      "remaining": format_duration(status.remaining)}
            key = "batch.running.progress" if status.remaining is not None else "batch.running.progress_noestimate"
            st.progress(finished / total, text=tr(key).format(**fields))
            st.dataframe(status_table(status, tr), hide_index=True, width="stretch")
            if status.log_tail:
                st.caption(tr("batch.log.header"))
                st.code("\n".join(short_log_line(line) for line in status.log_tail[-LOG_LINES:]), language=None,
                        wrap_lines=True)
        if st.button(tr("batch.stop"), key="batch_stop", icon=":material/stop_circle:"):
            with st.spinner(tr("batch.stop.stopping")):
                outcome = batch_runner.stop_batch()
            st.session_state.pop("batch_running_dir", None)
            if outcome == "terminated":
                _flash("warning", tr("batch.stop.forced"))
            else:
                _flash("success", tr("batch.stop.done"))
            st.rerun(scope="app")


@st.fragment
def _render_editor(tr, running: bool) -> None:
    st.markdown(f"#### {tr('batch.list.header')}")
    files = preset_files()
    names = [path.name for path in files]
    preset, preset_path = None, None
    if not names:
        st.warning(tr("batch.preset.none").format(folder=presets_dir()))
    else:
        name = st.selectbox(tr("batch.preset.label"), names, key="batch_preset_select")
        preset_path = presets_dir() / name
        try:
            preset = bp.load_preset(preset_path)
        except bp.PresetError as exc:
            st.error(tr("batch.preset.error").format(error=exc))
    language = (preset.video.get("video_language") if preset else "") or ""
    voices = voices_for(language)
    if preset is not None:
        summary = preset_summary(preset, tr)
        st.dataframe(pd.DataFrame(summary, columns=[tr("batch.preset.setting"), tr("batch.preset.value")]),
                     hide_index=True, width="stretch")

    notice = st.session_state.pop("batch_editor_flash", None)
    if notice:
        getattr(st, notice[0])(notice[1])
    # Temizleme düzenleyiciden önce işlenir: düzenleyici aynı çalıştırmada yeni (boş) veriyle çizilir.
    uploaded = st.file_uploader(tr("batch.list.import"), type=["csv", "txt"],
                                key=f"batch_upload_{st.session_state['batch_upload_version']}")
    if uploaded is not None:
        try:
            imported = import_rows(uploaded.name, uploaded.getvalue())
        except bp.BatchInputError as exc:
            st.error(tr("batch.list.import_error").format(error=exc))
        else:
            # Yükleme kutusu yeni anahtarla boşalır; eski kutu ekranda kalırsa sonraki yükleme kaybolurdu.
            _replace_rows(imported)
            st.session_state["batch_upload_version"] += 1
            st.session_state["batch_editor_flash"] = ("success", tr("batch.list.imported").format(count=len(imported)))
            st.rerun()
    if st.button(tr("batch.list.clear"), key="batch_clear", icon=":material/delete_sweep:"):
        _replace_rows([])

    base = pd.DataFrame(st.session_state["batch_rows"], columns=list(COLUMNS))
    edited = st.data_editor(
        base,
        key=f"batch_editor_{st.session_state['batch_editor_version']}",
        num_rows="dynamic",
        hide_index=True,
        width="stretch",
        column_config={
            "konu": st.column_config.TextColumn(tr("batch.col.subject"), width="large"),
            "talimat": st.column_config.TextColumn(tr("batch.col.instruction"), width="large"),
            "ses": st.column_config.SelectboxColumn(tr("batch.col.voice"), options=voices,
                                                    help=tr("batch.col.voice_help")),
        },
    )
    rows = normalize_rows(edited.to_dict("records"))
    selected_preset = st.session_state.get("batch_preset_select", "")
    if rows != st.session_state.get("batch_saved_rows") or selected_preset != st.session_state.get("batch_saved_preset"):
        save_draft(rows, selected_preset)
        st.session_state["batch_saved_rows"] = rows
        st.session_state["batch_saved_preset"] = selected_preset
    st.caption(tr("batch.list.draft_note"))

    with st.container(horizontal=True, gap="small"):
        start = st.button(tr("batch.start"), key="batch_start", type="primary", icon=":material/play_arrow:",
                          disabled=running or preset is None)
        st.download_button(tr("batch.list.export"), data=rows_csv(rows), file_name="konular.csv", mime="text/csv",
                           key="batch_export", icon=":material/download:", on_click="ignore")
    if running:
        st.caption(tr("batch.start.disabled_running"))
    if start:
        items, errors = validate_rows(rows, voices)
        if errors:
            st.error("\n".join(f"- {tr(key).format(**fields)}" for key, fields in errors))
            return
        try:
            batch_dir = bp.new_batch_dir(Path("webui"))
            input_path = write_input(items, batch_dir)
            batch_runner.start_batch(input_path, preset_path, batch_dir)
        except batch_runner.BatchBusy as exc:
            name = Path(exc.lock.get("batch_dir") or "").name
            st.error(tr("batch.error.busy").format(name=name))
            return
        except OSError as exc:
            logger.exception(f"toplu üretim başlatılamadı: {exc}")
            st.error(tr("batch.error.start").format(error=exc))
            return
        st.session_state["batch_running_dir"] = str(batch_dir)  # süreç hemen düşerse hata yine görünür
        _flash("success", tr("batch.start.started").format(name=batch_dir.name))
        st.rerun(scope="app")


def _history_counts(status: batch_runner.BatchStatus, tr) -> str:
    return tr("batch.history.counts").format(
        total=len(status.items), done=status.count("done") + status.count("review"),
        failed=status.count("failed"), review=status.count("review"),
    )


@st.fragment
def _render_history(tr, running: bool) -> None:
    st.markdown(f"#### {tr('batch.history.header')}")
    batches = batch_runner.list_batches(HISTORY_LIMIT)
    if not batches:
        st.caption(tr("batch.history.empty"))
        return
    local = local_access()
    for status in batches:
        folder = status.directory
        state = "running" if status.running else ("finished" if status.finished else "unfinished")
        with st.container(border=True, key=f"batch_history_{folder.name}"):
            columns = st.columns([5, 1.4, 1.4, 1.4], vertical_alignment="center")
            created = status.created.replace("T", " ")[:16]
            columns[0].markdown(f"**{created}** · `{folder.name}` · {tr(f'batch.history.state.{state}')}  \n"
                                f"{_history_counts(status, tr)}")
            report = folder / bp.REPORT_FILE
            if columns[1].button(tr("batch.history.open_report"), key=f"batch_report_{folder.name}",
                                 disabled=not report.exists()):
                if local:
                    open_report(report)
                else:
                    st.info(tr("batch.history.remote_path").format(path=report))
            if columns[2].button(tr("batch.history.open_folder"), key=f"batch_folder_{folder.name}"):
                if local:
                    open_folder(folder)
                else:
                    st.info(tr("batch.history.remote_path").format(path=folder))
            if state == "unfinished" and columns[3].button(tr("batch.history.resume"), key=f"batch_resume_{folder.name}",
                                                           disabled=running, type="primary"):
                try:
                    batch_runner.resume_batch(folder)
                except batch_runner.ResumeError as exc:
                    st.error(tr(f"batch.resume.{exc.reason}").format(path=exc.path))
                    continue
                except batch_runner.BatchBusy as exc:
                    st.error(tr("batch.error.busy").format(name=Path(exc.lock.get("batch_dir") or "").name))
                    continue
                st.session_state["batch_running_dir"] = str(folder)
                _flash("success", tr("batch.history.resumed").format(name=folder.name))
                st.rerun(scope="app")


def render_batch_page(tr) -> None:
    _init_session()
    st.subheader(tr("batch.title"))
    st.caption(tr("batch.intro"))
    flash = st.session_state.pop("batch_flash", None)
    if flash:
        getattr(st, flash[0])(flash[1])
    running = batch_runner.current_lock() is not None
    previous = None if running else st.session_state.pop("batch_running_dir", None)
    if previous:  # bu oturumun izlediği toplu iş bitti
        kind, message = ended_message(Path(previous), tr)
        getattr(st, kind)(message)
    if running:
        _render_running(tr)
    _render_editor(tr, running)
    _render_history(tr, running)
