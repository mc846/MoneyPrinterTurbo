"""
Kurgu kanalının WebUI parçaları (app/services/fiction.py): inceleme panelinde sahne planı ve Veo istemleri
(stil kitabı ve istemler düzenlenir), onaydan sonra sahne kartları (istemi kopyala, klibi yükle, durum), gelen/
klasörünü kontrol etme ve numarasız dosyaları sahneye bağlama; revizyonda kurgu sahnesinin yeni klibi.

LLM gerektirmez; yalnız konu klasörüne (review.json, klipler.json, gelen/, istem paketi) yazar, config.toml'a
yazmaz.
"""

import json
import re
from pathlib import Path

import streamlit as st

from app.services import batch_review, fiction, scene_edits
from app.services import batch_production as bp

STYLE_TEXT_FIELDS = ("visual_language", "lighting", "color", "era", "science")
STATUS_COLORS = {fiction.WAITING: "orange", fiction.ARRIVED: "green", fiction.UNSUITABLE: "red"}


def _md(text) -> str:
    return re.sub(r"([\\`*_\[\]#$<>|~])", r"\\\1", str(text or ""))


# ---------------------------------------------------------------- inceleme: sahne planı


def plan_keys(batch_dir: Path, entry) -> dict:
    """Öğe anahtarları planın içeriğini içerir: plan değişince (yeniden üretim, kayıt) öğeler yeni içerikle başlar."""
    plan = entry.review["scene_plan"]
    version = batch_review.script_sha256(json.dumps(plan, ensure_ascii=False, sort_keys=True))[:12]
    base = f"fx_{batch_dir.name}_{entry.item.index}"
    keys = {"base": base, "save": f"{base}_save"}
    for name in STYLE_TEXT_FIELDS + ("recurring",):
        keys[f"style_{name}"] = f"{base}_style_{name}_{version}"
    for scene in plan["scenes"]:
        keys[f"prompt_{scene['index']}"] = f"{base}_prompt_{scene['index']}_{version}"
    return keys


def _recurring_text(style: dict) -> str:
    return "\n".join(f"{item['name']}: {item['description']}" for item in style.get("recurring") or [])


def _recurring_list(text: str) -> list[dict]:
    items = []
    for line in str(text or "").splitlines():
        name, _, description = line.partition(":")
        if name.strip():
            items.append({"name": name.strip(), "description": description.strip()})
    return items


def plan_edits(batch_dir: Path, entry) -> dict | None:
    """Oturumdaki (kaydedilmemiş) plan düzenlemeleri {"prompts", "style_book"}; yoksa None."""
    review = entry.review or {}
    plan = review.get("scene_plan")
    if not plan:
        return None
    keys = plan_keys(batch_dir, entry)
    style = plan["style_book"]
    edited_style = {name: str(st.session_state.get(keys[f"style_{name}"], style.get(name, ""))).strip()
                    for name in STYLE_TEXT_FIELDS}
    edited_style["recurring"] = _recurring_list(st.session_state.get(keys["style_recurring"], _recurring_text(style)))
    prompts = {}
    for scene in plan["scenes"]:
        text = str(st.session_state.get(keys[f"prompt_{scene['index']}"], scene["prompt"])).strip()
        if text and text != scene["prompt"].strip():
            prompts[scene["index"]] = text
    style_changed = any(edited_style[name] != str(style.get(name, "")).strip() for name in STYLE_TEXT_FIELDS) \
        or edited_style["recurring"] != (style.get("recurring") or [])
    if not prompts and not style_changed:
        return None
    return {"prompts": prompts, **({"style_book": edited_style} if style_changed else {})}


def render_plan_editor(tr, batch_dir: Path, entry, text: str, busy: bool) -> bool:
    """Sahne planı ve istemler (düzenlenebilir); "Planı kaydet"e basıldıysa True."""
    plan = entry.review["scene_plan"]
    keys = plan_keys(batch_dir, entry)
    style = plan["style_book"]
    defaults = {keys[f"style_{name}"]: style.get(name, "") for name in STYLE_TEXT_FIELDS}
    defaults[keys["style_recurring"]] = _recurring_text(style)
    defaults.update({keys[f"prompt_{s['index']}"]: s["prompt"] for s in plan["scenes"]})
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value
    title = tr("batch.fiction.plan_header").format(count=plan["scene_count"], seconds=plan["clip_seconds"])
    with st.expander(title, expanded=True):
        if not fiction.plan_current(plan, batch_review.normalize_script(text)):
            st.warning(tr("batch.fiction.plan_stale"), icon=":material/sync_problem:")
        st.markdown(f"**{tr('batch.fiction.style_book')}**")
        columns = st.columns(2)
        for position, name in enumerate(STYLE_TEXT_FIELDS):
            columns[position % 2].text_area(tr(f"batch.fiction.style.{name}"), key=keys[f"style_{name}"], height=90,
                                            disabled=busy)
        st.text_area(tr("batch.fiction.style.recurring"), key=keys["style_recurring"], height=90, disabled=busy)
        for scene in plan["scenes"]:
            with st.container(border=True, key=f"{keys['base']}_scene_{scene['index']}"):
                title = tr("batch.fiction.scene").format(index=f"{scene['index']:02d}", file=scene["file"])
                edited = f" :gray-badge[{tr('batch.fiction.prompt_edited')}]" if scene.get("prompt_edited") else ""
                st.markdown(f"**{title}**{edited}")
                st.caption(f"{tr('batch.fiction.narration')}: {_md(scene['narration'])}")
                st.caption(f"{tr('batch.fiction.visual')}: {_md(scene.get('visual_tr', ''))}")
                st.text_area(tr("batch.fiction.prompt"), key=keys[f"prompt_{scene['index']}"], height=150,
                             disabled=busy)
        package = entry.folder / fiction.PACKAGE_FILE
        st.caption(tr("batch.fiction.package_path").format(path=str(package)))
        return st.button(tr("batch.fiction.save_plan"), key=keys["save"], disabled=busy, icon=":material/save:")


# ---------------------------------------------------------------- onaydan sonra: klipler


def _scene_label(tr, status: str) -> str:
    return tr(f"batch.fiction.status.{status}")


def render_clips(tr, view, entry, flash) -> None:
    """Onaylı kurgu konusu: sahne kartları (istem, durum, yükleme), gelen/ kontrolü ve eşleştirme."""
    review, item, folder = entry.review, entry.item, entry.folder
    plan = review["scene_plan"]
    fit = batch_review.clip_fit_mode(view.state)
    states = {state["index"]: state for state in fiction.scene_states(folder, plan)}
    arrived = sum(state["status"] == fiction.ARRIVED for state in states.values())
    base = f"fx_{view.batch_dir.name}_{item.index}"
    ready = arrived == len(states) and fiction.clips_complete(folder, plan)
    with st.container(border=True, key=f"batch_review_item_{item.index}"):
        badge = (f":green-badge[{tr('batch.fiction.ready_badge')}]" if ready
                 else f":orange-badge[{tr('batch.status.awaiting_clips')}]")
        st.markdown(f"**{item.index}. {_md(item.subject)}** {badge}  \n"
                    + tr("batch.fiction.clips_header").format(arrived=arrived, total=len(states)))
        st.caption(tr("batch.fiction.clips_intro").format(seconds=plan["clip_seconds"],
                                                         folder=str(folder / fiction.INBOX)))
        st.caption(tr("batch.fiction.package_path").format(path=str(folder / fiction.PACKAGE_FILE)))
        if st.button(tr("batch.fiction.check"), key=f"{base}_check", icon=":material/folder_open:"):
            result = fiction.check_inbox(folder, plan, fit)
            matched = ", ".join(f"{m['file']} → {m['scene']:02d}" for m in result["matched"]) or "—"
            flash("success", tr("batch.fiction.checked").format(matched=matched))
            st.rerun()
        unmatched = fiction.load_clips(folder).get("unmatched") or []
        if unmatched:
            st.warning(tr("batch.fiction.unmatched"), icon=":material/help:")
            options = [scene["index"] for scene in plan["scenes"]]
            for name in unmatched:
                # The element key (and its st-key-… CSS class) without dots and spaces from the file name.
                slug = re.sub(r"[^0-9A-Za-z_-]", "_", name)
                columns = st.columns([3, 2, 1], vertical_alignment="bottom")
                columns[0].markdown(f"`{_md(name)}`")
                chosen = columns[1].selectbox(tr("batch.fiction.assign_scene").format(name=name), options,
                                              key=f"{base}_assign_{slug}",
                                              format_func=lambda index: f"{index:02d}")
                if columns[2].button(tr("batch.fiction.assign"), key=f"{base}_assign_btn_{slug}"):
                    try:
                        fiction.assign(folder, plan, name, int(chosen), fit_mode=fit)
                    except fiction.FictionError as exc:
                        flash("error", tr("batch.fiction.error").format(error=exc))
                    st.rerun()
        if ready:
            st.success(tr("batch.fiction.ready"), icon=":material/check_circle:")
        for scene in plan["scenes"]:
            state = states[scene["index"]]
            with st.container(border=True, key=f"{base}_clip_{scene['index']}"):
                color = STATUS_COLORS.get(state["status"], "gray")
                title = tr("batch.fiction.scene").format(index=f"{scene['index']:02d}", file=scene["file"])
                st.markdown(f"**{title}** :{color}-badge[{_scene_label(tr, state['status'])}]"
                            + (f" `{_md(state['file'])}`" if state["file"] else ""))
                if state["reason"]:
                    st.error(state["reason"], icon=":material/block:")
                for warning in state["warnings"]:
                    st.caption(f":orange[{_md(warning)}]")
                st.caption(f"{tr('batch.fiction.narration')}: {_md(scene['narration'])}")
                st.caption(f"{tr('batch.fiction.visual')}: {_md(scene.get('visual_tr', ''))}")
                st.code(scene["prompt"], language=None, wrap_lines=True)
                key = f"{base}_upload_{scene['index']}"
                upload = st.file_uploader(tr("batch.fiction.upload").format(index=f"{scene['index']:02d}"), key=key,
                                          type=[suffix.lstrip(".") for suffix in fiction.VIDEO_SUFFIXES])
                if upload is not None and st.session_state.get(f"{key}_done") != upload.file_id:
                    st.session_state[f"{key}_done"] = upload.file_id
                    try:
                        entry_ = fiction.accept_upload(folder, plan, scene["index"], upload.name, upload.getvalue(), fit)
                    except fiction.FictionError as exc:
                        flash("error", tr("batch.fiction.error").format(error=exc))
                    else:
                        flash("success" if entry_["status"] == fiction.ARRIVED else "warning",
                              tr("batch.fiction.uploaded").format(index=f"{scene['index']:02d}",
                                                                  status=_scene_label(tr, entry_["status"])))
                    st.rerun()


def is_fiction(entry) -> bool:
    return bool(entry.review and entry.review.get("scene_plan"))


def shows_clips(entry) -> bool:
    """Klip paneli: onaylı (render bekleyen ya da klip bekleyen) kurgu konuları."""
    return is_fiction(entry) and entry.status in (batch_review.AWAITING_CLIPS, bp.APPROVED)


# ---------------------------------------------------------------- revizyon: kurgu sahnesinin yeni klibi


def revision_replace(tr, task_id: str, base: dict, scene_id: str, materials: dict, add_op, close) -> None:
    """Kurgu görevinde sahne değiştirme: istem (düzenlenip kopyalanır) ve Flow'dan gelen yeni klip."""
    from app.services import material_upload

    scene = next(s for s in base["scenes"] if s["id"] == scene_id)
    source = (materials.get(scene["material"]) or {}).get("source") or {}
    key = f"revision_fiction_{task_id}_{scene_id}"
    st.caption(tr("revision.fiction.hint").format(seconds=f"{float(scene.get('duration') or 0):.1f}"))
    prompt = st.text_area(tr("revision.fiction.prompt"), value=source.get("prompt", ""), key=f"{key}_prompt",
                          height=170)
    st.code(prompt, language=None, wrap_lines=True)
    upload = st.file_uploader(tr("revision.fiction.upload"), key=f"{key}_upload",
                              type=[suffix.lstrip(".") for suffix in fiction.VIDEO_SUFFIXES])
    if upload is not None and st.session_state.get(f"{key}_uploaded") != upload.file_id:
        st.session_state[f"{key}_uploaded"] = upload.file_id
        st.session_state[f"{key}_path"] = material_upload.save_material_upload(upload.name, upload)
    path = st.session_state.get(f"{key}_path")
    if path:
        info = fiction.probe(path)
        status, reason, warnings = fiction.validate_clip(info, float(scene.get("duration") or 0))
        if status != fiction.ARRIVED:
            st.error(reason, icon=":material/block:")
        for warning in warnings:
            st.caption(f":orange[{_md(warning)}]")
        if st.button(tr("revision.replace.add"), key=f"{key}_add", type="primary", disabled=status != fiction.ARRIVED):
            edited = prompt.strip() if prompt.strip() != str(source.get("prompt") or "").strip() else ""
            add_op(task_id, base["revision"], scene_edits.SceneOp(scene_edits.REPLACE, scene_id, path=path,
                                                                  prompt=edited))
            st.session_state.pop(f"{key}_path", None)
            close()
            st.rerun()
