"""İki aşamalı toplu üretim: hazırlık, durum geçişleri, review.json, onay kuralları, render aşaması ve komutlar.
LLM, görev ve paket sahte fonksiyonlarla değişir; video üretilmez."""

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from app.config import config
from app.services import batch_package, batch_review, batch_runner
from app.services import batch_production as bp

PRESET = Path(__file__).resolve().parents[2] / "presets" / "uzay.toml"
FLAGGED = "Sagittarius A"
CLEAN_AUDIT = {"durum": "tamam", "iddialar": [{"claim": "Işık kaçamaz.", "rating": "D", "reason": "Doğru."}],
               "sayim": {"D": 1, "B": 0, "Y": 0, "X": 0}, "isaretli": []}


def flagged_audit(claim=f"Merkezdeki kara delik {FLAGGED}."):
    entry = {"claim": claim, "rating": "Y", "reason": "Adı Sagittarius A*."}
    return {"durum": "tamam", "iddialar": [CLEAN_AUDIT["iddialar"][0], entry],
            "sayim": {"D": 1, "B": 0, "Y": 1, "X": 0}, "isaretli": [entry]}


def audit_for(script: str) -> dict:
    return flagged_audit() if FLAGGED in script and f"{FLAGGED}*" not in script else CLEAN_AUDIT


SCRIPTS = {
    "Kara delikler": f"Kara deliğin içinde zaman durur mu? Samanyolu'nun merkezinde {FLAGGED} var.",
    "Ay": "Ay neden hep aynı yüzünü gösterir? Kütleçekim kilidi yüzünden.",
    "Nötron yıldızları": "Nötron yıldızları çok yoğundur. Bir çay kaşığı milyarlarca ton gelir.",
}


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(bp, "batches_dir", lambda: tmp_path / "batches")
    with patch.dict(config.app, {}):
        yield


def fake_preparer(calls=None, fail=()):
    def prepare(preset, item, task_id):
        if calls is not None:
            calls.append(item.subject)
        if item.subject in fail:
            raise bp.TaskFailed("LLM yanıt vermedi", "script")
        script = SCRIPTS[item.subject]
        return {"script": script, "script_sha256": batch_review.script_sha256(script), "terms": ["black hole", "galaxy"],
                "anchors": ["space"], "audit": audit_for(script), **batch_review.estimate(script)}
    return prepare


class FakeServices:
    """Sahte LLM işlemleri: çağrıları sayar; denetim metne göre Y verir."""

    def __init__(self, generated="Yeni metin: Samanyolu'nun merkezinde Sagittarius A* var."):
        self.calls, self.generated = [], generated

    def audit(self, subject, script):
        self.calls.append(("audit", script))
        return audit_for(script)

    def terms(self, subject, script):
        self.calls.append(("terms", script))
        return ["regenerated term"], ["regenerated anchor"]

    def generate(self, subject, instruction=""):
        self.calls.append(("generate", instruction))
        return {"script": self.generated, "terms": ["new term"], "anchors": ["new anchor"]}


class FakeRunner:
    def __init__(self, on_first=None, interrupt=False):
        self.argvs, self.on_first, self.interrupt = [], on_first, interrupt

    def __call__(self, argv, task_id):
        self.argvs.append(argv)
        if len(self.argvs) == 1 and self.on_first:
            self.on_first()
        if self.interrupt:
            raise KeyboardInterrupt
        return {"videos": [f"{task_id}.mp4"], "script": "x"}


def fake_packager(calls):
    def package(item, result, preset, folder, log_lines, review=None):
        folder.mkdir(parents=True, exist_ok=True)
        calls.append((item.subject, review))
        return {"kontrol_gerekli": bool(review and review.get("approved_despite_flags")), "nedenler": []}
    return package


def write_input(tmp_path, subjects=("Kara delikler", "Ay", "Nötron yıldızları")) -> Path:
    path = tmp_path / "konular.txt"
    path.write_text("\n".join(subjects) + "\n", encoding="utf-8")
    return path


def prepare(tmp_path, subjects=("Kara delikler", "Ay", "Nötron yıldızları"), **kwargs) -> Path:
    kwargs.setdefault("preparer", fake_preparer())
    code = bp.run_batch(write_input(tmp_path, subjects), PRESET, output=tmp_path / "out",
                        reporter=lambda state, batch_dir: None, **kwargs)
    assert code == 0
    return tmp_path / "out"


def folder(batch_dir, index) -> Path:
    return batch_dir / bp.BatchState.load(batch_dir).items[index - 1].folder


def render(batch_dir, runner=None, packages=None):
    return bp.render_batch(batch_dir, task_runner=runner or FakeRunner(),
                           packager=fake_packager([] if packages is None else packages),
                           reporter=lambda state, batch_dir: None)


# ---------------------------------------------------------------- reçete ve hazırlık

def test_preset_option(tmp_path):
    assert bp.load_preset(PRESET).review_before_render is True
    bad = tmp_path / "bad.toml"
    bad.write_text("[quality]\nreview_before_render = \"evet\"\n", encoding="utf-8")
    with pytest.raises(bp.PresetError, match="review_before_render"):
        bp.load_preset(bad)


def test_preparation_writes_files_and_waits_for_review(tmp_path):
    calls, runner = [], FakeRunner()
    batch_dir = prepare(tmp_path, preparer=fake_preparer(calls), task_runner=runner)
    state = bp.BatchState.load(batch_dir)
    assert state.review_before_render is True and not state.finished
    assert [(item.status, item.prepared) for item in state.items] == [(bp.AWAITING_REVIEW, True)] * 3
    assert calls == ["Kara delikler", "Ay", "Nötron yıldızları"] and runner.argvs == []  # ses ve video yok
    preparation = batch_review.load_preparation(folder(batch_dir, 1))
    assert preparation["script"] == SCRIPTS["Kara delikler"] and preparation["terms"] == ["black hole", "galaxy"]
    assert preparation["subject"] == "Kara delikler" and preparation["audit"]["sayim"]["Y"] == 1
    assert preparation["kelime"] == len(SCRIPTS["Kara delikler"].split()) and preparation["tahmini_sn"] > 0
    assert not (folder(batch_dir, 1) / batch_review.REVIEW_FILE).exists()  # yalnız arayüz ve komutlar yazar


def test_failed_preparation_is_retried_and_resumed(tmp_path):
    calls = []
    code = bp.run_batch(write_input(tmp_path), PRESET, output=tmp_path / "out", reporter=lambda s, d: None,
                        preparer=fake_preparer(calls, fail={"Ay"}))
    state = bp.BatchState.load(tmp_path / "out")
    assert code == 1 and calls.count("Ay") == 2  # turun sonunda bir kez daha
    assert (state.items[1].status, state.items[1].prepared, state.items[1].failed_stage) == (bp.FAILED, False, "script")
    calls.clear()
    assert bp.run_batch(write_input(tmp_path), PRESET, output=tmp_path / "out", reporter=lambda s, d: None,
                        preparer=fake_preparer(calls)) == 0
    assert calls == ["Ay"]  # hazırlananlar yeniden üretilmez


def test_interrupted_preparation_returns_to_pending(tmp_path):
    def interrupt(preset, item, task_id):
        if item.index == 2:
            raise KeyboardInterrupt
        return fake_preparer()(preset, item, task_id)

    assert bp.run_batch(write_input(tmp_path), PRESET, output=tmp_path / "out", reporter=lambda s, d: None,
                        preparer=interrupt) == 130
    statuses = [item.status for item in bp.BatchState.load(tmp_path / "out").items]
    assert statuses == [bp.AWAITING_REVIEW, bp.PENDING, bp.PENDING]


def test_single_phase_flow_is_unchanged_when_review_is_off(tmp_path):
    runner, packages, preparer_calls = FakeRunner(), [], []
    code = bp.run_batch(write_input(tmp_path), PRESET, output=tmp_path / "out", task_runner=runner,
                        packager=lambda **kwargs: packages.append(kwargs) or {"kontrol_gerekli": False},
                        reporter=lambda s, d: None, review=False, preparer=fake_preparer(preparer_calls))
    state = bp.BatchState.load(tmp_path / "out")
    assert code == 0 and state.review_before_render is False and state.finished
    assert [item.status for item in state.items] == [bp.DONE] * 3 and preparer_calls == []
    preset = bp.load_preset(PRESET)
    assert runner.argvs[0] == bp.build_task_argv(preset, state.items[0])  # reçetenin talimatıyla, metinsiz
    assert "--video-script-prompt" in runner.argvs[0] and not any(a.startswith("--video-script=") for a in runner.argvs[0])
    assert all("review" not in kwargs for kwargs in packages)  # paketleyici eski imzayla çağrılır
    assert not any((tmp_path / "out" / item.folder / batch_review.PREPARATION_FILE).exists() for item in state.items)


def test_flow_is_fixed_when_the_batch_is_created(tmp_path):
    prepare(tmp_path)
    runner = FakeRunner()
    # Aynı klasör sonradan --inceleme-yok ile sürdürülse de akış değişmez (render yapılmaz).
    assert bp.run_batch(write_input(tmp_path), PRESET, output=tmp_path / "out", task_runner=runner,
                        reporter=lambda s, d: None, review=False, preparer=fake_preparer()) == 0
    assert runner.argvs == []


def test_old_state_without_the_review_field_uses_the_preset(tmp_path):
    batch_dir = prepare(tmp_path, review=False, task_runner=FakeRunner(),
                        packager=lambda **kwargs: {"kontrol_gerekli": False})
    data = json.loads((batch_dir / bp.STATE_FILE).read_text(encoding="utf-8"))
    data.pop("review_before_render")
    for item in data["items"]:
        for key in ("prepared", "prepare_seconds", "prepare_attempts", "prepare_task_id"):
            item.pop(key)
    (batch_dir / bp.STATE_FILE).write_text(json.dumps(data), encoding="utf-8")
    state = bp.BatchState.load(batch_dir)
    assert state.review_before_render is None and state.items[0].prepared is False


# ---------------------------------------------------------------- review.json

def test_review_starts_from_the_preparation_and_is_written_atomically(tmp_path):
    batch_dir = prepare(tmp_path)
    target = folder(batch_dir, 2)
    review = batch_review.load_review(target)
    assert review["script"] == SCRIPTS["Ay"] and review["approved"] is False and review["terms_edited"] is False
    assert batch_review.audit_current(review) and batch_review.terms_current(review)
    batch_review.save_review(target, review)
    assert json.loads((target / batch_review.REVIEW_FILE).read_text(encoding="utf-8"))["updated_at"]
    assert not list(target.glob(".*.tmp"))


def test_reading_while_another_thread_replaces_review_json(tmp_path):
    target = tmp_path / "konu"
    batch_review.write_json(target / batch_review.REVIEW_FILE, {"n": 0, "pad": "x" * 20000})
    errors, done = [], threading.Event()

    def writer():
        try:
            for n in range(1, 300):
                batch_review.write_json(target / batch_review.REVIEW_FILE, {"n": n, "pad": "y" * 20000})
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            done.set()

    thread = threading.Thread(target=writer)
    thread.start()
    reads = 0
    while not done.is_set() or reads < 50:
        data = batch_review.read_json(target / batch_review.REVIEW_FILE)
        assert isinstance(data["n"], int) and len(data["pad"]) == 20000
        reads += 1
    thread.join()
    assert errors == [] and batch_review.read_json(target / batch_review.REVIEW_FILE)["n"] == 299
    assert not list(target.glob(".*.tmp"))


def test_read_retries_a_file_held_by_a_writer(tmp_path, monkeypatch):
    path = tmp_path / "review.json"
    path.write_text('{"a": 1}', encoding="utf-8")
    real, attempts = Path.read_text, []

    def flaky(self, *args, **kwargs):
        attempts.append(1)
        if len(attempts) < 3:
            raise PermissionError("kullanımda")
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", flaky)
    assert batch_review.read_json(path) == {"a": 1} and len(attempts) == 3


# ---------------------------------------------------------------- onay kuralları

def test_approve_unchanged_clean_script_without_llm(tmp_path):
    batch_dir = prepare(tmp_path)
    outcome = batch_review.approve(folder(batch_dir, 2))  # services yok: LLM gerekmez
    assert outcome.status == "approved" and batch_review.is_approved(batch_review.load_review(folder(batch_dir, 2)))


def test_changed_script_is_audited_and_terms_regenerated_before_approval(tmp_path):
    batch_dir = prepare(tmp_path)
    services = FakeServices()
    edited = "Ay hep aynı yüzünü gösterir; buna kütleçekim kilidi denir."
    outcome = batch_review.approve(folder(batch_dir, 2), services, script=edited)
    review = outcome.review
    assert outcome.status == "approved" and review["script"] == edited and review["script_edited"]
    assert [call[0] for call in services.calls] == ["audit", "terms"]
    assert review["terms"] == ["regenerated term"] and review["anchors"] == ["regenerated anchor"]
    assert review["approved_sha256"] == batch_review.script_sha256(edited) == review["audit_sha256"]


def test_hand_edited_terms_are_kept_when_the_script_changes(tmp_path):
    batch_dir = prepare(tmp_path)
    services = FakeServices()
    outcome = batch_review.approve(folder(batch_dir, 2), services, script="Ay kilitlidir.",
                                   terms="moon surface\nlunar orbit", anchors="moon")
    assert [call[0] for call in services.calls] == ["audit"]  # terimler yeniden üretilmez
    assert outcome.review["terms"] == ["moon surface", "lunar orbit"] and outcome.review["anchors"] == ["moon"]
    assert outcome.review["terms_edited"] is True
    # Sonraki metin değişikliklerinde de korunur.
    batch_review.unskip(folder(batch_dir, 2))
    review = batch_review.load_review(folder(batch_dir, 2))
    assert review["terms"] == ["moon surface", "lunar orbit"]


def test_flagged_audit_requires_an_explicit_approve_anyway(tmp_path):
    batch_dir = prepare(tmp_path)
    target = folder(batch_dir, 1)
    outcome = batch_review.approve(target)
    assert outcome.status == "needs_force" and "Y 1" in outcome.message
    review = batch_review.load_review(target)
    assert not review["approved"] and review["last_action"]["reason"] == "needs_force"
    outcome = batch_review.approve(target, force=True)
    assert outcome.status == "approved" and outcome.review["approved_despite_flags"] is True


def test_edit_that_still_has_flags_is_saved_with_its_new_audit(tmp_path):
    batch_dir = prepare(tmp_path)
    target, services = folder(batch_dir, 1), FakeServices()
    edited = f"Kara delik. Merkezde {FLAGGED} adlı dev kara delik var."
    assert batch_review.approve(target, services, script=edited).status == "needs_force"
    review = batch_review.load_review(target)
    assert review["script"] == edited and batch_review.audit_current(review)  # yeni denetim kaydedildi
    services.calls.clear()
    assert batch_review.approve(target, services, force=True).status == "approved"
    assert services.calls == []  # metin değişmedi: yeniden denetim yok


def test_fixed_script_passes_the_new_audit(tmp_path):
    batch_dir = prepare(tmp_path)
    fixed = SCRIPTS["Kara delikler"].replace(FLAGGED, f"{FLAGGED}*")
    outcome = batch_review.approve(folder(batch_dir, 1), FakeServices(), script=fixed)
    assert outcome.status == "approved" and not outcome.review["approved_despite_flags"]


def test_failed_audit_also_needs_approve_anyway(tmp_path):
    batch_dir = prepare(tmp_path)

    class Failing(FakeServices):
        def audit(self, subject, script):
            return {"durum": "yapılamadı", "hata": "zaman aşımı", "iddialar": [], "sayim": {}, "isaretli": []}

    assert batch_review.approve(folder(batch_dir, 2), Failing(), script="Değişti.").status == "needs_force"


def test_changed_script_without_services_cannot_be_approved(tmp_path):
    batch_dir = prepare(tmp_path)
    with pytest.raises(batch_review.ReviewError) as error:
        batch_review.approve(folder(batch_dir, 2), script="Başka bir metin.")
    assert error.value.reason == "needs_llm"
    assert batch_review.needs_llm(batch_review.load_review(folder(batch_dir, 2)), "Başka bir metin.")
    assert not batch_review.needs_llm(batch_review.load_review(folder(batch_dir, 2)), SCRIPTS["Ay"])


def test_approval_is_invalid_once_the_script_changes():
    review = batch_review.initial_review({"script": "Bir.", "terms": ["a"]})
    review.update(approved=True, approved_sha256=batch_review.script_sha256("Bir."))
    assert batch_review.is_approved(review)
    review["script"] = "İki."
    assert not batch_review.is_approved(review) and batch_review.approved_copy(review) is None


def test_edits_are_normalized_like_llm_output():
    assert batch_review.normalize_script("Way’in a‑b c \r\n") == "Way'in a-b c"
    assert batch_review.parse_list("black hole, Black Hole\nnebula,, ") == ["black hole", "nebula"]


def test_reaudit_checks_without_approving(tmp_path):
    batch_dir = prepare(tmp_path)
    services = FakeServices()
    outcome = batch_review.reaudit(folder(batch_dir, 1), services,
                                   script=SCRIPTS["Kara delikler"].replace(FLAGGED, f"{FLAGGED}*"))
    assert outcome.status == "audited" and not outcome.review["approved"]
    assert [call[0] for call in services.calls] == ["audit", "terms"]
    assert batch_review.is_clean(outcome.review)


def test_regenerate_keeps_the_previous_script_in_history(tmp_path):
    batch_dir = prepare(tmp_path)
    target, services = folder(batch_dir, 1), FakeServices()
    batch_review.approve(target, services, terms="hand term")  # terim düzenlendi, onay Y yüzünden reddedildi
    outcome = batch_review.regenerate(target, services, instruction="  Sagittarius A* adını doğru yaz ")
    review = outcome.review
    assert outcome.status == "regenerated" and review["script"] == services.generated
    assert review["terms"] == ["new term"] and review["terms_edited"] is False and not review["approved"]
    assert batch_review.is_clean(review)
    [entry] = review["history"]
    assert entry["instruction"] == "Sagittarius A* adını doğru yaz" and entry["script"] == SCRIPTS["Kara delikler"]
    assert entry["terms"] == ["hand term"] and entry["audit"]["sayim"]["Y"] == 1
    assert ("generate", "Sagittarius A* adını doğru yaz") in services.calls


def test_skip_and_unskip(tmp_path):
    batch_dir = prepare(tmp_path)
    target = folder(batch_dir, 3)
    batch_review.skip(target)
    with pytest.raises(batch_review.ReviewError, match="atlandı"):
        batch_review.approve(target)
    batch_review.unskip(target)
    batch_review.approve(target)
    with pytest.raises(batch_review.ReviewError) as error:
        batch_review.skip(target)
    assert error.value.reason == "approved"


def test_approve_clean_matches_only_current_clean_audits(tmp_path):
    batch_dir = prepare(tmp_path, subjects=("Kara delikler", "Ay", "Nötron yıldızları"))
    state = bp.BatchState.load(batch_dir)
    # 2: düzenlenmiş ama yeniden denetlenmemiş metin (eski denetim) — kaydedilmiş bir düzenleme
    review = batch_review.load_review(folder(batch_dir, 2))
    review["script"] = "Ay kilitli."
    batch_review.save_review(folder(batch_dir, 2), review)
    approved = batch_review.approve_clean(batch_dir, state)
    assert approved == [3]  # 1: Y var, 2: denetim eski
    assert batch_review.is_approved(batch_review.load_review(folder(batch_dir, 3)))


def test_approve_clean_skips_busy_excluded_and_stale_term_items(tmp_path):
    batch_dir = prepare(tmp_path, subjects=("Ay", "Nötron yıldızları"))
    state = bp.BatchState.load(batch_dir)
    assert batch_review.mark_busy(folder(batch_dir, 1), "approve", os.getpid())
    assert batch_review.approve_clean(batch_dir, state, exclude={2}) == []
    batch_review.release_busy(folder(batch_dir, 1))
    review = batch_review.load_review(folder(batch_dir, 2))
    review["terms_sha256"] = "eski"
    batch_review.save_review(folder(batch_dir, 2), review)
    assert batch_review.approve_clean(batch_dir, state) == [1]


# ---------------------------------------------------------------- render aşaması

def test_render_uses_the_approved_script_and_terms_without_llm(tmp_path):
    batch_dir = prepare(tmp_path)
    services = FakeServices()
    edited = SCRIPTS["Kara delikler"].replace(FLAGGED, f"{FLAGGED}*")
    batch_review.approve(folder(batch_dir, 1), services, script=edited)
    batch_review.approve(folder(batch_dir, 2), terms="moon\nlunar surface")
    runner, packages = FakeRunner(), []
    with patch.object(batch_review, "prepare_item", side_effect=AssertionError("hazırlık çağrılmamalı")), \
            patch.object(batch_package, "audit_script", side_effect=AssertionError("yeniden denetim yok")):
        assert render(batch_dir, runner, packages) == 0
    first, second = runner.argvs
    assert f"--video-script={edited}" in first and "--video-terms=regenerated term" in first
    assert "--video-anchors=regenerated anchor" in first and "--video-script-prompt" not in first
    assert "--video-terms=moon, lunar surface" in second
    assert [subject for subject, _ in packages] == ["Kara delikler", "Ay"]
    assert packages[0][1]["audit"] == CLEAN_AUDIT and packages[0][1]["script_edited"] is True
    state = bp.BatchState.load(batch_dir)
    assert [item.status for item in state.items] == [bp.DONE, bp.DONE, bp.AWAITING_REVIEW] and not state.finished
    # Üçüncü sonradan onaylanır ve ikinci render çağrısında alınır.
    batch_review.approve(folder(batch_dir, 3))
    runner = FakeRunner()
    assert render(batch_dir, runner) == 0
    assert len(runner.argvs) == 1 and SCRIPTS["Nötron yıldızları"] in runner.argvs[0][-3]
    assert bp.BatchState.load(batch_dir).finished


def test_approvals_during_a_render_wait_for_the_next_call(tmp_path):
    batch_dir = prepare(tmp_path)
    batch_review.approve(folder(batch_dir, 2))
    runner = FakeRunner(on_first=lambda: batch_review.approve(folder(batch_dir, 3)))
    assert render(batch_dir, runner) == 0
    assert len(runner.argvs) == 1  # 3 bu turda alınmadı
    assert [item.status for item in bp.BatchState.load(batch_dir).items] == [
        bp.AWAITING_REVIEW, bp.DONE, bp.AWAITING_REVIEW]
    assert batch_review.effective_status(bp.BatchState.load(batch_dir).items[2],
                                         batch_review.load_review(folder(batch_dir, 3))) == bp.APPROVED
    runner = FakeRunner()
    assert render(batch_dir, runner) == 0 and len(runner.argvs) == 1


def test_interrupted_render_resumes_and_skipped_items_finish_the_batch(tmp_path):
    batch_dir = prepare(tmp_path)
    batch_review.approve(folder(batch_dir, 1), force=True)
    batch_review.approve(folder(batch_dir, 2))
    batch_review.skip(folder(batch_dir, 3))
    assert render(batch_dir, FakeRunner(interrupt=True)) == 130
    state = bp.BatchState.load(batch_dir)
    assert [item.status for item in state.items] == [bp.APPROVED, bp.APPROVED, bp.SKIPPED]
    packages = []
    assert render(batch_dir, FakeRunner(), packages) == 0
    state = bp.BatchState.load(batch_dir)
    assert [item.status for item in state.items] == [bp.DONE, bp.DONE, bp.SKIPPED] and state.finished
    assert packages[0][1]["approved_despite_flags"] is True


def test_render_refuses_a_single_phase_batch_and_a_changed_preset(tmp_path):
    batch_dir = prepare(tmp_path, review=False, task_runner=FakeRunner(),
                        packager=lambda **kwargs: {"kontrol_gerekli": False})
    with pytest.raises(bp.BatchInputError, match="iki aşamalı"):
        render(batch_dir)
    (tmp_path / "b").mkdir()
    other = prepare(tmp_path / "b")
    data = json.loads((other / bp.STATE_FILE).read_text(encoding="utf-8"))
    data["preset_sha256"] = "farklı"
    (other / bp.STATE_FILE).write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(bp.PresetError, match="değişmiş"):
        render(other)


def test_render_task_uses_the_approved_text_and_never_asks_the_llm(tmp_path):
    """Gerçek görev yolu: onaylı metin ve terimler parametre olur, generate_script/terms LLM'e gitmez."""
    from app.services import llm
    from app.services import task as tm

    preset = bp.load_preset(PRESET)
    item = bp.ItemState(index=1, subject="Kara delikler")
    approved = {"script": "Onaylı metin, düzeltilmiş: Sagittarius A*.", "terms": ["black hole", "event horizon"],
                "anchors": ["space"]}
    cli = bp._cli_module()
    params = cli.build_video_params(bp._parse_cli(bp.build_render_argv(preset, item, approved)))
    with patch.object(llm, "generate_script", side_effect=AssertionError("LLM metin")), \
            patch.object(llm, "generate_visual_terms", side_effect=AssertionError("LLM terim")), \
            patch.object(llm, "generate_terms", side_effect=AssertionError("LLM terim")):
        assert tm.generate_script("t", params) == approved["script"]
        assert tm.generate_terms("t", params, approved["script"]) == approved["terms"]
    assert tm._video_anchor_list(params.video_anchors) == ["space"]


def test_package_uses_the_review_audit(tmp_path, monkeypatch):
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    (task_dir / "script.json").write_text(json.dumps({"script": "Metin.", "search_terms": ["a"], "params": {}}),
                                          encoding="utf-8")
    monkeypatch.setattr(batch_package.utils, "task_dir", lambda sub="": str(task_dir))
    for name, value in (("_place_video", "kopya"), ("social_texts", {}), ("social_markdown", ""),
                        ("choose_cover", (None, {})), ("contact_sheet", [])):
        monkeypatch.setattr(batch_package, name, lambda *a, value=value, **k: value)
    monkeypatch.setattr(batch_package.batch_subtitles, "write_subtitles",
                        lambda *a, **k: {"dosyalar": [], "uyarilar": []})
    monkeypatch.setattr(batch_package, "_media_duration", lambda path: 40.0)
    monkeypatch.setattr(batch_package, "technical_checks", lambda *a, **k: {"sure_sn": 40.0, "kelime": 1})
    monkeypatch.setattr(batch_package, "audit_script", lambda *a, **k: pytest.fail("denetim yeniden yapılmamalı"))
    review = {"audit": flagged_audit(), "approved_at": "2026-11-09T12:00:00", "approved_despite_flags": True,
              "script_edited": True, "terms_edited": False, "regenerations": 1}
    item = bp.ItemState(index=1, subject="Kara delikler", task_id="t")
    summary = batch_package.build_package(item, {"videos": [str(tmp_path / "v.mp4")], "script": "Metin."},
                                          bp.load_preset(PRESET), tmp_path / "paket", [], ask=None, review=review)
    quality = json.loads((tmp_path / "paket" / "kalite.json").read_text(encoding="utf-8"))
    assert quality["dogruluk"] == review["audit"] and quality["inceleme"]["regenerations"] == 1
    assert summary["kontrol_gerekli"] and "Y/X ile yine de onaylandı" in quality["nedenler"]


# ---------------------------------------------------------------- görünüm

def test_effective_status_combines_state_and_review():
    item = bp.ItemState(index=1, subject="x", prepared=True)
    review = batch_review.initial_review({"script": "Bir.", "terms": ["a"]})
    item.status = bp.AWAITING_REVIEW
    assert batch_review.effective_status(item, review) == bp.AWAITING_REVIEW
    assert batch_review.effective_status(item, {**review, "skipped": True}) == bp.SKIPPED
    approved = {**review, "approved": True, "approved_sha256": batch_review.script_sha256("Bir.")}
    assert batch_review.effective_status(item, approved) == bp.APPROVED
    item.status = bp.RENDERING
    assert batch_review.effective_status(item, approved, running=True) == bp.RENDERING
    assert batch_review.effective_status(item, approved, running=False) == bp.APPROVED
    item.status = bp.PREPARING
    assert batch_review.effective_status(item, None, running=False) == bp.PENDING
    item.status, item.quality = bp.DONE, {"kontrol_gerekli": True}
    assert batch_review.effective_status(item, approved) == "review"


def test_batch_status_and_history_flags(tmp_path):
    batch_dir = prepare(tmp_path)
    batch_review.approve(folder(batch_dir, 2))
    batch_review.skip(folder(batch_dir, 3))
    status = batch_runner.batch_status(batch_dir, lock={})
    assert [item.status for item in status.items] == [bp.AWAITING_REVIEW, bp.APPROVED, bp.SKIPPED]
    assert status.review and status.awaiting_review and not status.unfinished
    assert status.items[0].note == "D 1 B 0 Y 1 X 0"
    view = batch_review.overview(batch_dir)
    assert (view.count(bp.AWAITING_REVIEW), view.approved_waiting, view.flagged, view.clean) == (1, 1, 1, 0)


def test_estimate_uses_the_measured_turkish_edge_rate():
    info = batch_review.estimate("a" * 500 + " b", voice_rate=1.0)
    assert info == {"kelime": 2, "harf": 501, "tahmini_sn": round(0.067 * 501 + 7.7, 1)}
    assert batch_review.estimate("a" * 500, voice_rate=2.0)["tahmini_sn"] == round((0.067 * 500 + 7.7) / 2, 1)


# ---------------------------------------------------------------- işlem kilidi

def test_busy_marker_of_a_live_process_blocks_and_a_dead_one_is_removed(tmp_path):
    target = tmp_path / "konu"
    target.mkdir()
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert batch_review.mark_busy(target, "regenerate", child.pid)
        assert batch_review.busy(target)["action"] == "regenerate"
        with pytest.raises(batch_review.ReviewBusy):
            batch_review.acquire_busy(target, "approve")
    finally:
        child.kill()
        child.wait()
    assert batch_review.busy(target) is None and not (target / batch_review.BUSY_FILE).exists()
    batch_review.acquire_busy(target, "approve")
    assert batch_review.busy(target)["pid"] == os.getpid()
    batch_review.release_busy(target)
    assert batch_review.busy(target) is None


# ---------------------------------------------------------------- komutlar

@pytest.fixture
def fake_pipeline(monkeypatch):
    services = FakeServices()
    monkeypatch.setattr(batch_review, "PipelineServices", lambda preset, item: services)
    return services


def run_cli(*argv):
    return bp.main(list(argv))


def test_cli_status_lists_words_estimate_and_flags(tmp_path, capsys):
    batch_dir = prepare(tmp_path)
    assert run_cli("durum", str(batch_dir)) == 0
    output = capsys.readouterr().out
    assert "3 onay bekliyor (1 Y/X)" in output and "denetim D 1 B 0 Y 1 X 0" in output
    assert f"Y: Merkezdeki kara delik {FLAGGED}." in output and "(hedef 90-100)" in output
    assert run_cli("durum", batch_dir.name) == 2  # storage/batches altında değil


def test_cli_approve_needs_anyway_and_accepts_an_edited_script(tmp_path, capsys, fake_pipeline):
    batch_dir = prepare(tmp_path)
    assert run_cli("onayla", str(batch_dir), "1") == 4
    assert "--yine-de gerekiyor" in capsys.readouterr().out
    fixed = tmp_path / "metin.txt"
    fixed.write_text(SCRIPTS["Kara delikler"].replace(FLAGGED, f"{FLAGGED}*"), encoding="utf-8")
    assert run_cli("onayla", str(batch_dir), "1", "--metin-dosyasi", str(fixed)) == 0
    assert batch_review.is_approved(batch_review.load_review(folder(batch_dir, 1)))
    assert [call[0] for call in fake_pipeline.calls] == ["audit", "terms"]
    assert run_cli("onayla", str(batch_dir), "1") == 2  # zaten onaylı
    assert run_cli("onayla", str(batch_dir), "9") == 2
    assert not (folder(batch_dir, 1) / batch_review.BUSY_FILE).exists()


def test_cli_regenerate_reaudit_skip_and_approve_clean(tmp_path, capsys, fake_pipeline):
    batch_dir = prepare(tmp_path)
    assert run_cli("yeniden-uret", str(batch_dir), "1", "--talimat", "Adı doğru yaz") == 0
    assert batch_review.load_review(folder(batch_dir, 1))["history"][0]["instruction"] == "Adı doğru yaz"
    text = tmp_path / "ay.txt"
    text.write_text("Ay kilitlidir.", encoding="utf-8")
    assert run_cli("yeniden-denetle", str(batch_dir), "2", "--metin-dosyasi", str(text), "--terimler", "moon") == 0
    review = batch_review.load_review(folder(batch_dir, 2))
    assert review["script"] == "Ay kilitlidir." and not review["approved"] and review["terms"] == ["moon"]
    assert run_cli("atla", str(batch_dir), "3") == 0
    assert run_cli("temizleri-onayla", str(batch_dir)) == 0
    assert "onaylandı: 1, 2" in capsys.readouterr().out
    assert run_cli("atla", str(batch_dir), "3", "--geri-al") == 0
    assert batch_review.load_review(folder(batch_dir, 3))["skipped"] is False


def test_cli_records_an_llm_failure_for_the_webui(tmp_path, fake_pipeline):
    batch_dir = prepare(tmp_path)
    fake_pipeline.generate = lambda subject, instruction="": (_ for _ in ()).throw(RuntimeError("kota doldu"))
    assert run_cli("yeniden-uret", str(batch_dir), "2") == 1
    last = batch_review.load_review(folder(batch_dir, 2))["last_action"]
    assert (last["action"], last["ok"], last["reason"]) == ("regenerate", False, "failed")
    assert "kota doldu" in last["message"]


def test_cli_render_runs_the_render_phase(tmp_path, monkeypatch):
    batch_dir = prepare(tmp_path)
    batch_review.approve(folder(batch_dir, 2))
    called = {}

    def fake_render(path, **kwargs):
        called.update(path=path, **kwargs)
        return 0

    monkeypatch.setattr(bp, "render_batch", fake_render)
    assert run_cli("render", str(batch_dir), "--gunluk-stderr") == 0
    assert called == {"path": batch_dir, "hold_lock": True, "log_file": False}


def test_cli_rejects_a_single_phase_batch(tmp_path):
    batch_dir = prepare(tmp_path, review=False, task_runner=FakeRunner(),
                        packager=lambda **kwargs: {"kontrol_gerekli": False})
    assert run_cli("onayla", str(batch_dir), "1") == 2


def test_legacy_command_takes_the_flow_flags(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(bp, "run_batch", lambda *args, **kwargs: seen.append(kwargs) or 0)
    source = write_input(tmp_path)
    assert run_cli(str(source), "--preset", str(PRESET)) == 0
    assert run_cli(str(source), "--preset", str(PRESET), "--inceleme-yok") == 0
    assert run_cli(str(source), "--preset", str(PRESET), "--inceleme") == 0
    assert "review" not in seen[0] and seen[1]["review"] is False and seen[2]["review"] is True


def test_runner_commands(tmp_path):
    assert batch_runner.batch_command(tmp_path / "k.csv", PRESET, tmp_path / "b", review=True)[-1] == "--inceleme"
    assert batch_runner.batch_command(tmp_path / "k.csv", PRESET, tmp_path / "b", review=False)[-1] == "--inceleme-yok"
    assert "--inceleme" not in " ".join(batch_runner.batch_command(tmp_path / "k.csv", PRESET, tmp_path / "b"))
    assert batch_runner.render_command(tmp_path / "b")[-3:] == ["render", str((tmp_path / "b").resolve()),
                                                                "--gunluk-stderr"]
    assert batch_runner.review_action_command(tmp_path / "b", 2, "onayla", ["--yine-de"])[-4:] == [
        "onayla", str((tmp_path / "b").resolve()), "2", "--yine-de"]


def test_resume_chooses_the_phase(tmp_path, monkeypatch):
    batch_dir = prepare(tmp_path)
    started = []
    monkeypatch.setattr(batch_runner, "start_render", lambda path: started.append(("render", path)) or 1)
    monkeypatch.setattr(batch_runner, "start_batch", lambda *args, **kwargs: started.append(("prepare",)) or 1)
    with pytest.raises(batch_runner.ResumeError) as error:
        batch_runner.resume_batch(batch_dir)  # onay bekliyor: devam edecek iş yok
    assert error.value.reason == "awaiting_review"
    data = json.loads((batch_dir / bp.STATE_FILE).read_text(encoding="utf-8"))
    data["items"][0]["status"] = bp.RENDERING
    (batch_dir / bp.STATE_FILE).write_text(json.dumps(data), encoding="utf-8")
    batch_runner.resume_batch(batch_dir)
    data["items"][1].update(status=bp.PENDING, prepared=False)
    (batch_dir / bp.STATE_FILE).write_text(json.dumps(data), encoding="utf-8")
    batch_runner.resume_batch(batch_dir)
    assert started == [("render", batch_dir), ("prepare",)]


def test_review_action_subprocess_marks_the_subject_busy(tmp_path, monkeypatch):
    batch_dir = prepare(tmp_path)
    target = folder(batch_dir, 2)
    spawned, real_popen = [], subprocess.Popen

    class FakeProcess:
        def __init__(self, command, **kwargs):
            spawned.append((command, kwargs))
            self.child = real_popen([sys.executable, "-c", "import time; time.sleep(20)"])
            self.pid = self.child.pid

    monkeypatch.setattr(batch_runner.subprocess, "Popen", FakeProcess)
    pid = batch_runner.start_review_action(batch_dir, target, 2, "yeniden-uret", ["--talimat=kısa tut"])
    try:
        command, kwargs = spawned[0]
        assert command[-4:] == ["yeniden-uret", str(batch_dir.resolve()), "2", "--talimat=kısa tut"]
        assert kwargs["stdin"] is subprocess.DEVNULL and Path(kwargs["stdout"].name).name == batch_review.ACTION_LOG
        assert batch_review.busy(target)["pid"] == pid
        with pytest.raises(batch_review.ReviewBusy):
            batch_runner.start_review_action(batch_dir, target, 2, "onayla")
    finally:
        batch_runner._children.pop(pid).child.kill()
    time.sleep(0.2)
