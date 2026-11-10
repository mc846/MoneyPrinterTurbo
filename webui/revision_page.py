"""WebUI revision panel: a finished task's text, voice, subtitles and music changed over its saved materials.

Opened with "Geliştir" in the Task Manager or in the batch history (``?view=revision&task=<id>``,
for a batch task also ``&batch=<folder>&item=<index>``). The settings here are the revision's:
widgets have their own keys and nothing is written to config.toml (Main.py returns before its
settings are saved, as for the batch view). The revision is made by ``cli.py revizyon`` in a
background process (``task_revisions.start_background``); this page follows its progress file.
"""

import os
from pathlib import Path

import streamlit as st

from app.services import batch_review, task_revisions, voice
from app.services import bgm as bgm_service
from app.utils import utils

VIEW_PARAM = "view"
VIEW_REVISION = "revision"
TASK_PARAM = "task"
BATCH_PARAM = "batch"
ITEM_PARAM = "item"
REFRESH_SECONDS = 2
POSITIONS = ("top", "center", "bottom", "custom")
MUSIC_TYPES = ("", "random", "custom")
FONT_SUFFIXES = (".ttf", ".ttc", ".otf")


def open_revision(task_id: str, batch_dir: str | Path | None = None, index: int | None = None) -> None:
    st.query_params[VIEW_PARAM] = VIEW_REVISION
    st.query_params[TASK_PARAM] = str(task_id)
    if batch_dir is not None and index is not None:
        st.query_params[BATCH_PARAM] = str(batch_dir)
        st.query_params[ITEM_PARAM] = str(index)
    else:
        st.query_params.pop(BATCH_PARAM, None)
        st.query_params.pop(ITEM_PARAM, None)
    st.rerun(scope="app")


def revision_view_active() -> bool:
    return st.query_params.get(VIEW_PARAM) == VIEW_REVISION and bool(st.query_params.get(TASK_PARAM))


def _close() -> None:
    for name in (VIEW_PARAM, TASK_PARAM, BATCH_PARAM, ITEM_PARAM):
        st.query_params.pop(name, None)


def _fonts() -> list[str]:
    return sorted(name for name in os.listdir(utils.font_dir()) if name.lower().endswith(FONT_SUFFIXES))


def _music_files() -> list[str]:
    return sorted(os.path.basename(path) for path in bgm_service.list_bgm_files())


def _voices(params: dict) -> list[str]:
    language = str(params.get("video_language") or "").strip()
    voices = voice.get_all_azure_voices(filter_locals=[language] if language else None)
    current = params.get("voice_name")
    return voices if not current or current in voices else [current, *voices]


def _with(options: list, value) -> list:
    return options if value in options else [value, *options]


def _choice(container, label: str, values: list, current, describe, key: str):
    """A select box over translated labels that returns the raw value (no format_func)."""
    labels = [describe(value) for value in values]
    chosen = container.selectbox(label, labels, index=values.index(current), key=key)
    return values[labels.index(chosen)]


def _form(tr, task_id: str, base: dict) -> tuple[str, dict]:
    """The editable values, starting from the base revision's; returns (text, changed settings)."""
    params = base.get("params") or {}
    key = f"revision_{task_id}_{base['revision']}"
    script = st.text_area(tr("revision.script"), value=task_revisions.script_text(task_id, base),
                          height=220, key=f"{key}_script")
    columns = st.columns(2)
    voices = _voices(params)
    voice_name = columns[0].selectbox(tr("revision.voice"), voices,
                                      index=voices.index(params.get("voice_name")) if params.get("voice_name") in voices else 0,
                                      key=f"{key}_voice")
    voice_rate = columns[1].number_input(tr("revision.voice_rate"), min_value=0.5, max_value=2.0, step=0.05,
                                         value=float(params.get("voice_rate") or 1.0), key=f"{key}_rate")
    estimate = batch_review.estimate(script, voice_rate)
    st.caption(tr("revision.words").format(words=estimate["kelime"], seconds=estimate["tahmini_sn"]))

    with st.expander(tr("revision.subtitle_section"), expanded=True):
        enabled = st.checkbox(tr("revision.subtitle_enabled"), value=bool(params.get("subtitle_enabled", True)),
                              key=f"{key}_subtitle")
        columns = st.columns(3)
        fonts = _with(_fonts(), params.get("font_name"))
        font_name = columns[0].selectbox(tr("revision.font"), fonts, index=fonts.index(params.get("font_name")),
                                         key=f"{key}_font")
        font_size = columns[1].number_input(tr("revision.font_size"), min_value=10, max_value=200, step=1,
                                            value=int(params.get("font_size") or 60), key=f"{key}_font_size")
        stroke_width = columns[2].number_input(tr("revision.stroke_width"), min_value=0.0, max_value=10.0,
                                               step=0.5, value=float(params.get("stroke_width") or 0.0),
                                               key=f"{key}_stroke_width")
        columns = st.columns(3)
        text_color = columns[0].color_picker(tr("revision.text_color"), value=params.get("text_fore_color") or "#FFFFFF",
                                             key=f"{key}_text_color")
        stroke_color = columns[1].color_picker(tr("revision.stroke_color"), value=params.get("stroke_color") or "#000000",
                                               key=f"{key}_stroke_color")
        positions = _with(list(POSITIONS), params.get("subtitle_position") or "bottom")
        position = _choice(columns[2], tr("revision.position"), positions,
                           params.get("subtitle_position") or "bottom",
                           lambda value: tr(f"revision.position.{value}"), f"{key}_position")
        custom_position = float(params.get("custom_position") or 70.0)
        if position == "custom":
            custom_position = st.slider(tr("revision.custom_position"), 0.0, 100.0, custom_position,
                                        key=f"{key}_custom_position")

    with st.expander(tr("revision.music_section"), expanded=False):
        columns = st.columns(3)
        music_types = _with(list(MUSIC_TYPES), params.get("bgm_type") or "")
        bgm_type = _choice(columns[0], tr("revision.music_type"), music_types, params.get("bgm_type") or "",
                           lambda value: tr(f"revision.music.{value or 'none'}"), f"{key}_bgm_type")
        bgm_file = params.get("bgm_file") or ""
        if bgm_type == "custom":
            files = _with(_music_files(), bgm_file) if bgm_file else _music_files()
            bgm_file = columns[1].selectbox(tr("revision.music_file"), files or [""],
                                            index=files.index(bgm_file) if bgm_file in files else 0,
                                            key=f"{key}_bgm_file")
        bgm_volume = columns[2].slider(tr("revision.music_volume"), 0.0, 1.0, float(params.get("bgm_volume") or 0.2),
                                       step=0.05, key=f"{key}_bgm_volume")
    credit = st.checkbox(tr("revision.nasa_credit"), value=bool(params.get("nasa_credit_overlay")),
                         key=f"{key}_credit")

    values = {
        "voice_name": voice_name, "voice_rate": float(voice_rate), "subtitle_enabled": enabled,
        "font_name": font_name, "font_size": int(font_size), "text_fore_color": text_color,
        "stroke_color": stroke_color, "stroke_width": float(stroke_width), "subtitle_position": position,
        "custom_position": float(custom_position), "bgm_type": bgm_type, "bgm_file": bgm_file,
        "bgm_volume": float(bgm_volume), "nasa_credit_overlay": credit,
    }
    changed = {name: value for name, value in values.items() if params.get(name) != value}
    if "custom_position" in changed and position != "custom":
        changed.pop("custom_position")
    if "bgm_file" in changed and bgm_type != "custom":
        changed.pop("bgm_file")
    return script, changed


def _render_scenes(tr, task_id: str, base: dict) -> None:
    rows = task_revisions.scene_rows(task_id, base)
    st.markdown(f"**{tr('revision.scenes')}** ({len(rows)})")
    per_row = 6
    for start in range(0, len(rows), per_row):
        columns = st.columns(per_row)
        for column, row in zip(columns, rows[start:start + per_row]):
            with column:
                if row["preview"]:
                    st.image(row["preview"], width="stretch")
                st.caption(tr("revision.scene_caption").format(
                    id=row["id"], term=row["term"], seconds=f"{float(row['duration'] or 0):.1f}"))


def _summary(tr, record: dict) -> str:
    changes = record.get("changes") or {}
    parts = []
    for name, change in changes.items():
        if name == "script":
            parts.append(tr("revision.change.script").format(before=change.get("from_words"), after=change.get("to_words")))
        else:
            parts.append(f"{name}: {change.get('from')} → {change.get('to')}")
    return " · ".join(parts) or tr("revision.change.none")


def _render_history(tr, task_id: str, records: list[dict], busy: bool) -> None:
    st.markdown(f"#### {tr('revision.history')}")
    batch_dir, item = st.query_params.get(BATCH_PARAM), st.query_params.get(ITEM_PARAM)
    for record in reversed(records):
        name = record["revision"]
        with st.container(border=True, key=f"revision_history_{name}"):
            created = str(record.get("created_at") or "").replace("T", " ")[:16]
            title = f"**{name}** · {created}"
            if name == task_revisions.FIRST:
                title += f" · {tr('revision.original')}"
            elif record.get("parent"):
                title += f" · {tr('revision.parent').format(parent=record['parent'])}"
            if record.get("check_required"):
                title += f" · :orange-badge[{tr('revision.check_required')}]"
            elif record.get("audit"):
                title += f" · :green-badge[{tr('revision.audit_ok')}]"
            st.markdown(title)
            st.caption(_summary(tr, record))
            video = task_revisions.file_path(task_id, record, "video")
            details = st.columns([2, 3])
            if video is not None and video.is_file():
                details[0].video(str(video))
            with details[1]:
                st.caption(tr("revision.timeline").format(
                    scenes=len(record.get("scenes") or []),
                    seconds=f"{float((record.get('audio') or {}).get('duration') or 0):.1f}"))
                if record.get("dropped"):
                    st.caption(tr("revision.dropped").format(scenes=", ".join(d["id"] for d in record["dropped"])))
                if record.get("added"):
                    st.caption(tr("revision.added").format(scenes=", ".join(a["id"] for a in record["added"])))
                for warning in record.get("warnings") or []:
                    st.caption(f":orange[{warning}]")
                for reason in record.get("check_reasons") or []:
                    st.caption(f":orange[{reason}]")
                if batch_dir and item and name != task_revisions.FIRST and st.button(
                        tr("revision.package_refresh"), key=f"revision_package_{name}", disabled=busy):
                    try:
                        task_revisions.start_package_refresh(batch_dir, int(item), task_id, name)
                    except batch_review.ReviewBusy:
                        st.error(tr("revision.busy"))
                    else:
                        st.session_state["revision_flash"] = ("info", tr("revision.package_started"))
                        st.rerun(scope="app")


@st.fragment(run_every=REFRESH_SECONDS)
def _render_progress(tr, task_id: str) -> None:
    progress = task_revisions.read_progress(task_id) or {}
    if task_revisions.busy(task_id):
        st.info(tr("revision.progress").format(revision=progress.get("revision") or "…",
                                               stage=progress.get("stage") or "…"))
        return
    st.session_state.pop(f"revision_running_{task_id}", None)
    st.rerun(scope="app")


def render_revision_page(tr) -> None:
    task_id = st.query_params.get(TASK_PARAM, "")
    if st.button(tr("revision.back"), key="revision_back", icon=":material/arrow_back:"):
        _close()
        st.rerun(scope="app")
    try:
        records = task_revisions.history(task_id)
    except Exception as exc:  # a task without materials.json, a missing video...
        st.error(tr("revision.error.unavailable").format(error=exc))
        return
    first = records[0]
    subject = (first.get("params") or {}).get("video_subject") or task_id
    st.subheader(tr("revision.title").format(subject=subject))
    flash = st.session_state.pop("revision_flash", None)
    if flash:
        getattr(st, flash[0])(flash[1])
    running = bool(task_revisions.busy(task_id))
    if running:
        _render_progress(tr, task_id)
    else:
        progress = task_revisions.read_progress(task_id) or {}
        if progress.get("stage") == "failed":
            st.error(tr("revision.failed").format(error=progress.get("error") or ""))

    names = [record["revision"] for record in records]
    base_name = st.selectbox(tr("revision.base"), names, index=len(names) - 1, key=f"revision_base_{task_id}")
    base = records[names.index(base_name)]
    script, changed = _form(tr, task_id, base)
    script_changed = batch_review.normalize_script(script) != task_revisions.script_text(task_id, base)
    nothing = not changed and not script_changed
    if st.button(tr("revision.generate"), key="revision_generate", type="primary", disabled=running or nothing,
                 icon=":material/movie:"):
        arguments = [f"--temel-revizyon={base_name}", *task_revisions.settings_arguments(changed)]
        if script_changed:
            arguments.append(f"--metin-dosyasi={task_revisions.write_text_handover(task_id, script)}")
        try:
            task_revisions.start_background(task_id, arguments)
        except batch_review.ReviewBusy:
            st.error(tr("revision.busy"))
        else:
            st.session_state["revision_flash"] = ("info", tr("revision.started"))
            st.rerun(scope="app")
    if nothing:
        st.caption(tr("revision.nothing_changed"))
    _render_scenes(tr, task_id, base)
    _render_history(tr, task_id, records, running)
