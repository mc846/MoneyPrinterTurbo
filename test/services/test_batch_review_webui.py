"""WebUI Toplu Üretim: iki aşamalı akışın inceleme paneli (AppTest). Hazırlık sahte fonksiyonla; LLM gerektiren
işlemler alt süreç yerine sahte başlatıcıya gider. config.toml'a yazılmaz."""

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest
from streamlit.testing.v1 import AppTest

from app.config import config
from app.services import batch_review, voice
from app.services import batch_production as bp
from app.services import batch_runner as br
from test.services.test_batch_review import SCRIPTS, fake_preparer

ROOT = Path(__file__).resolve().parents[2]
PRESET = ROOT / "presets" / "uzay.toml"
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
    source.write_text("Kara delikler\nAy\nNötron yıldızları\n", encoding="utf-8")
    with patch.dict(config.app, {}):
        assert bp.run_batch(source, PRESET, output=folder / "20261109-120000_webui", preparer=fake_preparer(),
                            reporter=lambda state, batch_dir: None) == 0
    with patch.object(config, "try_save_config") as soft, patch.object(config, "save_config") as hard:
        yield folder / "20261109-120000_webui"
    soft.assert_not_called()
    hard.assert_not_called()


def page():
    app = AppTest.from_string(SCRIPT.format(path=str(ROOT / "webui/i18n/en.json")), default_timeout=60)
    return run(app)


def run(app):
    app.run()
    assert not app.exception, [str(item.value) for item in app.exception]
    return app


def scripts(app):
    return [area for area in app.text_area if area.label == "Script"]


def item_folder(batch_dir, index):
    return batch_dir / bp.BatchState.load(batch_dir).items[index - 1].folder


def button(app, batch_dir, index, name):
    return app.button(key=f"rv_{batch_dir.name}_{index}_{name}")


def markdown(app):
    return "\n".join(str(item.value) for item in app.markdown)


def metrics(app):
    return {item.label: item.value for item in app.metric}


def test_panel_shows_scripts_audit_and_summary(batch):
    app = page()
    assert [area.value for area in scripts(app)] == [SCRIPTS["Kara delikler"], SCRIPTS["Ay"], SCRIPTS["Nötron yıldızları"]]
    assert metrics(app) == {"Awaiting review": "3", "Approved, not rendered": "0", "With Y/X": "1", "Skipped": "0",
                            "Rendered": "0"}
    text = markdown(app)
    assert ":orange-badge[Y] Merkezdeki kara delik Sagittarius A." in text and ":small[Adı Sagittarius A\\*.]" in text
    assert button(app, batch, 1, "approve").label == "Approve anyway"  # güncel denetimde Y var
    assert button(app, batch, 2, "approve").label == "Approve"
    assert app.button(key="batch_review_approve_clean").label == "Approve clean ones (2)"
    assert app.button(key="batch_review_render").disabled  # onaylı yok
    words = len(SCRIPTS["Ay"].split())  # örnek metinler kısa: hedef dışı, uyarı olarak
    assert any(f"{words} words · about" in str(item.value) and "word target 90–100" in str(item.value)
               for item in app.warning)


def test_editing_updates_the_estimate_and_marks_the_audit_outdated(batch):
    app = page()
    scripts(app)[1].set_value("Ay " * 120)
    run(app)
    warnings = [str(item.value) for item in app.warning]
    assert any("120 words" in text and "word target 90–100" in text for text in warnings)
    assert ":gray-badge[outdated check]" in markdown(app)
    assert button(app, batch, 2, "approve").label == "Approve"  # eski denetim: önce yeniden denetlenecek


def test_unsaved_edit_survives_refreshes_and_other_subjects_changing(batch):
    app = page()
    edited = SCRIPTS["Kara delikler"] + " Kaydedilmemiş düzenleme."
    scripts(app)[0].set_value(edited)
    run(app)
    batch_review.skip(item_folder(batch, 3))  # başka bir konu (başka oturum ya da komut) değişti
    run(app)  # sayfanın kendini yenilemesi
    run(app)
    assert scripts(app)[0].value == edited
    assert batch_review.load_review(item_folder(batch, 1))["script"] == SCRIPTS["Kara delikler"]  # dosyaya yazılmadı
    assert "Unsaved edits (subjects 1)" in " ".join(str(item.value) for item in app.caption)


def test_approve_without_changes_needs_no_llm(batch):
    app = page()
    with patch.object(br, "start_review_action") as start:
        button(app, batch, 2, "approve").click()
        run(app)
    start.assert_not_called()
    assert batch_review.is_approved(batch_review.load_review(item_folder(batch, 2)))
    assert metrics(app)["Approved, not rendered"] == "1" and len(scripts(app)) == 2
    assert app.button(key="batch_review_render").label == "Render approved (1)"


def test_approve_with_a_changed_script_runs_the_command_in_a_subprocess(batch):
    app = page()
    edited = SCRIPTS["Kara delikler"].replace("Sagittarius A", "Sagittarius A*")
    scripts(app)[0].set_value(edited)
    run(app)
    with patch.object(br, "start_review_action") as start:
        button(app, batch, 1, "approve").click()
        run(app)
    batch_dir, folder, index, action, arguments = start.call_args.args
    assert (batch_dir, folder, index, action) == (batch, item_folder(batch, 1), 1, "onayla")
    [edit_file] = [argument.split("=", 1)[1] for argument in arguments if argument.startswith("--metin-dosyasi=")]
    assert Path(edit_file).read_text(encoding="utf-8") == edited and not any("--yine-de" in a for a in arguments)
    assert not batch_review.load_review(item_folder(batch, 1))["approved"]


def test_regenerate_and_reaudit_pass_the_instruction_and_edited_terms(batch):
    app = page()
    app.text_input(key=f"rv_{batch.name}_2_instruction").input("Daha sade yaz")
    run(app)
    with patch.object(br, "start_review_action") as start:
        button(app, batch, 2, "regenerate").click()
        run(app)
    assert start.call_args.args[3:] == ("yeniden-uret", ["--talimat=Daha sade yaz"])
    terms = [area for area in app.text_area if area.label == "Search terms (one per line)"][2]
    terms.set_value("neutron star\npulsar")
    run(app)
    with patch.object(br, "start_review_action") as start:
        button(app, batch, 3, "reaudit").click()
        run(app)
    assert start.call_args.args[3:] == ("yeniden-denetle", ["--terimler=neutron star, pulsar"])


def test_busy_subject_locks_its_buttons_and_shows_progress(batch):
    assert batch_review.mark_busy(item_folder(batch, 1), "yeniden-uret", os.getpid())
    app = page()
    assert all(button(app, batch, 1, name).disabled for name in ("approve", "regenerate", "reaudit", "skip"))
    assert scripts(app)[0].disabled and not scripts(app)[1].disabled
    assert not any(button(app, batch, 2, name).disabled for name in ("approve", "regenerate", "reaudit", "skip"))
    assert any("Regeneration in progress" in str(item.value) for item in app.info)
    assert app.button(key="batch_review_approve_clean").label == "Approve clean ones (2)"


def test_approve_clean_leaves_out_flagged_and_unsaved_subjects(batch):
    app = page()
    scripts(app)[2].set_value(SCRIPTS["Nötron yıldızları"] + " Düzenlendi.")
    run(app)
    assert app.button(key="batch_review_approve_clean").label == "Approve clean ones (1)"
    app.button(key="batch_review_approve_clean").click()
    run(app)
    approved = [batch_review.is_approved(batch_review.load_review(item_folder(batch, i))) for i in (1, 2, 3)]
    assert approved == [False, True, False]
    assert "Approved: 2" in [str(item.value) for item in app.success]


def test_render_button_starts_the_render_phase(batch):
    batch_review.approve(item_folder(batch, 2))
    app = page()
    with patch.object(br, "start_render", return_value=4242) as start:
        app.button(key="batch_review_render").click()
        run(app)
    start.assert_called_once_with(batch)
    assert any("Rendering started" in str(item.value) for item in app.success)


def test_render_button_is_locked_while_a_batch_runs(batch, monkeypatch):
    batch_review.approve(item_folder(batch, 2))
    monkeypatch.setattr(br, "current_lock", lambda: {"pid": 1, "batch_dir": str(batch.parent / "x"), "started": ""})
    app = AppTest.from_string(SCRIPT.format(path=str(ROOT / "webui/i18n/en.json")), default_timeout=60)
    app.run()
    assert app.button(key="batch_review_render").disabled


def test_skip_and_undo_skip(batch):
    app = page()
    button(app, batch, 3, "skip").click()
    run(app)
    assert batch_review.load_review(item_folder(batch, 3))["skipped"] and len(scripts(app)) == 2
    app.button(key=f"rv_{batch.name}_3_unskip").click()
    run(app)
    assert not batch_review.load_review(item_folder(batch, 3))["skipped"] and len(scripts(app)) == 3


def test_failed_and_refused_actions_are_shown(batch):
    folder = item_folder(batch, 2)
    batch_review.record_failure(folder, "yeniden-uret", "RuntimeError: kota doldu")
    batch_review.approve(item_folder(batch, 1))  # Y: onaylanmadı, needs_force
    app = page()
    assert any("Regeneration failed: RuntimeError: kota doldu" in str(item.value) for item in app.error)
    assert any("Not approved" in str(item.value) and "Y 1" in str(item.value) for item in app.warning)


def test_history_marks_batches_awaiting_review(batch):
    app = page()
    card = markdown(app)
    assert ":orange-badge[awaiting review]" in card and "3 awaiting review · 0 approved · 0 skipped" in card
    assert app.button(key=f"batch_review_open_{batch.name}").label == "Review"


def test_single_phase_start_when_the_option_is_cleared(batch):
    app = page()
    app.session_state["batch_editor_0"] = {"edited_rows": {}, "added_rows": [{"konu": "Ay"}], "deleted_rows": []}
    run(app)
    app.checkbox(key="batch_review_flow_uzay.toml").uncheck()
    run(app)
    with patch.object(br, "start_batch", return_value=4242) as start:
        app.button(key="batch_start").click()
        run(app)
    assert start.call_args.kwargs["review"] is False
    assert any(str(item.value).startswith("Batch started:") for item in app.success)
    assert json.loads((bp.batches_dir() / ".taslak.json").read_text(encoding="utf-8"))["rows"][0]["konu"] == "Ay"
