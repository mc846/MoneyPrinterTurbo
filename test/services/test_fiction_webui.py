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
fiction_panel.revision_replace(tr, "t1", base, "v1-s02", materials, lambda *a: None, lambda: None)
"""


def test_the_revision_dialog_of_a_fiction_scene_offers_its_prompt():
    app = AppTest.from_string(REVISION_SCRIPT.format(path=str(ROOT / "webui/i18n/en.json")), default_timeout=60)
    app = run(app)
    prompt = next(area for area in app.text_area if area.label.startswith("Veo prompt of this scene"))
    assert prompt.value == "the old prompt" and app.code[0].value == "the old prompt"
    assert any("at least 7.4 s" in str(item.value) for item in app.caption)
    assert not [button for button in app.button if button.label == "Add to the changes"]  # no clip yet
