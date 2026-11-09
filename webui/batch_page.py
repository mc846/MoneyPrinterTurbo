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
import uuid
import webbrowser
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st
from loguru import logger

from app import paths
from app.config import config
from app.services import batch_package, batch_review, batch_runner, batch_subtitles, voice
from app.services import batch_production as bp

VIEW_PARAM = "view"
VIEW_BATCH = "batch"
REFRESH_SECONDS = 3
DRAFT_FILE = ".taslak.json"
COLUMNS = ("konu", "talimat", "ses")
# Tablodaki "ses seçilmedi" değeri. Streamlit'in SelectboxColumn'u "" değerini (seçeneklerde olsa da,
# format_func etiketi verilse de) boş değer sayıp gri "None" çizer; TextColumn çizmez. Tek boşluk boş
# görünür; normalize_rows hücreleri kırptığı için taslağa, CSV'ye ve doğrulamaya yine "" gider.
EMPTY_VOICE = " "
DEFAULT_PRESET = "uzay.toml"
HISTORY_LIMIT = 10
LOG_LINES = 12
# Bir konuda LLM işlemi (yeniden üretim, denetim) sürerken panel bu aralıkla yenilenir.
REVIEW_REFRESH_SECONDS = 2
RATING_COLORS = {"D": "green", "B": "blue", "Y": "orange", "X": "red"}
LIVE_STATUSES = ("running", "preparing", "rendering")
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
    """
    Taslak atomik yazılır. Geçici dosyanın adı her yazımda farklı: yeniden başlatılan WebUI'ya iki oturum aynı
    anda bağlanınca (açık sekme + yeni sekme) aynı adlı geçici dosyayı biri taşıyıp öteki bulamıyordu.
    """
    path = draft_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f"{path.name}.{uuid.uuid4().hex[:12]}.tmp")
    temp.write_text(json.dumps({"rows": normalize_rows(rows), "preset": preset}, ensure_ascii=False), encoding="utf-8")
    try:
        bp.replace_file(temp, path)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise


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
        (tr("batch.preset.translations"), ", ".join(preset.subtitle_translations) or tr("batch.preset.no_translations")),
    ]


def translation_options(preset: bp.Preset, language: str) -> list[str]:
    """Çeviri dilleri: bilinen diller (videonun dili hariç) ve reçetedeki listede olmayan kodlar."""
    source = (language or "tr").split("-")[0].lower()
    options = [code for code in batch_subtitles.LANGUAGES if code != source]
    return options + [code for code in preset.subtitle_translations if code not in options]


def language_label(code: str) -> str:
    names = batch_subtitles.LANGUAGES.get(code.split("-")[0])
    return f"{code} · {names[1]}" if names else code


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
    if item.status in LIVE_STATUSES:
        stage = tr(f"batch.stage.{item.stage}") if item.stage else tr("batch.stage.preparing")
        return tr(f"batch.status.{item.status}").format(stage=stage)
    return tr(f"batch.status.{item.status}")


def progress_counts(status: batch_runner.BatchStatus) -> tuple[int, int]:
    """(biten, toplam): hazırlıkta hazırlananlar, render'da onaylı kuyruktan çıkanlar biter sayılır."""
    rendering = any(item.status == "rendering" for item in status.items)
    open_statuses = {"pending", *LIVE_STATUSES} | ({"approved"} if rendering else set())
    total = len(status.items)
    return total - sum(item.status in open_statuses for item in status.items), total


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
    crashed = batch_runner.crash_line(batch_dir)
    if crashed:
        return "error", tr("batch.error.exited").format(line=crashed)
    status = batch_runner.batch_status(batch_dir, lock={})
    if status.awaiting_review:
        st.session_state["batch_review_dir"] = str(batch_dir)
        return "info", tr("batch.review.ready").format(name=batch_dir.name, count=status.count("awaiting_review"))
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
            finished, total = progress_counts(status)
            fields = {"done": finished, "total": total, "elapsed": format_duration(status.elapsed),
                      "remaining": format_duration(status.remaining)}
            key = "batch.running.progress" if status.remaining is not None else "batch.running.progress_noestimate"
            st.progress(finished / (total or 1), text=tr(key).format(**fields))
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
    translations: list[str] = []
    review_flow = False
    if preset is not None:
        summary = preset_summary(preset, tr)
        st.dataframe(pd.DataFrame(summary, columns=[tr("batch.preset.setting"), tr("batch.preset.value")]),
                     hide_index=True, width="stretch")
        # Yalnız bu toplu iş için: reçete dosyasına ve config.toml'a yazılmaz; batch.py'ye argüman olarak gider.
        options = translation_options(preset, language)
        translations = st.multiselect(
            tr("batch.translations.label"), options,
            default=[code for code in preset.subtitle_translations if code in options],
            format_func=language_label, key=f"batch_translations_{name}", help=tr("batch.translations.help"),
        )
        # İki aşamalı akış: reçeteden gelir, yalnız bu toplu iş için değiştirilebilir (batch.py --inceleme[-yok]).
        review_flow = st.checkbox(tr("batch.review.flow"), value=preset.review_before_render,
                                  key=f"batch_review_flow_{name}", help=tr("batch.review.flow_help"))

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

    base = pd.DataFrame([{**row, "ses": row["ses"] or EMPTY_VOICE} for row in st.session_state["batch_rows"]],
                        columns=list(COLUMNS))
    edited = st.data_editor(
        base,
        key=f"batch_editor_{st.session_state['batch_editor_version']}",
        num_rows="dynamic",
        hide_index=True,
        width="stretch",
        # Boş hücreler "None" yerine boş görünsün: metin sütunlarında varsayılan "", seste EMPTY_VOICE
        # (taslaktan yüklenen satırlarda da; boş = reçetedeki ses, ipucu başlıkta ve tablonun altında).
        column_config={
            "konu": st.column_config.TextColumn(tr("batch.col.subject"), width="large", default=""),
            "talimat": st.column_config.TextColumn(tr("batch.col.instruction"), width="large", default=""),
            "ses": st.column_config.SelectboxColumn(tr("batch.col.voice"), options=[EMPTY_VOICE, *voices],
                                                    default=EMPTY_VOICE, help=tr("batch.col.voice_help")),
        },
    )
    rows = normalize_rows(edited.to_dict("records"))
    selected_preset = st.session_state.get("batch_preset_select", "")
    if rows != st.session_state.get("batch_saved_rows") or selected_preset != st.session_state.get("batch_saved_preset"):
        save_draft(rows, selected_preset)
        st.session_state["batch_saved_rows"] = rows
        st.session_state["batch_saved_preset"] = selected_preset
    preset_voice = (preset.video.get("voice_name") if preset else "") or tr("batch.preset.default")
    st.caption(f"{tr('batch.col.voice_hint').format(voice=preset_voice)} {tr('batch.list.draft_note')}")

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
            batch_runner.start_batch(input_path, preset_path, batch_dir, translations=list(translations),
                                     review=review_flow)
        except batch_runner.BatchBusy as exc:
            name = Path(exc.lock.get("batch_dir") or "").name
            st.error(tr("batch.error.busy").format(name=name))
            return
        except OSError as exc:
            logger.exception(f"toplu üretim başlatılamadı: {exc}")
            st.error(tr("batch.error.start").format(error=exc))
            return
        st.session_state["batch_running_dir"] = str(batch_dir)  # süreç hemen düşerse hata yine görünür
        if review_flow:
            st.session_state["batch_review_dir"] = str(batch_dir)
        _flash("success", tr("batch.review.started" if review_flow else "batch.start.started").format(
            name=batch_dir.name))
        st.rerun(scope="app")


# ---------------------------------------------------------------- inceleme (iki aşamalı akış)

def _md(text) -> str:
    """İddia ve gerekçe metni Markdown/biçim yönergesi olarak yorumlanmasın."""
    return re.sub(r"([\\`*_\[\]#$<>|~])", r"\\\1", str(text or ""))


def _review_dir() -> Path | None:
    """İncelenen toplu iş: Geçmiş'ten seçilen ya da onay bekleyen konusu olan en yeni iki aşamalı toplu iş."""
    chosen = st.session_state.get("batch_review_dir")
    if chosen and (Path(chosen) / bp.STATE_FILE).is_file():
        return Path(chosen)
    for status in batch_runner.list_batches(HISTORY_LIMIT):
        if status.review and (status.count("awaiting_review") or status.count("approved")):
            return status.directory
    return None


def _review_keys(batch_dir: Path, entry: batch_review.ItemReview) -> dict:
    """
    Panel öğelerinin anahtarları. Metin, terim ve çapa anahtarları review.json'un sürümünü içerir: dosya
    değişince (yeniden üretim, onay denemesi) öğe yeni içerikle başlar; değişmedikçe kaydedilmemiş düzenleme
    oturumda kalır, sayfanın yenilenmesi onu silmez.
    """
    base = f"rv_{batch_dir.name}_{entry.item.index}"
    version = re.sub(r"\W", "", (entry.review.get("updated_at") or "")
                     + batch_review.script_sha256(entry.review["script"])[:12])
    return {"base": base, "text": f"{base}_text_{version}", "terms": f"{base}_terms_{version}",
            "anchors": f"{base}_anchors_{version}", "instruction": f"{base}_instruction"}


def _edits(batch_dir: Path, entry: batch_review.ItemReview) -> tuple[str, list[str], list[str]]:
    keys, review = _review_keys(batch_dir, entry), entry.review
    text = st.session_state.get(keys["text"], review["script"])
    terms = batch_review.parse_list(st.session_state.get(keys["terms"], "\n".join(review["terms"])))
    anchors = batch_review.parse_list(st.session_state.get(keys["anchors"], ", ".join(review["anchors"])))
    return text, terms, anchors


def has_unsaved_edits(batch_dir: Path, entry: batch_review.ItemReview) -> bool:
    if entry.review is None:
        return False
    text, terms, anchors = _edits(batch_dir, entry)
    return (batch_review.normalize_script(text) != entry.review["script"] or terms != entry.review["terms"]
            or anchors != entry.review["anchors"])


def _find_entry(batch_dir: Path, index: int) -> tuple[batch_review.Overview, batch_review.ItemReview] | None:
    lock = batch_runner.current_lock()
    running = bool(lock) and Path(lock.get("batch_dir") or "").resolve() == Path(batch_dir).resolve()
    try:
        view = batch_review.overview(batch_dir, running=running)
    except (OSError, ValueError, TypeError):
        return None
    entry = next((entry for entry in view.items if entry.item.index == index), None)
    return (view, entry) if entry else None


def _start_llm_action(tr, batch_dir: Path, entry: batch_review.ItemReview, action: str,
                      edits: tuple[str, list[str], list[str]] | None = None, instruction: str = "") -> None:
    """LLM gerektiren onay işlemi alt süreçte: batch.py <action> <klasör> <sıra> (CLI ile aynı fonksiyonlar)."""
    arguments = []
    if edits is not None:
        text, terms, anchors = edits
        if batch_review.normalize_script(text) != entry.review["script"]:
            edit_file = entry.folder / batch_review.EDIT_FILE
            edit_file.write_text(text, encoding="utf-8")
            arguments.append(f"--metin-dosyasi={edit_file}")
        if terms != entry.review["terms"]:
            arguments.append(f"--terimler={', '.join(terms)}")
        if anchors != entry.review["anchors"]:
            arguments.append(f"--capalar={', '.join(anchors)}")
    if instruction.strip():
        arguments.append(f"--talimat={instruction.strip()}")
    try:
        batch_runner.start_review_action(batch_dir, entry.folder, entry.item.index, action, arguments)
    except batch_review.ReviewBusy:
        _flash("error", tr("batch.review.error.busy"))
    except OSError as exc:
        logger.exception(f"onay işlemi başlatılamadı: {exc}")
        _flash("error", tr("batch.review.error.start").format(error=exc))


def _direct(tr, action, *args, **kwargs):
    """LLM gerektirmeyen onay işlemi bu süreçte; kural hatası arayüz mesajı olur."""
    try:
        return action(*args, **kwargs)
    except batch_review.ReviewError as exc:
        _flash("error", tr(f"batch.review.error.{exc.reason}"))
    return None


def _render_audit(tr, review: dict, stale: bool) -> None:
    audit = review.get("audit") or {}
    label = f"**{tr('batch.review.audit')}**"
    if audit.get("durum") == "tamam":
        counts = audit.get("sayim") or {}
        label += " " + " ".join(f":{RATING_COLORS[r]}-badge[{r} {counts.get(r, 0)}]" for r in batch_package.RATINGS)
    if stale:
        label += f" :gray-badge[{tr('batch.review.audit_stale')}]"
    st.markdown(label)
    if audit.get("durum") == "kapalı":
        st.caption(tr("batch.review.audit_off"))
        return
    if audit.get("durum") != "tamam":
        st.warning(tr("batch.review.audit_failed").format(error=audit.get("hata", "")))
        return
    for claim in audit.get("iddialar", []):
        color = RATING_COLORS.get(claim.get("rating"), "gray")
        st.markdown(f":{color}-badge[{claim.get('rating')}] {_md(claim.get('claim'))}  \n"
                    f":small[{_md(claim.get('reason'))}]")


def _review_item(tr, view: batch_review.Overview, entry: batch_review.ItemReview) -> None:
    """Onay bekleyen konunun paneli: düzenlenebilir metin, tahmin, denetim, terimler ve düğmeler."""
    batch_dir, item, review = view.batch_dir, entry.item, entry.review
    keys = _review_keys(batch_dir, entry)
    busy = entry.busy is not None
    preset = view.preset
    with st.container(border=True, key=f"batch_review_item_{item.index}"):
        st.markdown(f"**{item.index}. {_md(item.subject)}** :orange-badge[{tr('batch.status.awaiting_review')}]")
        for key, default in ((keys["text"], review["script"]), (keys["terms"], "\n".join(review["terms"])),
                             (keys["anchors"], ", ".join(review["anchors"]))):
            if key not in st.session_state:
                st.session_state[key] = default
        text = st.text_area(tr("batch.review.text"), key=keys["text"], height=230, disabled=busy)
        info = batch_review.estimate(text, (preset.video if preset else {}).get("voice_rate", 1.0))
        line = tr("batch.review.estimate").format(words=info["kelime"], seconds=f"{info['tahmini_sn']:.0f}")
        problems = []
        if preset and preset.target_words and not preset.target_words[0] <= info["kelime"] <= preset.target_words[1]:
            problems.append(tr("batch.review.words_out").format(low=preset.target_words[0],
                                                                 high=preset.target_words[1]))
        if preset and preset.target_duration and not (
                preset.target_duration[0] <= info["tahmini_sn"] <= preset.target_duration[1]):
            problems.append(tr("batch.review.duration_out").format(low=f"{preset.target_duration[0]:g}",
                                                                    high=f"{preset.target_duration[1]:g}"))
        if problems:
            st.warning(f"{line} · {' · '.join(problems)}", icon=":material/warning:")
        else:
            st.caption(line)
        stale = batch_review.script_sha256(text) != review.get("audit_sha256")
        _render_audit(tr, review, stale)
        with st.expander(tr("batch.review.terms_header")):
            st.text_area(tr("batch.review.terms"), key=keys["terms"], height=170, disabled=busy,
                         help=tr("batch.review.terms_help"))
            st.text_input(tr("batch.review.anchors"), key=keys["anchors"], disabled=busy,
                          help=tr("batch.review.anchors_help"))
            if review.get("terms_edited"):
                st.caption(tr("batch.review.terms_edited"))
        instruction = st.text_input(tr("batch.review.instruction"), key=keys["instruction"], disabled=busy,
                                    placeholder=tr("batch.review.instruction_placeholder"))
        blocked = not stale and batch_review.audit_blocks(review.get("audit"))
        with st.container(horizontal=True, gap="small"):
            approve = st.button(tr("batch.review.approve_anyway" if blocked else "batch.review.approve"),
                                key=f"{keys['base']}_approve", type="primary", disabled=busy,
                                icon=":material/warning:" if blocked else ":material/check:")
            regenerate = st.button(tr("batch.review.regenerate"), key=f"{keys['base']}_regenerate", disabled=busy,
                                   icon=":material/autorenew:")
            reaudit = st.button(tr("batch.review.reaudit"), key=f"{keys['base']}_reaudit", disabled=busy,
                                icon=":material/fact_check:")
            skip = st.button(tr("batch.review.skip"), key=f"{keys['base']}_skip", disabled=busy,
                             icon=":material/skip_next:")
        if busy:
            action = batch_review.action_key(entry.busy.get("action", ""))
            started = batch_runner._seconds_since(entry.busy.get("started", ""), datetime.now())
            st.info(tr("batch.review.busy").format(action=tr(f"batch.review.action.{action}"),
                                                   seconds=int(started)), icon=":material/hourglass_top:")
            st.progress(min(0.95, started / 90), text=tr("batch.review.busy_hint"))
        last = review.get("last_action") or {}
        if not busy and last and not last.get("ok"):
            if last.get("reason") == "needs_force":
                st.warning(tr("batch.review.needs_force").format(audit=last.get("message", "")))
            else:
                action = batch_review.action_key(last.get("action", ""))
                st.error(tr("batch.review.action_failed").format(action=tr(f"batch.review.action.{action}"),
                                                                 error=last.get("message", "")))
    if not (approve or regenerate or reaudit or skip):
        return
    if batch_review.busy(entry.folder):
        _flash("error", tr("batch.review.error.busy"))
        st.rerun()
    edits = _edits(batch_dir, entry)
    if approve:
        try:
            needs_llm = batch_review.needs_llm(review, *edits)
        except batch_review.ReviewError as exc:
            _flash("error", tr(f"batch.review.error.{exc.reason}"))
            st.rerun()
        if needs_llm:
            _start_llm_action(tr, batch_dir, entry, "onayla", edits=edits)
        else:
            outcome = _direct(tr, batch_review.approve, entry.folder, script=edits[0], terms=edits[1],
                              anchors=edits[2], force=blocked)
            if outcome is not None and outcome.status == "approved":
                _flash("success", tr("batch.review.approved").format(index=item.index, subject=item.subject))
    elif regenerate:
        _start_llm_action(tr, batch_dir, entry, "yeniden-uret", instruction=instruction)
    elif reaudit:
        _start_llm_action(tr, batch_dir, entry, "yeniden-denetle", edits=edits)
    elif skip:
        _direct(tr, batch_review.skip, entry.folder)
    st.rerun()


@st.fragment
def _review_item_static(tr, batch_dir: Path, index: int) -> None:
    found = _find_entry(batch_dir, index)
    if found is None:
        return
    view, entry = found
    if entry.status != bp.AWAITING_REVIEW or entry.busy:  # başka bir oturum ya da komut değiştirdi
        st.rerun(scope="app")
    _review_item(tr, view, entry)


@st.fragment(run_every=REVIEW_REFRESH_SECONDS)
def _review_item_live(tr, batch_dir: Path, index: int) -> None:
    """LLM işlemi süren konu: öğeler kilitli, ilerleme her 2 sn'de yenilenir; işlem bitince sayfa yenilenir."""
    found = _find_entry(batch_dir, index)
    if found is None or not found[1].busy:
        st.rerun(scope="app")
        return
    _review_item(tr, *found)


def _review_item_compact(tr, view: batch_review.Overview, entry: batch_review.ItemReview) -> None:
    item = entry.item
    colors = {"approved": "green", "skipped": "gray", "done": "green", "review": "orange", "failed": "red",
              "rendering": "violet", "preparing": "blue", "pending": "gray", "running": "violet"}
    with st.container(border=True, key=f"batch_review_item_{item.index}"):
        columns = st.columns([5, 1.6], vertical_alignment="center")
        label = tr(f"batch.status.{entry.status}")
        if entry.status in LIVE_STATUSES:
            label = tr(f"batch.status.{entry.status}").format(stage=tr("batch.stage.preparing"))
        text = f"**{item.index}. {_md(item.subject)}** :{colors.get(entry.status, 'gray')}-badge[{_md(label)}]"
        review = entry.review or {}
        if entry.status == bp.APPROVED:
            when = (review.get("approved_at") or "").replace("T", " ")[:16]
            text += "  \n" + tr("batch.review.approved_at").format(time=when)
            if review.get("approved_despite_flags"):
                text += " · " + tr("batch.review.approved_despite_flags")
        elif entry.status == bp.FAILED and item.error:
            stage = f" ({item.failed_stage})" if item.failed_stage else ""
            text += f"  \n:small[{_md(item.error)}{_md(stage)}]"
        columns[0].markdown(text)
        if entry.status == bp.SKIPPED and columns[1].button(tr("batch.review.unskip"),
                                                            key=f"rv_{view.batch_dir.name}_{item.index}_unskip"):
            _direct(tr, batch_review.unskip, entry.folder)
            st.rerun()
        if entry.status in (bp.APPROVED, bp.DONE, "review", bp.RENDERING) and review.get("script"):
            with st.expander(tr("batch.review.approved_text")):
                st.markdown(_md(review["script"]))


def _render_review(tr, running: bool) -> None:
    batch_dir = _review_dir()
    if batch_dir is None:
        return
    lock = batch_runner.current_lock()
    this_running = bool(lock) and Path(lock.get("batch_dir") or "").resolve() == batch_dir.resolve()
    try:
        view = batch_review.overview(batch_dir, running=this_running)
    except (OSError, ValueError, TypeError):
        return
    if not view.state.review_before_render:
        return
    st.markdown(f"#### {tr('batch.review.header').format(name=batch_dir.name)}")
    st.caption(tr("batch.review.intro"))
    metrics = (("batch.review.count.awaiting", view.count(bp.AWAITING_REVIEW)),
               ("batch.review.count.approved", view.approved_waiting),
               ("batch.review.count.flagged", view.flagged),
               ("batch.review.count.skipped", view.count(bp.SKIPPED)),
               ("batch.review.count.done", view.count(bp.DONE) + view.count("review")))
    for column, (key, value) in zip(st.columns(len(metrics)), metrics):
        column.metric(tr(key), value)
    unsaved = {entry.item.index for entry in view.items
               if entry.status == bp.AWAITING_REVIEW and has_unsaved_edits(batch_dir, entry)}
    clean = sum(entry.status == bp.AWAITING_REVIEW and batch_review.is_clean(entry.review) and not entry.busy
                and entry.item.index not in unsaved for entry in view.items)
    with st.container(horizontal=True, gap="small"):
        approve_clean = st.button(tr("batch.review.approve_clean").format(count=clean), key="batch_review_approve_clean",
                                  disabled=clean == 0, icon=":material/done_all:")
        render = st.button(tr("batch.review.render").format(count=view.approved_waiting), key="batch_review_render",
                           type="primary", disabled=running or view.approved_waiting == 0, icon=":material/movie:")
    if running:
        st.caption(tr("batch.review.render_disabled_running"))
    if unsaved:
        st.caption(tr("batch.review.unsaved").format(items=", ".join(map(str, sorted(unsaved)))))
    if approve_clean:
        approved = batch_review.approve_clean(batch_dir, view.state, exclude=unsaved)
        _flash("success", tr("batch.review.approved_clean").format(items=", ".join(map(str, approved)) or "-"))
        st.rerun()
    if render:
        try:
            batch_runner.start_render(batch_dir)
        except batch_runner.BatchBusy as exc:
            st.error(tr("batch.error.busy").format(name=Path(exc.lock.get("batch_dir") or "").name))
            return
        except OSError as exc:
            logger.exception(f"render başlatılamadı: {exc}")
            st.error(tr("batch.error.start").format(error=exc))
            return
        st.session_state["batch_running_dir"] = str(batch_dir)
        _flash("success", tr("batch.review.render_started").format(name=batch_dir.name, count=view.approved_waiting))
        st.rerun()
    for entry in view.items:
        if entry.status == bp.AWAITING_REVIEW and entry.review is not None:
            (_review_item_live if entry.busy else _review_item_static)(tr, batch_dir, entry.item.index)
        else:
            _review_item_compact(tr, view, entry)


def _history_counts(status: batch_runner.BatchStatus, tr) -> str:
    counts = tr("batch.history.counts").format(
        total=len(status.items), done=status.count("done") + status.count("review"),
        failed=status.count("failed"), review=status.count("review"),
    )
    if status.review:
        counts += " · " + tr("batch.history.counts_review").format(
            awaiting=status.count("awaiting_review"), approved=status.count("approved"),
            skipped=status.count("skipped"))
    return counts


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
        state = "running" if status.running else ("finished" if status.finished else (
            "awaiting" if status.awaiting_review else "unfinished"))
        with st.container(border=True, key=f"batch_history_{folder.name}"):
            columns = st.columns([5, 1.4, 1.4, 1.4], vertical_alignment="center")
            created = status.created.replace("T", " ")[:16]
            label = tr(f"batch.history.state.{state}")
            if state == "awaiting":
                label = f":orange-badge[{label}]"
            columns[0].markdown(f"**{created}** · `{folder.name}` · {label}  \n"
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
            if status.review and (status.count("awaiting_review") or status.count("approved")) and columns[3].button(
                    tr("batch.history.review"), key=f"batch_review_open_{folder.name}", type="primary"):
                st.session_state["batch_review_dir"] = str(folder)
                st.rerun(scope="app")
            files = subtitle_listing(status)
            if files:
                with st.expander(tr("batch.history.files")):
                    st.markdown("\n".join(f"- {line}" for line in files))


def subtitle_listing(status: batch_runner.BatchStatus) -> list[str]:
    """Geçmiş kartı için video başına .srt dosyaları ("1. Konu: altyazi.tr.srt, altyazi.en.srt")."""
    lines = []
    for item in status.items:
        names = batch_package.subtitle_files(status.directory / item.folder) if item.folder else []
        if names:
            lines.append(f"{item.index}. {item.subject}: " + ", ".join(f"`{name}`" for name in names))
    return lines


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
    _render_review(tr, running)
    _render_editor(tr, running)
    _render_history(tr, running)
