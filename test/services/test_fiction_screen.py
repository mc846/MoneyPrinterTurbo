"""Kurgu klip taraması (fiction_screen): yanıt ve karar, maliyet, kareler ve önbellek, yedek model, sahne kaydı,
"yine de kullan" ve render kapısı, klipler komutu, görev kaydı ve kalite raporu, revizyon klibi, reçete.
Sahte model (conftest gerçek arka ucu kapatır); klipler ffmpeg ile üretilir. Ağ yok, config.toml'a yazılmaz."""

import json
import re
from pathlib import Path

import pytest

from app.services import batch_package, batch_review, fiction, fiction_screen, scene_edits, task_materials, vision_screen
from app.config import config
from app.services import batch_production as bp
from test.services.test_fiction import PRESET, _folder, _render_plan, fiction_preparer, make_clip

FLAG = {"flag": True, "emblem": False, "patch": True, "logo": False, "readable_text": False,
        "recognizable_face": False, "details": "US flag on the left shoulder", "matches_prompt": False,
        "deviation": "wheeled rover instead of the tracked excavator"}
CLEAN = {**{key: False for key in fiction_screen.FINDINGS}, "details": "", "matches_prompt": True, "deviation": ""}


class FakeModel:
    """Sahne numarasına göre yanıt verir: ``answers`` {sahne: yanıt sözlüğü}; ``down`` kullanılamayan modeller."""

    def __init__(self, answers=None, down=(), usage=None):
        self.answers, self.down, self.calls = answers or {}, set(down), []
        self.usage = usage if usage is not None else {"input": 4000, "output": 80, "thinking": 20}

    def __call__(self, images, prompt, backend, model):
        self.calls.append({"backend": backend, "model": model, "images": len(images), "prompt": prompt})
        if f"{backend}:{model}" in self.down:
            raise vision_screen.VisionUnavailable(f"{backend} down")
        index = int(re.search(r"scene (\d+)", prompt).group(1))
        return json.dumps(self.answers.get(index, CLEAN)), dict(self.usage)


@pytest.fixture
def fake(monkeypatch):
    model = FakeModel({2: FLAG})
    monkeypatch.setattr(fiction_screen, "_ask_backend", model)
    return model


@pytest.fixture
def kurgu(tmp_path, monkeypatch):
    """Hazırlanmış (sahte) bir kurgu toplu işi (test_fiction'daki fiction_batch fikstürünün kopyası)."""
    from unittest.mock import patch

    folder = tmp_path / "batches"
    monkeypatch.setattr(bp, "batches_dir", lambda: folder)
    source = tmp_path / "konular.txt"
    source.write_text("Ya olsaydı: Ay'da helyum-3 madenciliği yapılsaydı\n", encoding="utf-8")
    with patch.dict(config.app, {}):
        assert bp.run_batch(source, PRESET, output=folder / "kurgu", preparer=fiction_preparer,
                            reporter=lambda state, batch_dir: None) == 0
        yield folder / "kurgu"


# ---------------------------------------------------------------- yanıt, karar, maliyet


def test_parse_and_decide():
    assert fiction_screen.parse("```json\n" + json.dumps(FLAG) + "\n```")["flag"] is True
    assert fiction_screen.parse('{"flag": "yes"}') is None and fiction_screen.parse("hayır") is None
    status, findings, warnings = fiction_screen.decide(fiction_screen.parse(json.dumps(FLAG)))
    assert status == fiction.SCREEN_CHECK and findings == [{"code": "flag", "text": "bayrak: US flag on the left shoulder"}]
    assert warnings == ["yama: US flag on the left shoulder",
                        "istemden sapma: wheeled rover instead of the tracked excavator"]
    assert fiction_screen.decide({**CLEAN, "patch": True})[0] == fiction.SCREEN_WARN  # a patch alone is a warning
    assert fiction_screen.decide({**CLEAN, "matches_prompt": False})[0] == fiction.SCREEN_WARN
    for code in ("emblem", "logo", "readable_text", "recognizable_face"):
        assert fiction_screen.decide({**CLEAN, code: True})[0] == fiction.SCREEN_CHECK
    assert fiction_screen.decide(CLEAN) == (fiction.SCREEN_CLEAN, [], [])


def test_cost_and_prompt():
    usage = {"input": 4000, "output": 80, "thinking": 20}
    assert fiction_screen.cost_usd("gemini:gemini-3.5-flash-lite", usage) == pytest.approx(0.00145)
    assert fiction_screen.cost_usd("claude:sonnet", usage) is None  # subscription
    assert fiction_screen.cost_usd("gemini:unknown-model", usage) is None
    text = fiction_screen.build_prompt(6, 7, "Scene 6 of 7: an astronaut walks past the crawler.")
    assert "scene 6 of 7" in text and "an astronaut walks past the crawler" in text and "matches_prompt" in text
    assert "scene 6 of a vertical" in fiction_screen.build_prompt(6, 0, "x")
    assert fiction_screen.frame_times(8.04) == [0.5, 4.02, 7.54]


# ---------------------------------------------------------------- klip: kareler, önbellek, yedek


def test_a_clip_is_screened_once_with_three_frames(tmp_path, fake):
    clip = make_clip(tmp_path / "c.mp4", size="720x1280")
    record = fiction_screen.screen_clip(clip, "Scene 2 of 3: a rover.", 2, 3, fiction.Settings())
    assert record["status"] == fiction.SCREEN_CHECK and record["model"] == "gemini:gemini-3.5-flash-lite"
    assert fake.calls[0]["images"] == 3 and "Scene 2 of 3: a rover." in fake.calls[0]["prompt"]
    assert record["usage"] == {"input": 4000, "output": 80, "thinking": 20} and record["cost_usd"] > 0
    assert record["frames"] == [0.5, 4.0, 7.5] and record["cached"] is False
    again = fiction_screen.screen_clip(clip, "Scene 2 of 3: a rover.", 2, 3, fiction.Settings())
    assert again["cached"] is True and len(fake.calls) == 1  # the same clip and prompt: from the cache
    fiction_screen.screen_clip(clip, "Scene 2 of 3: another prompt.", 2, 3, fiction.Settings())
    fiction_screen.screen_clip(clip, "Scene 2 of 3: a rover.", 2, 3, fiction.Settings(), force=True)
    assert len(fake.calls) == 3


def test_the_fallback_model_and_an_unscreened_clip(tmp_path, monkeypatch):
    clip = make_clip(tmp_path / "c.mp4")
    model = FakeModel(down={"gemini:gemini-3.5-flash-lite"})
    monkeypatch.setattr(fiction_screen, "_ask_backend", model)
    record = fiction_screen.screen_clip(clip, "Scene 1 of 2: x.", 1, 2, fiction.Settings())
    assert record["status"] == fiction.SCREEN_CLEAN and record["model"] == "claude:sonnet"
    assert "gemini down" in record["fallback_from"] and record["cost_usd"] is None
    model.down.add("claude:sonnet")
    other = make_clip(tmp_path / "d.mp4", color="red")
    record = fiction_screen.screen_clip(other, "Scene 1 of 2: x.", 1, 2, fiction.Settings())
    assert record["status"] == fiction.SCREEN_UNSCREENED and "claude down" in record["error"]
    assert fiction_screen.cached(record["sha256"], "Scene 1 of 2: x.") is None  # a failure is not cached
    assert fiction.screening_text(record).startswith("taranamadı: ")


# ---------------------------------------------------------------- konu: kayıt, onay, render kapısı


def _approved(kurgu):
    folder = _folder(kurgu)
    batch_review.approve(folder)
    return folder, batch_review.load_review(folder)["scene_plan"]


def test_a_flag_blocks_the_render_until_the_user_uses_the_clip_anyway(kurgu, fake):
    folder, plan = _approved(kurgu)
    settings = bp.load_preset(PRESET).fiction
    assert settings.screening and settings.screening_fallback == "claude:sonnet"
    for scene in plan["scenes"]:
        fiction.accept_upload(folder, plan, scene["index"], "x.mp4", make_clip(folder / f"u{scene['index']}.mp4").read_bytes())
    done = fiction_screen.screen_pending(folder, plan, settings)
    assert [index for index, _ in done] == [scene["index"] for scene in plan["scenes"]]
    assert fiction.clips_complete(folder, plan) and fiction.screening_blockers(folder, plan) == [2]
    assert not fiction.render_ready(folder, plan)
    assert batch_review.overview(kurgu).items[0].status == batch_review.AWAITING_CLIPS
    page = (folder / fiction.PACKAGE_FILE).read_text(encoding="utf-8")
    assert "Tarama: kontrol gerekli: bayrak: US flag on the left shoulder" in page
    with pytest.raises(fiction.FictionError, match="kontrol gerekli: sahne 02"):
        fiction.write_render_plan(folder, plan, "konu", settings=settings)
    with pytest.raises(fiction.FictionError, match="bulgu yok"):
        fiction.use_anyway(folder, plan, 1)
    fiction.use_anyway(folder, plan, 2)
    assert fiction.render_ready(folder, plan)
    render = json.loads(fiction.write_render_plan(folder, plan, "konu", settings=settings).read_text(encoding="utf-8"))
    flagged = render["scenes"][1]["screening"]
    assert flagged["status"] == fiction.SCREEN_CHECK and flagged["use_anyway"] is True
    assert flagged["findings"] == ["bayrak: US flag on the left shoulder"]
    assert render["scenes"][0]["screening"]["status"] == fiction.SCREEN_CLEAN
    # Screening already done: nothing is asked again; a new clip drops the approval and is screened again.
    calls = len(fake.calls)
    assert fiction_screen.screen_pending(folder, plan, settings) and len(fake.calls) == calls
    fiction.accept_upload(folder, plan, 2, "y.mp4", make_clip(folder / "new.mp4", color="white").read_bytes())
    assert fiction.screening_blockers(folder, plan) == []  # not screened yet
    fiction_screen.screen_pending(folder, plan, settings)
    assert fiction.screening_blockers(folder, plan) == [2] and len(fake.calls) == calls + 1
    fiction.use_anyway(folder, plan, 2)
    fiction.use_anyway(folder, plan, 2, revoke=True)
    assert fiction.screening_blockers(folder, plan) == [2]
    # Screening switched off in the preset: no record in the render plan.
    off = fiction.Settings(screening=False)
    fake.answers = {}
    fiction_screen.screen_pending(folder, plan, settings, force=True, indexes=[2])
    assert "screening" not in json.loads(fiction.write_render_plan(folder, plan, "konu", settings=off)
                                         .read_text(encoding="utf-8"))["scenes"][0]


def test_the_clips_command_screens_and_uses_anyway(kurgu, fake, capsys):
    folder, plan = _approved(kurgu)
    for scene in plan["scenes"]:
        make_clip(fiction.inbox(folder) / scene["file"])
    capsys.readouterr()
    assert batch_review.cli_main(["klipler", str(kurgu), "1", "--kontrol"]) == 0
    out = capsys.readouterr().out
    assert "sahne 01: geldi" in out and "tarama: temiz" in out
    assert "sahne 02: geldi" in out and "tarama: kontrol gerekli: bayrak: US flag on the left shoulder" in out
    assert f"tarama: {len(plan['scenes'])} çağrı, ~" in out and "kontrol gerekli: sahne 02" in out
    assert batch_review.cli_main(["klipler", str(kurgu), "1", "--yine-de-kullan", "2"]) == 0
    out = capsys.readouterr().out
    assert "yine de kullanılacak" in out and "render'a hazır" in out and "çağrı" not in out
    assert batch_review.cli_main(["klipler", str(kurgu), "1", "--yine-de-kullan", "1"]) == 2
    assert batch_review.cli_main(["klipler", str(kurgu), "1", "--yeniden-tara", "1"]) == 0
    assert "tarama: 1 çağrı" in capsys.readouterr().out


def test_the_render_snapshot_screens_unscreened_clips_first(kurgu, fake):
    folder, plan = _approved(kurgu)
    for scene in plan["scenes"]:
        make_clip(fiction.inbox(folder) / scene["file"])
    fiction.check_inbox(folder, plan)  # dropped into gelen/ and checked without screening
    state = bp.BatchState.load(kurgu)
    snapshot = bp._render_snapshot(state, kurgu, bp.load_preset(PRESET))
    assert snapshot == {} and fiction.screening_blockers(folder, plan) == [2]  # screened, then held back
    fiction.use_anyway(folder, plan, 2)
    snapshot = bp._render_snapshot(state, kurgu, bp.load_preset(PRESET))
    assert Path(snapshot[1]["ai_manual_plan"]).is_file()


# ---------------------------------------------------------------- görev kaydı, kalite raporu, revizyon


def test_the_task_record_and_the_quality_report(tmp_path, monkeypatch):
    monkeypatch.setattr(fiction.utils, "task_dir", lambda task_id: str(tmp_path / "task"))
    persisted = {}
    from app.services import material

    monkeypatch.setattr(material, "_persist_material_sources", lambda task_id, records: persisted.update(r=records))
    path = _render_plan(tmp_path, [8, 8, 8])
    data = json.loads(path.read_text(encoding="utf-8"))
    data["scenes"][0]["screening"] = {"status": "temiz", "model": "gemini:m", "findings": [], "warnings": [],
                                      "error": "", "use_anyway": False}
    data["scenes"][1]["screening"] = {"status": "kontrol", "model": "gemini:m", "findings": ["bayrak: omuzda"],
                                      "warnings": ["istemden sapma: tekerlekli araç"], "error": "", "use_anyway": True}
    data["scenes"][2]["screening"] = {"status": "taranamadi", "model": "", "findings": [], "warnings": [],
                                      "error": "gemini down", "use_anyway": False}
    path.write_text(json.dumps(data), encoding="utf-8")
    fiction.task_clips("t1", path, audio_duration=21.0)
    records = persisted["r"]
    assert records[0]["vision"] == {"decision": "use", "model": "gemini:m", "screening": "temiz",
                                    "use_anyway": False, "reasons": []}
    assert records[1]["vision"]["use_anyway"] is True and records[1]["vision"]["reasons"] == [
        "bayrak: omuzda", "istemden sapma: tekerlekli araç"]
    assert records[2]["vision"]["decision"] == "unscreened"
    findings = batch_package.clip_screening(records)
    assert findings == [{"asset_id": "sahne-02", "durum": "kontrol", "bulgular": ["bayrak: omuzda",
                         "istemden sapma: tekerlekli araç"], "yine_de_kullanildi": True, "model": "gemini:m"}]
    checks = {"klip_taramasi": findings, "taranmamis_sahneler": batch_package.unscreened_scenes(records)}
    warnings = batch_package.check_warnings(checks)
    assert any(line.startswith("klip taraması: sahne-02: bayrak: omuzda") and "yine de kullan" in line
               for line in warnings)
    reasons = batch_package.review_reasons({"durum": "tamam"}, checks)
    assert reasons == ["görsel tarama: 1 sahne taranmadan kullanıldı (sahne-03)"]  # an approved flag is listed


def test_a_revision_clip_takes_the_screening_of_the_dialog(tmp_path, fake):
    manifest = task_materials.manifest_path("t2")
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps({"schema_version": 1, "materials": [], "terms": [], "scenes": []}), encoding="utf-8")
    scene = {"id": "v1-s02", "duration": 7.4, "search_term": "n2", "term_index": None}
    previous = {"provider": "ai_manual", "asset_id": "sahne-02", "tool": fiction.TOOL, "prompt": "Scene 2 of 7: x",
                "style_version": "abc", "scene_label": fiction.SCENE_LABEL}
    clip = make_clip(tmp_path / "n.mp4")
    fiction_screen.screen_clip(clip, "Scene 2 of 7: x", 2, 7, fiction.Settings())  # the revision dialog
    added, warnings = scene_edits.add_fiction_clip("t2", "r002", scene, str(clip), previous)
    assert len(fake.calls) == 1  # from the cache
    vision = added["source"]["vision"]
    assert vision["screening"] == "kontrol" and vision["use_anyway"] is True
    assert warnings == ["klip taraması: kontrol gerekli: bayrak: US flag on the left shoulder; "
                        "yama: US flag on the left shoulder; istemden sapma: wheeled rover instead of the tracked "
                        "excavator"]


def test_the_preset_options(tmp_path):
    preset = bp.load_preset(PRESET)
    assert preset.fiction.screening_model == "gemini:gemini-3.5-flash-lite"
    base = PRESET.read_text(encoding="utf-8")
    path = tmp_path / "kurgu-deneme.toml"
    path.write_text(base.replace("clip_screening = true", "clip_screening = false"), encoding="utf-8")
    assert bp.load_preset(path).fiction.screening is False
    path.write_text(base.replace("clip_screening = true", 'clip_screening = "yes"'), encoding="utf-8")
    with pytest.raises(bp.PresetError, match="clip_screening bool olmalı"):
        bp.load_preset(path)
    path.write_text(base.replace("clip_seconds = 8", "clip_seconds = true"), encoding="utf-8")
    with pytest.raises(bp.PresetError, match="clip_seconds int olmalı"):
        bp.load_preset(path)
