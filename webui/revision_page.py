"""WebUI revision panel: a finished task's text, voice, subtitles and music changed over its saved materials.

Opened with "Geliştir" in the Task Manager or in the batch history (``?view=revision&task=<id>``,
for a batch task also ``&batch=<folder>&item=<index>``). The settings here are the revision's:
widgets have their own keys and nothing is written to config.toml (Main.py returns before its
settings are saved, as for the batch view). The revision is made by ``cli.py revizyon`` in a
background process (``task_revisions.start_background``); this page follows its progress file.
"""

import os
from datetime import datetime
from pathlib import Path

import streamlit as st
from PIL import Image, ImageDraw

from app.models.schema import VideoAspect
from app.services import batch_review, material, material_upload, scene_edits, task_revisions, voice
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


def _ops_key(task_id: str, base_name: str) -> str:
    return f"revision_ops_{task_id}_{base_name}"


def _ops(task_id: str, base_name: str) -> list[scene_edits.SceneOp]:
    return [scene_edits.SceneOp.from_dict(data) for data in st.session_state.get(_ops_key(task_id, base_name), [])]


def _set_ops(task_id: str, base_name: str, ops: list[scene_edits.SceneOp]) -> None:
    st.session_state[_ops_key(task_id, base_name)] = [op.to_dict() for op in ops]


def _add_op(task_id: str, base_name: str, op: scene_edits.SceneOp) -> None:
    """A newer operation of the same kind on the same scene replaces the older one."""
    ops = [old for old in _ops(task_id, base_name) if not (old.scene == op.scene and old.op == op.op)]
    if op.op == scene_edits.REMOVE:
        ops = [old for old in ops if old.scene != op.scene]
    _set_ops(task_id, base_name, [*ops, op])


@st.cache_data(show_spinner=False, max_entries=64)
def _thumbnail(path: str, mtime: float, max_side: int) -> Image.Image:
    with Image.open(path) as image:
        image.draft("RGB", (max_side * 2, max_side * 2))
        image = image.convert("RGB")
        image.thumbnail((max_side, max_side))
        return image


def framed(path: str, aspect: VideoAspect, position=scene_edits.CENTER, max_side: int = 480) -> Image.Image:
    """The image with the video's crop window drawn on it (the rest dimmed) at ``position``."""
    image = _thumbnail(path, os.path.getmtime(path), max_side).copy()
    width, height = image.size
    crop_width, crop_height = material._center_crop_size(width, height, aspect)
    left = round((width - crop_width) * position[0])
    top = round((height - crop_height) * position[1])
    shade = Image.new("RGBA", image.size, (0, 0, 0, 140))
    ImageDraw.Draw(shade).rectangle((left, top, left + crop_width - 1, top + crop_height - 1), fill=(0, 0, 0, 0))
    image = Image.alpha_composite(image.convert("RGBA"), shade)
    ImageDraw.Draw(image).rectangle((left, top, left + crop_width - 1, top + crop_height - 1),
                                    outline=(255, 59, 48, 255), width=3)
    return image.convert("RGB")


def crop_axes(path: str, aspect: VideoAspect) -> tuple[bool, bool]:
    """Which crop positions matter: (x, y); an image already in the video's shape has neither."""
    with Image.open(path) as image:
        width, height = image.size
    crop_width, crop_height = material._center_crop_size(width, height, aspect)
    return crop_width < width, crop_height < height


def _describe(tr, op: scene_edits.SceneOp) -> str:
    """A pending scene operation in the UI language (SceneOp.describe is the English one)."""
    if op.op == scene_edits.REMOVE:
        return tr("revision.op.remove").format(scene=op.scene)
    if op.op == scene_edits.CROP:
        return tr("revision.op.crop").format(scene=op.scene, x=f"{op.position[0]:.2f}", y=f"{op.position[1]:.2f}")
    if op.candidate:
        return tr("revision.op.replace_candidate").format(scene=op.scene, candidate=op.candidate)
    if op.search:
        return tr("revision.op.replace_search").format(scene=op.scene, words=op.search)
    return tr("revision.op.replace_file").format(scene=op.scene, name=os.path.basename(op.path))


FILTER_NAMES = ("text", "face")
# Stock source names in the new-search source list (labels, not format_func: AppTest).
SOURCE_LABELS = {"nasa": "NASA", "nasa_svs": "NASA SVS", "pixabay": "Pixabay", "pexels": "Pexels",
                 "coverr": "Coverr"}


def _reason(tr, choice: dict) -> str:
    """Why a candidate cannot be chosen, in the UI language (scene_edits keeps the English reason)."""
    code = choice.get("reason_code")
    if code == "filtered":
        names = [tr(f"revision.filter.{name}") if name in FILTER_NAMES else name
                 for name in choice.get("filters_rejected") or []]
        return tr("revision.reason.filtered").format(filters=", ".join(names) or "—")
    if code == "other_scene":
        return tr("revision.reason.other_scene").format(scene=choice.get("used_by") or "")
    if code in ("failed", "current", "no_address"):
        return tr(f"revision.reason.{code}")
    return str(choice.get("reason") or "")


def _scene_image(task_id: str, materials: dict, row: dict):
    """The scene's source image for cropping (NASA original or the user's image)."""
    return scene_edits._image_for_crop(task_id, materials.get(row["material"] or "", {}))


# The open scene dialog ({"task", "base", "kind", "scene"}): kept in the session so the
# dialog stays open over full reruns; closing it (X, Esc, outside) clears it.
DIALOG_KEY = "revision_dialog"


def _close_dialog() -> None:
    st.session_state.pop(DIALOG_KEY, None)


@st.dialog(" ", width="large", on_dismiss=_close_dialog)
def _replace_dialog(tr, task_id: str, base: dict, scene_id: str) -> None:
    st.markdown(f"**{tr('revision.replace.title').format(scene=scene_id)}**")
    manifest, materials = task_revisions._materials(task_id)
    if (base.get("params") or {}).get("video_source") == "ai_manual":
        # Fiction: no search; the scene's Veo prompt (edit, copy to Flow) and the new clip.
        from webui import fiction_panel

        fiction_panel.revision_replace(tr, task_id, base, scene_id, materials, _add_op, _close_dialog)
        return
    scene = next(s for s in base["scenes"] if s["id"] == scene_id)
    aspect = VideoAspect((base.get("params") or {}).get("video_aspect") or VideoAspect.portrait)
    key = f"revision_replace_{task_id}_{scene_id}"
    where = scene_edits.search_sources(task_id)
    # The task's chain first (the default), then each stock source with a key on its own.
    source_values = ([scene_edits.TASK_CHAIN] if where["chain"] else []) + where["sources"]
    source_labels = [
        tr("revision.replace.source_chain").format(chain=" → ".join(SOURCE_LABELS.get(s, s) for s in where["chain"]))
        if value == scene_edits.TASK_CHAIN else SOURCE_LABELS.get(value, value)
        for value in source_values
    ]
    with st.form(f"{key}_search_form", border=False):
        columns = st.columns([3, 2, 1], vertical_alignment="bottom")
        words = columns[0].text_input(tr("revision.replace.search"), key=f"{key}_words")
        chosen_label = columns[1].selectbox(tr("revision.replace.source"), source_labels, index=0,
                                            key=f"{key}_source") if source_labels else None
        if columns[2].form_submit_button(tr("revision.replace.search_button"), key=f"{key}_search") \
                and words.strip():
            source = source_values[source_labels.index(chosen_label)] if chosen_label else scene_edits.TASK_CHAIN
            try:
                with st.spinner(tr("revision.replace.searching")):
                    found = scene_edits.search(task_id, scene, words, source)
                st.session_state[f"{key}_flash"] = tr("revision.replace.search_done_source").format(
                    count=len(found["candidates"]), term=found["term"],
                    source=SOURCE_LABELS.get(found.get("provider") or "", found.get("provider") or "—"))
            except Exception as exc:
                st.session_state[f"{key}_flash"] = tr("revision.replace.search_failed").format(error=exc)
            manifest, materials = task_revisions._materials(task_id)
    flash = st.session_state.pop(f"{key}_flash", None)
    if flash:
        st.info(flash)
    choices = scene_edits.candidate_choices(manifest, materials, scene, base["scenes"])
    folder = task_revisions.task_materials.folder(task_id)
    st.markdown(f"**{tr('revision.replace.saved')}** ({len(choices)})")
    if not choices:
        st.caption(tr("revision.replace.none"))
    per_row = 4
    for start in range(0, len(choices), per_row):
        columns = st.columns(per_row)
        for column, choice in zip(columns, choices[start:start + per_row]):
            with column.container(border=True, key=f"{key}_card_{choice['key']}".replace(":", "_")):
                preview = folder / choice["preview"] if choice.get("preview") else None
                if preview is not None and preview.is_file():
                    st.image(str(preview), width="stretch")
                score = (choice.get("relevance") or {}).get("score")
                st.caption(f"{'🔍 ' if choice['search'] else ''}{choice['key']}"
                           + (f" · {tr('revision.scene.score').format(score=f'{score:.2f}')}" if score is not None else ""))
                st.caption(tr(f"revision.candidate.status.{choice.get('status') or 'not_selected'}"))
                if not choice["selectable"]:
                    st.caption(f":orange[{tr('revision.replace.not_selectable').format(reason=_reason(tr, choice))}]")
                if st.button(tr("revision.replace.choose"), key=f"{key}_choose_{choice['key']}",
                             disabled=not choice["selectable"], width="stretch"):
                    st.session_state[f"{key}_selected"] = {"candidate": choice["key"],
                                                           "preview": str(preview) if preview else ""}
    upload = st.file_uploader(tr("revision.replace.upload"), key=f"{key}_upload",
                              type=[suffix.lstrip(".") for suffix in scene_edits.IMAGE_SUFFIXES + scene_edits.VIDEO_SUFFIXES])
    if upload is not None and st.session_state.get(f"{key}_uploaded") != upload.file_id:
        # save_material_upload returns the stored file's name in the uploads folder, not a path: the
        # revision process and the preview need the full path (a bare name was not found; E2E 14.11).
        saved = os.path.join(material_upload.uploaded_material_dir(),
                             material_upload.save_material_upload(upload.name, upload))
        st.session_state[f"{key}_uploaded"] = upload.file_id
        st.session_state[f"{key}_selected"] = {"path": saved, "preview": saved}
    selected = st.session_state.get(f"{key}_selected")
    if selected:
        st.markdown(tr("revision.replace.selected").format(
            name=selected.get("candidate") or os.path.basename(selected.get("path") or "")))
        preview = selected.get("preview") or ""
        if preview and os.path.splitext(preview)[1].lower() in scene_edits.IMAGE_SUFFIXES:
            st.image(framed(preview, aspect), caption=tr("revision.replace.frame"))
        if st.button(tr("revision.replace.add"), key=f"{key}_add", type="primary"):
            _add_op(task_id, base["revision"], scene_edits.SceneOp(
                scene_edits.REPLACE, scene_id, candidate=selected.get("candidate", ""), path=selected.get("path", "")))
            st.session_state.pop(f"{key}_selected", None)
            _close_dialog()
            st.rerun()


@st.dialog(" ", width="large", on_dismiss=_close_dialog)
def _crop_dialog(tr, task_id: str, base: dict, scene_id: str, image: str) -> None:
    st.markdown(f"**{tr('revision.crop.title').format(scene=scene_id)}**")
    aspect = VideoAspect((base.get("params") or {}).get("video_aspect") or VideoAspect.portrait)
    scene = next(s for s in base["scenes"] if s["id"] == scene_id)
    pending = next((op for op in _ops(task_id, base["revision"]) if op.scene == scene_id and op.op == scene_edits.CROP),
                   None)
    current = pending.position if pending else tuple(scene.get("crop") or scene_edits.CENTER)
    horizontal, vertical = crop_axes(image, aspect)
    key = f"revision_crop_{task_id}_{scene_id}"
    x = st.slider(tr("revision.crop.x"), 0.0, 1.0, float(current[0]), 0.01, key=f"{key}_x",
                  disabled=not horizontal)
    y = st.slider(tr("revision.crop.y"), 0.0, 1.0, float(current[1]), 0.01, key=f"{key}_y",
                  disabled=not vertical)
    if not (horizontal or vertical):
        st.caption(tr("revision.crop.none"))
    st.image(framed(image, aspect, (x, y), max_side=560), caption=tr("revision.replace.frame"))
    if st.button(tr("revision.crop.apply"), key=f"{key}_apply", type="primary"):
        _add_op(task_id, base["revision"], scene_edits.SceneOp(scene_edits.CROP, scene_id, position=(x, y)))
        _close_dialog()
        st.rerun()


def _render_scenes(tr, task_id: str, base: dict, busy: bool) -> None:
    rows = task_revisions.scene_rows(task_id, base)
    _manifest, materials = task_revisions._materials(task_id)
    ops = _ops(task_id, base["revision"])
    removed = {op.scene for op in ops if op.op == scene_edits.REMOVE}
    changed = {op.scene: op for op in ops if op.op != scene_edits.REMOVE}
    images = {}
    st.markdown(f"**{tr('revision.scenes')}** ({len(rows)})")
    per_row = 4
    for start in range(0, len(rows), per_row):
        columns = st.columns(per_row)
        for column, row in zip(columns, rows[start:start + per_row]):
            with column.container(border=True, key=f"revision_scene_{row['id']}"):
                if row["preview"]:
                    st.image(row["preview"], width="stretch")
                st.markdown(f"**{row['id']}** · {row['term']}")
                credit = row.get("credit") or "—"
                st.caption(f"{row.get('provider') or ''} · {credit}")
                details = tr("revision.scene_caption").format(id=row["kind"], term=row.get("asset_id") or "",
                                                              seconds=f"{float(row['duration'] or 0):.1f}")
                if row.get("score") is not None:
                    details += " · " + tr("revision.scene.score").format(score=f"{row['score']:.2f}")
                st.caption(details)
                if row["id"] in removed:
                    st.caption(f":red-badge[{tr('revision.scene.removed')}]")
                elif row["id"] in changed:
                    st.caption(f":orange-badge[{_describe(tr, changed[row['id']])}]")
                buttons = st.columns(3)
                opened = {"task": task_id, "base": base["revision"], "scene": row["id"]}
                if buttons[0].button(tr("revision.scene.replace"), key=f"revision_replace_btn_{row['id']}",
                                     disabled=busy, width="stretch"):
                    st.session_state[DIALOG_KEY] = {**opened, "kind": "replace"}
                last = len(rows) - len(removed) <= 1 and row["id"] not in removed
                if buttons[1].button(tr("revision.scene.remove"), key=f"revision_remove_btn_{row['id']}",
                                     disabled=busy or last or row["id"] in removed, width="stretch",
                                     help=tr("revision.last_scene") if last else None):
                    _add_op(task_id, base["revision"], scene_edits.SceneOp(scene_edits.REMOVE, row["id"]))
                    st.rerun()
                image = _scene_image(task_id, materials, row) if row.get("kind") == "image" else None
                if image is not None:
                    images[row["id"]] = str(image)
                    if buttons[2].button(tr("revision.scene.crop"), key=f"revision_crop_btn_{row['id']}",
                                         disabled=busy, width="stretch"):
                        st.session_state[DIALOG_KEY] = {**opened, "kind": "crop"}
    dialog = st.session_state.get(DIALOG_KEY) or {}
    if busy or (dialog.get("task"), dialog.get("base")) != (task_id, base["revision"]) \
            or dialog.get("scene") not in {row["id"] for row in rows}:
        return
    if dialog.get("kind") == "replace":
        _replace_dialog(tr, task_id, base, dialog["scene"])
    elif dialog.get("kind") == "crop" and dialog["scene"] in images:
        _crop_dialog(tr, task_id, base, dialog["scene"], images[dialog["scene"]])


def _render_pending(tr, task_id: str, base_name: str) -> None:
    ops = _ops(task_id, base_name)
    if not ops:
        return
    st.markdown(f"**{tr('revision.pending')}**")
    for index, op in enumerate(ops):
        columns = st.columns([6, 1], vertical_alignment="center")
        columns[0].markdown(f"- {_describe(tr, op)}")
        if columns[1].button(tr("revision.pending.undo"), key=f"revision_undo_{index}_{op.scene}_{op.op}"):
            _set_ops(task_id, base_name, [other for other in ops if other != op])
            st.rerun()


PERFORMANCE_FIELDS = ("final_render_engine", "material_concurrency", "video_clip_concurrency", "video_codec",
                      "n_threads")
CODECS = ("", "libx264", "h264_nvenc", "h264_amf", "h264_qsv", "h264_mf")


def _render_performance(tr, task_id: str) -> dict:
    """Speed settings (Advanced): only values changed here are passed as explicit ones."""
    resolved = task_revisions.performance(task_id)
    explicit = {}
    with st.expander(tr("revision.advanced"), expanded=False):
        st.caption(tr("revision.performance.help"))
        key = f"revision_performance_{task_id}"
        columns = st.columns(5)
        engine = resolved["final_render_engine"]["value"] or "ffmpeg"
        values = {
            "final_render_engine": columns[0].selectbox(tr("revision.performance.engine"), ["ffmpeg", "moviepy"],
                                                        index=["ffmpeg", "moviepy"].index(engine)
                                                        if engine in ("ffmpeg", "moviepy") else 0,
                                                        key=f"{key}_engine"),
            "material_concurrency": columns[1].number_input(
                tr("revision.performance.material"), 1, 8, int(resolved["material_concurrency"]["value"] or 3),
                key=f"{key}_material"),
            "video_clip_concurrency": columns[2].number_input(
                tr("revision.performance.clips"), 1, 8, int(resolved["video_clip_concurrency"]["value"] or 3),
                key=f"{key}_clips"),
            "video_codec": columns[3].selectbox(tr("revision.performance.codec"), list(CODECS),
                                                index=list(CODECS).index(resolved["video_codec"]["value"] or "")
                                                if (resolved["video_codec"]["value"] or "") in CODECS else 0,
                                                key=f"{key}_codec") or None,
            "n_threads": columns[4].number_input(tr("revision.performance.threads"), 0, 64,
                                                 int(resolved["n_threads"]["value"] or 0), key=f"{key}_threads") or None,
        }
        for name in PERFORMANCE_FIELDS:
            source = resolved[name]["source"]
            if values[name] != resolved[name]["value"]:
                explicit[name] = values[name]
                source = "explicit"
            st.caption(f"{name}: {resolved[name]['value']} → {values[name]} · "
                       + tr(f"revision.performance.source.{source}"))
    return explicit


def _local_time(value) -> str:
    """revision.json keeps UTC; the page shows the computer's local time."""
    try:
        return datetime.fromisoformat(str(value)).astimezone().strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return str(value or "").replace("T", " ")[:16]


def _summary(tr, record: dict) -> str:
    changes = record.get("changes") or {}
    parts = []
    for name, change in changes.items():
        if name == "script":
            parts.append(tr("revision.change.script").format(before=change.get("from_words"), after=change.get("to_words")))
        elif name == "scenes":
            # revision.json keeps the English descriptions; the page names each scene and what was done.
            done = [f"{c['scene']} {tr('revision.op.name.' + c['op'])}" for c in record.get("scene_changes") or []]
            parts.append(tr("revision.change.scenes").format(changes=", ".join(done) or "; ".join(change)))
        else:
            parts.append(f"{name}: {change.get('from')} → {change.get('to')}")
    return " · ".join(parts) or tr("revision.change.none")


def _render_history(tr, task_id: str, records: list[dict], busy: bool) -> None:
    st.markdown(f"#### {tr('revision.history')}")
    batch_dir, item = st.query_params.get(BATCH_PARAM), st.query_params.get(ITEM_PARAM)
    for record in reversed(records):
        name = record["revision"]
        with st.container(border=True, key=f"revision_history_{name}"):
            created = _local_time(record.get("created_at"))
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
            # A portrait video in a narrow column; a wide one would fill the screen.
            details = st.columns([1, 3])
            if video is not None and video.is_file():
                details[0].video(str(video))
            with details[1]:
                st.caption(tr("revision.timeline").format(
                    scenes=len(record.get("scenes") or []),
                    seconds=f"{float((record.get('audio') or {}).get('duration') or 0):.1f}"))
                if record.get("dropped"):
                    st.caption(tr("revision.dropped").format(scenes=", ".join(d["id"] for d in record["dropped"])))
                for change in record.get("scene_changes") or []:
                    before = (change.get("from") or {}).get("asset_id") if isinstance(change.get("from"), dict) \
                        else change.get("from")
                    after = (change.get("to") or {}).get("asset_id") if isinstance(change.get("to"), dict) \
                        else change.get("to")
                    st.caption(tr("revision.scene_change").format(
                        scene=change["scene"], op=tr(f"revision.op.name.{change['op']}"), before=before,
                        after=after or "—"))
                clips = record.get("clips") or {}
                if clips:
                    st.caption(tr("revision.clips").format(rendered=len(clips.get("rendered") or []),
                                                           reused=len(clips.get("reused") or [])))
                if record.get("returned"):
                    st.caption(tr("revision.returned").format(scenes=", ".join(record["returned"])))
                if record.get("added"):
                    st.caption(tr("revision.added").format(scenes=", ".join(a["id"] for a in record["added"])))
                for warning in record.get("warnings") or []:
                    # revision.json keeps the module's English text; the known warning is translated.
                    if warning == task_revisions.timing.term_matching_warning(True, True):
                        warning = tr("revision.warning.term_matching")
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
    script_changed = batch_review.normalize_script(script) != batch_review.normalize_script(
        task_revisions.script_text(task_id, base))
    _render_scenes(tr, task_id, base, running)
    ops = _ops(task_id, base_name)
    _render_pending(tr, task_id, base_name)
    explicit = _render_performance(tr, task_id)
    nothing = not changed and not script_changed and not ops
    if st.button(tr("revision.generate"), key="revision_generate", type="primary", disabled=running or nothing,
                 icon=":material/movie:"):
        arguments = [f"--temel-revizyon={base_name}", *task_revisions.settings_arguments(changed),
                     *scene_edits.to_arguments(ops), *task_revisions.performance_arguments(explicit)]
        if script_changed:
            arguments.append(f"--metin-dosyasi={task_revisions.write_text_handover(task_id, script)}")
        try:
            task_revisions.start_background(task_id, arguments)
        except batch_review.ReviewBusy:
            st.error(tr("revision.busy"))
        else:
            _set_ops(task_id, base_name, [])
            st.session_state["revision_flash"] = ("info", tr("revision.started"))
            st.rerun(scope="app")
    if nothing:
        st.caption(tr("revision.nothing_changed"))
    _render_history(tr, task_id, records, running)
