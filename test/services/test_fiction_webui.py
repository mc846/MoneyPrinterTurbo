"""WebUI kurgu kanalı (AppTest): inceleme panelinde sahne planı ve istemler (düzenleme, kaydetme), onaydan sonra
sahne kartları, gelen/ kontrolü, numarasız dosyanın eşleştirilmesi ve render düğmesinin koşulu; revizyonda kurgu
sahnesinin penceresi. Hazırlık sahte; klipler ffmpeg ile. config.toml'a yazılmaz."""

from pathlib import Path
from unittest.mock import patch

import pytest
from streamlit.testing.v1 import AppTest

from app.config import config
from app.services import batch_review, fiction, voice
from app.services import batch_production as bp
from app.services import batch_runner as br
from test.services.test_fiction import PRESET, fiction_preparer, make_clip
from test.services.test_task_revisions import storage  # noqa: F401 -- fikstür (revizyon sayfası testleri)

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = """
import json
from pathlib import Path
from webui.batch_page import render_batch_page
tr = json.loads(Path({path!r}).read_text(encoding="utf-8"))["Translation"].__getitem__
render_batch_page(tr)
"""


@pytest.fixture
def batch(tmp_path, monkeypatch):
    folder = tmp_path / "batches"
    folder.mkdir()
    monkeypatch.setattr(bp, "batches_dir", lambda: folder)
    monkeypatch.setattr(voice, "get_all_azure_voices", lambda filter_locals=None: ["tr-TR-EmelNeural-Female"])
    monkeypatch.setattr(br, "current_lock", lambda: None)
    source = tmp_path / "konular.txt"
    source.write_text("Ya olsaydı: Ay'da helyum-3 madenciliği yapılsaydı\n", encoding="utf-8")
    with patch.dict(config.app, {}):
        assert bp.run_batch(source, PRESET, output=folder / "20261114-100000_kurgu", preparer=fiction_preparer,
                            reporter=lambda state, batch_dir: None) == 0
    with patch.object(config, "try_save_config") as soft, patch.object(config, "save_config") as hard:
        yield folder / "20261114-100000_kurgu"
    soft.assert_not_called()
    hard.assert_not_called()


def page():
    app = AppTest.from_string(SCRIPT.format(path=str(ROOT / "webui/i18n/en.json")), default_timeout=90)
    return run(app)


def run(app):
    app.run()
    assert not app.exception, [str(item.value) for item in app.exception]
    return app


def folder_of(batch_dir):
    return batch_dir / bp.BatchState.load(batch_dir).items[0].folder


def markdown(app):
    return "\n".join(str(item.value) for item in app.markdown)


def test_the_review_panel_shows_the_plan_and_saves_prompt_edits(batch):
    plan = batch_review.load_review(folder_of(batch))["scene_plan"]
    app = page()
    assert any(expander.label.startswith(f"Scene plan and Veo prompts ({plan['scene_count']} scenes, 8 s each)")
               for expander in app.expander)
    prompts = [area for area in app.text_area if area.label == "Veo prompt (English)"]
    assert [area.value for area in prompts] == [scene["prompt"] for scene in plan["scenes"]]
    assert any(area.label == "Visual language" for area in app.text_area)
    assert not any(area.label == "Search terms" for area in app.text_area)  # fiction: no search terms
    prompts[1].set_value("A hand-written prompt for scene two.")
    app = run(app)
    app.button(key=f"fx_{batch.name}_1_save").click()
    app = run(app)
    saved = batch_review.load_review(folder_of(batch))["scene_plan"]["scenes"][1]
    assert saved["prompt"] == "A hand-written prompt for scene two." and saved["prompt_edited"]
    assert "A hand-written prompt for scene two." in (folder_of(batch) / fiction.PACKAGE_FILE).read_text(encoding="utf-8")
    assert "edited: kept when the plan is regenerated" in markdown(app)


def test_after_approval_the_scene_cards_wait_for_clips(batch):
    folder = folder_of(batch)
    batch_review.approve(folder)
    plan = batch_review.load_review(folder)["scene_plan"]
    app = page()
    text = markdown(app)
    assert "Waiting for clips" in text and f"Clips: 0/{plan['scene_count']} arrived" in text
    assert len([item for item in app.code]) == plan["scene_count"]
    assert app.code[0].value == plan["scenes"][0]["prompt"]
    assert app.button(key="batch_review_render").disabled  # nothing ready
    # Clips land in the inbox: one with a scene number, one without (the user picks its scene).
    make_clip(fiction.inbox(folder) / "sahne-01.mp4")
    make_clip(fiction.inbox(folder) / "ay yuzeyi.mp4")
    app.button(key=f"fx_{batch.name}_1_check").click()
    app = run(app)
    assert "1/" in markdown(app) and "These files have no scene number" in "\n".join(str(w.value) for w in app.warning)
    box = app.selectbox(key=f"fx_{batch.name}_1_assign_ay_yuzeyi_mp4")  # file name made key-safe
    box.set_value(2)
    app.button(key=f"fx_{batch.name}_1_assign_btn_ay_yuzeyi_mp4").click()
    app = run(app)
    states = {s["index"]: s["status"] for s in fiction.scene_states(folder, plan)}
    assert states[1] == states[2] == fiction.ARRIVED and states[3] == fiction.WAITING
    assert f"Clips: 2/{plan['scene_count']} arrived" in markdown(app)


def test_a_short_clip_is_shown_as_not_suitable_and_all_clips_enable_the_render(batch, monkeypatch):
    folder = folder_of(batch)
    batch_review.approve(folder)
    plan = batch_review.load_review(folder)["scene_plan"]
    make_clip(fiction.inbox(folder) / "sahne-01.mp4", seconds=5)
    for scene in plan["scenes"][1:]:
        make_clip(fiction.inbox(folder) / scene["file"])
    fiction.check_inbox(folder, plan)
    app = page()
    assert any("short: 5.0" in str(item.value) or "kısa: 5.0 sn" in str(item.value) for item in app.error)
    assert app.button(key="batch_review_render").disabled
    make_clip(fiction.inbox(folder) / "sahne-01.mp4")  # a new export of scene 1
    app.button(key=f"fx_{batch.name}_1_check").click()
    app = run(app)
    assert "Ready to render" in markdown(app)
    render = app.button(key="batch_review_render")
    assert not render.disabled and render.label.endswith("(1)")
    started = []
    monkeypatch.setattr(br, "start_render", lambda batch_dir: started.append(batch_dir))
    render.click()
    run(app)
    assert started == [batch]


REVISION_SCRIPT = """
import json
from pathlib import Path
import streamlit as st
from webui import fiction_panel
tr = json.loads(Path({path!r}).read_text(encoding="utf-8"))["Translation"].__getitem__
base = {{"revision": "r001", "scenes": [{{"id": "v1-s02", "material": "m02", "duration": 7.4}}]}}
materials = {{"m02": {{"source": {{"provider": "ai_manual", "prompt": "the old prompt"}}}}}}
fiction_panel.revision_replace(tr, "t1", base, "v1-s02", materials, None, lambda *a: None, lambda *a: None,
                               lambda: None)
"""


def test_a_flagged_clip_needs_use_anyway_before_the_render(batch, monkeypatch):
    from app.services import fiction_screen
    from test.services.test_fiction_screen import FLAG, FakeModel

    model = FakeModel({2: FLAG})
    monkeypatch.setattr(fiction_screen, "_ask_backend", model)
    folder = folder_of(batch)
    batch_review.approve(folder)
    plan = batch_review.load_review(folder)["scene_plan"]
    for scene in plan["scenes"]:
        make_clip(fiction.inbox(folder) / scene["file"])
    app = page()
    app.button(key=f"fx_{batch.name}_1_check").click()  # matched, then screened
    app = run(app)
    assert len(model.calls) == len(plan["scenes"])
    text = markdown(app)
    assert ":red-badge[Screening: check needed] bayrak: US flag on the left shoulder" in text
    assert text.count(":green-badge[Screening: clean]") == len(plan["scenes"]) - 1
    assert "Screening needs a check: scene 02" in "\n".join(str(w.value) for w in app.warning)
    assert app.button(key="batch_review_render").disabled and "Ready to render" not in text
    app.button(key=f"fx_{batch.name}_1_use_anyway_2").click()
    app = run(app)
    assert "Ready to render" in markdown(app) and not app.button(key="batch_review_render").disabled
    assert any("Will be used anyway" in str(item.value) for item in app.caption)
    app.button(key=f"fx_{batch.name}_1_revoke_2").click()
    app = run(app)
    assert app.button(key="batch_review_render").disabled
    app.button(key=f"fx_{batch.name}_1_rescan_1").click()  # screened again, not from the cache
    run(app)
    assert len(model.calls) == len(plan["scenes"]) + 1


def test_an_uploaded_clip_is_kept_with_its_full_path(tmp_path, monkeypatch):
    """save_material_upload returns a bare file name; the dialogs keep the full path (E2E 14.11)."""
    import streamlit as st

    from app.services import material_upload
    from webui import fiction_panel

    (tmp_path / "uploads").mkdir()
    clip = make_clip(tmp_path / "uploads" / "x.mp4")
    monkeypatch.setattr(material_upload, "uploaded_material_dir", lambda create=True: str(tmp_path / "uploads"))
    monkeypatch.setattr(material_upload, "save_material_upload", lambda name, source: "x.mp4")
    upload = type("Upload", (), {"name": "flow.mp4", "file_id": "f1"})()
    added = []
    state = {}
    monkeypatch.setattr(st, "session_state", state)
    for name in ("caption", "code", "markdown", "error", "button", "success", "rerun"):
        monkeypatch.setattr(st, name, lambda *args, **kwargs: False)
    monkeypatch.setattr(st, "text_area", lambda *args, **kwargs: kwargs.get("value", ""))
    monkeypatch.setattr(st, "file_uploader", lambda *args, **kwargs: upload)
    base = {"revision": "r001", "scenes": [{"id": "v1-s02", "material": "m02", "duration": 7.4}]}
    fiction_panel.revision_replace(lambda key: key, "t1", base, "v1-s02",
                                   {"m02": {"source": {"prompt": "p"}}}, None, lambda *a: added.append(a),
                                   lambda *a: None, lambda: None)
    assert state["revision_fiction_t1_r001_v1-s02_path"] == str(clip)
    # Not screened in tests (nothing blocks it): the change is added by itself, with the full path.
    assert [a[2].path for a in added] == [str(clip)]


def test_the_revision_dialog_of_a_fiction_scene_offers_its_prompt():
    app = AppTest.from_string(REVISION_SCRIPT.format(path=str(ROOT / "webui/i18n/en.json")), default_timeout=60)
    app = run(app)
    prompt = next(area for area in app.text_area if area.label.startswith("Veo prompt of this scene"))
    assert prompt.value == "the old prompt" and app.code[0].value == "the old prompt"
    assert any("at least 7.4 s" in str(item.value) for item in app.caption)
    assert not [button for button in app.button if button.label == "Add to the changes"]  # no clip yet


# ---------------------------------------------------------------- revizyon paneli: bekleyen değişiklik akışı

BANNER = "The revision has not been made yet: {count} change(s) pending."


@pytest.fixture
def fiction_panel_page(storage, monkeypatch):  # noqa: F811 -- storage: test_task_revisions fikstürü
    """Kurgu görevinin revizyon sayfası (Main.py): tarama sahte (``answers``), arka plan süreci kaydedilir."""
    from app.services import fiction_screen, task_revisions
    from test.services.test_fiction_screen import FakeModel
    from test.services.test_fiction_timing import CUES, _fiction_task
    from test.services.test_task_revisions import _revision_page

    started, saves = [], []
    monkeypatch.setattr(task_revisions, "start_background", lambda task, arguments: started.append(arguments) or 1)
    monkeypatch.setattr(config, "save_config", lambda *a, **k: saves.append("save"))
    monkeypatch.setattr(config, "try_save_config", lambda *a, **k: saves.append("try") or True)

    def open_page(answers=None):
        model = FakeModel(answers or {})
        monkeypatch.setattr(fiction_screen, "_ask_backend", model)
        task_id = _fiction_task(storage, CUES)
        app = _revision_page(task_id).run()
        assert not app.exception
        app.button(key="revision_replace_btn_v1-s02").click().run()
        assert not app.exception
        key = f"revision_fiction_{task_id}_r001_v1-s02"
        # What the upload stores (AppTest cannot drive st.file_uploader): the saved clip's full path.
        app.session_state[f"{key}_path"] = str(make_clip(storage / "flow-yeni.mp4"))
        app.run()
        assert not app.exception
        return app, key, model

    yield {"open": open_page, "started": started}
    assert saves == []


def _pending(app):
    return [m.value for m in app.markdown if m.value.startswith("- v1-s")]


def _banner(app):
    return [str(w.value) for w in app.warning if "has not been made yet" in str(w.value)]


def test_a_clean_clip_joins_the_changes_by_itself_and_survives_closing_the_dialog(fiction_panel_page,
                                                                                    storage):  # noqa: F811
    app, key, model = fiction_panel_page["open"]()
    clip = storage / "flow-yeni.mp4"
    assert len(model.calls) == 1 and ":green-badge[Screening: clean]" in markdown(app)
    assert any("Added to the pending changes" in str(item.value) for item in app.success)
    assert _pending(app) == ["- v1-s02: replace with the file flow-yeni.mp4"]
    assert _banner(app) == [BANNER.format(count=1)]
    assert not app.button(key="revision_generate").disabled
    # An edited prompt goes with the pending change (screened again with the new prompt).
    app.text_area(key=f"{key}_prompt").set_value("Scene 2 of 3: a plain, unmarked suit").run()
    assert not app.exception and len(model.calls) == 2
    app.button(key=f"{key}_done").click().run()  # closed: the change and the clip stay
    assert not [area for area in app.text_area if area.label.startswith("Veo prompt")]
    assert _pending(app) == ["- v1-s02: replace with the file flow-yeni.mp4"] and _banner(app)
    del app.session_state[f"{key}_path"]  # even without the session's upload, the pending change has the clip
    app.button(key="revision_replace_btn_v1-s02").click().run()
    assert any("Uploaded clip: flow-yeni.mp4" in str(item.value) for item in app.caption)
    assert app.text_area(key=f"{key}_prompt").value == "Scene 2 of 3: a plain, unmarked suit"
    assert len(model.calls) == 2  # the same clip and prompt: from the cache
    app.button(key=f"{key}_done").click().run()
    app.button(key="revision_generate_top").click().run()  # the button above the form
    assert not app.exception
    assert fiction_panel_page["started"] == [["--temel-revizyon=r001", f"--sahne-dosya=v1-s02={clip}",
                                              "--sahne-istemi=v1-s02=Scene 2 of 3: a plain, unmarked suit"]]
    assert _pending(app) == [] and not _banner(app)
    assert not [name for name in app.session_state.keys() if str(name).startswith(key)]


def test_a_flagged_clip_joins_the_changes_only_with_use_anyway(fiction_panel_page):
    from test.services.test_fiction_screen import FLAG

    app, key, _model = fiction_panel_page["open"]({2: FLAG})
    assert ":red-badge[Screening: check needed]" in markdown(app)
    assert _pending(app) == [] and not _banner(app) and app.button(key="revision_generate").disabled
    assert any("tick the box above" in str(item.value) for item in app.caption)
    app.checkbox(key=f"{key}_use_anyway").check().run()
    assert _pending(app) == ["- v1-s02: replace with the file flow-yeni.mp4"] and _banner(app)
    app.checkbox(key=f"{key}_use_anyway").uncheck().run()
    assert _pending(app) == [] and not _banner(app)
    app.checkbox(key=f"{key}_use_anyway").check().run()
    undo = next(b for b in app.button if b.key and b.key.startswith("revision_undo_"))
    undo.click().run()  # undone: the uploaded clip is forgotten, the dialog does not add it back
    assert not app.exception and _pending(app) == []
    assert not [name for name in app.session_state.keys() if str(name).startswith(key + "_path")]
