"""Kurgu kanalı (fiction.py, ai_manual): reçete, kurgu denetimi, sahne planı ve süre hesabı, stil kitabı ve istem
kuralları, istem paketi, klip yükleme ve gelen/ eşleştirmesi, doğrulama (kısa, yatay, düşük çözünürlük, sesli,
bozuk), render koşulu ve render planı, görevin klipleri, atıflar, revizyonda sahne değiştirme, klipler komutu.
Sahte LLM; klipler ffmpeg ile üretilir. config.toml'a ve gerçek storage/tasks'a yazılmaz."""

import json
import re
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.config import config
from app.services import batch_package, batch_review, fiction, nasa, scene_edits, task_materials, video
from app.services import batch_production as bp
from test.services.test_batch_review import CLEAN_AUDIT, REAL_PIPELINE_SERVICES

ROOT = Path(__file__).resolve().parents[2]
PRESET = ROOT / "presets" / "kurgu.toml"
FF = video.utils.get_ffmpeg_binary()
SCRIPT = ("Ya Ay'da helyum-3 madenciliği yapılsaydı? Helyum-3, Güneş rüzgârının milyarlarca yılda Ay toprağına "
          "bıraktığı hafif bir izotop. Hayal edelim: geniş kraterlerde yavaşça ilerleyen kazıcılar regoliti "
          "ısıtıp gazı ayırıyor olsaydı. Bir ton regolitte yalnızca birkaç miligram bulunur. Füzyon reaktörleri "
          "bunu yakabilseydi, enerji sorunumuz nasıl değişirdi? Ama önce böyle bir reaktörü kurmamız gerekirdi.")


def plan_answer(prompt: str) -> str:
    count = int(re.search(r"narrated over (\d+) clips", prompt).group(1))
    return json.dumps({
        "style_book": {"visual_language": "photoreal cinematic sci-fi", "lighting": "low sun, hard shadows",
                       "color": "grey regolith, black sky", "era": "2040s lunar base",
                       "recurring": [{"name": "mining rover", "description": "six-wheeled white rover"}],
                       "science": "Moon: black sky, hard shadows, no atmosphere"},
        "scenes": [{"index": i, "visual_tr": f"Sahne {i} tarifi", "action": f"shot {i} of the rover",
                    "camera": "slow dolly", "light": "low sun"} for i in range(1, count + 1)],
    })


def make_clip(path: Path, seconds=8.0, size="1080x1920", audio=False, color="blue") -> Path:
    command = [FF, "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"color=c={color}:s={size}:r=24:d={seconds}"]
    if audio:
        command += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}", "-c:a", "aac", "-shortest"]
    command += ["-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)]
    subprocess.run(command, check=True, capture_output=True)
    return path


@pytest.fixture
def plan():
    return fiction.build_plan("Ay'da helyum-3", SCRIPT, 40.0, fiction.Settings(), ask=plan_answer)


# ---------------------------------------------------------------- reçete


def test_the_fiction_preset():
    preset = bp.load_preset(PRESET)
    assert preset.is_fiction and preset.review_before_render
    assert preset.fiction.clip_seconds == 8 and preset.fiction.tool == "Google Flow / Veo"
    assert preset.fiction.scene_label == fiction.SCENE_LABEL
    assert preset.video["video_source"] == "ai_manual" and preset.video["video_aspect"] == "9:16"
    assert preset.video["video_clip_duration"] == 8 and preset.video["video_fit_mode"] == "blur"
    assert preset.app["material_vision_screening"] is False and preset.app["scene_labels_enabled"] is True
    uzay = bp.load_preset(ROOT / "presets" / "uzay.toml")
    for key in ("voice_name", "voice_rate", "font_name", "font_size", "stroke_width", "final_render_engine",
                "subtitle_enabled", "text_fore_color", "stroke_color", "video_language", "llm_provider"):
        assert preset.video[key] == uzay.video[key], key
    assert not uzay.is_fiction and uzay.fiction is None


@pytest.mark.parametrize(("text", "error"), [
    ('[video]\nvideo_source = "nasa"\n[fiction]\nclip_seconds = 8\n', "yalnız video_source"),
    ('[video]\nvideo_source = "ai_manual"\n[script]\ncontent = "kurgu"\n', "iki aşamalı"),
    ('[video]\nvideo_source = "ai_manual"\n[quality]\nreview_before_render = true\n', "content = \"kurgu\""),
    ('[video]\nvideo_source = "ai_manual"\nvideo_clip_duration = 5\n[script]\ncontent = "kurgu"\n'
     '[quality]\nreview_before_render = true\n', "aynı olmalı"),
    ('[script]\ncontent = "roman"\n', "content şunlardan"),
])
def test_preset_rules(tmp_path, text, error):
    path = tmp_path / "p.toml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(bp.PresetError, match=error):
        bp.load_preset(path)


# ---------------------------------------------------------------- denetçi


def test_the_audit_prompt_gains_the_fiction_rule_only_in_fiction_mode():
    plain = batch_package.audit_prompt("Ay", "metin")
    assert plain == batch_package.AUDIT_PROMPT.format(subject="Ay", script="metin")  # uzay/gerçek aynı istem
    fiction_prompt = batch_package.audit_prompt("Ay", "metin", mode="kurgu")
    assert "FICTION" in fiction_prompt and "yapılıyor" in fiction_prompt and "is \"Y\"" in fiction_prompt
    assert fiction_prompt.index("5. This script") < fiction_prompt.index("## Topic")


def test_fiction_presented_as_fact_is_flagged():
    seen = []

    def ask(prompt):
        seen.append(prompt)
        return json.dumps({"claims": [
            {"claim": "Helyum-3 Güneş rüzgârıyla gelir.", "rating": "D", "reason": "Doğru."},
            {"claim": "Ay'da helyum-3 çıkarılıyor.", "rating": "Y", "reason": "Kurgu gerçekmiş gibi sunuluyor."}]})

    audit = batch_package.audit_script("Ay'da helyum-3", "Ay'da helyum-3 çıkarılıyor.", ask=ask, mode="kurgu")
    assert audit["sayim"]["Y"] == 1 and audit["kip"] == "kurgu" and audit["istem_surumu"] == 2
    assert "FICTION" in seen[0]
    assert batch_package.review_reasons(audit, {}) == ["doğruluk: 1 Y (yanıltıcı)"]
    assert batch_review.audit_blocks(audit)


def test_the_services_audit_in_the_presets_mode(monkeypatch):
    calls = []
    presets = [bp.load_preset(PRESET), bp.load_preset(ROOT / "presets" / "uzay.toml")]
    services = [lambda item, preset=preset: REAL_PIPELINE_SERVICES(preset, item) for preset in presets]
    monkeypatch.setattr(batch_review, "_ensure_not_webui", lambda: None)
    monkeypatch.setattr(bp, "apply_app_overrides", lambda preset: None)
    monkeypatch.setattr(bp, "_cli_module", lambda: SimpleNamespace(apply_llm_overrides=lambda args: None))
    monkeypatch.setattr(bp, "_parse_cli", lambda argv: None)
    monkeypatch.setattr(bp, "build_task_argv", lambda preset, item: [])
    monkeypatch.setattr(batch_package, "audit_script", lambda subject, script, **kwargs: calls.append(kwargs) or {})
    item = bp.ItemState(index=1, subject="x")
    for service in services:
        service(item).audit("x", "metin")
    assert calls == [{"mode": "kurgu"}, {}]


# ---------------------------------------------------------------- sahne planı


def test_scene_count_from_the_estimate_and_the_clip_length():
    assert fiction.scene_count(40.0, 8) == 6  # (40 + 3) / 8 → 6
    assert fiction.scene_count(45.0, 8) == 6 and fiction.scene_count(45.1, 8) == 7
    assert fiction.scene_count(0, 8) == 1
    seconds = batch_review.estimate(SCRIPT)["tahmini_sn"]
    assert seconds == pytest.approx(0.067 * len(re.sub(r"\s+", "", SCRIPT)) + 7.7, abs=0.1)


def test_the_narration_is_split_in_order_and_balanced():
    parts = fiction.split_narration(SCRIPT, 6)
    assert len(parts) == 6 and " ".join(parts) == re.sub(r"\s+", " ", SCRIPT)
    assert parts == fiction.sentences(SCRIPT)  # six sentences, six scenes
    # More sentences than scenes: neighbours are grouped, the longest group kept as short as possible.
    parts = fiction.split_narration(SCRIPT, 3)
    assert len(parts) == 3 and " ".join(parts) == re.sub(r"\s+", " ", SCRIPT)
    assert max(map(len, parts)) <= 0.5 * len(SCRIPT)
    # Fewer sentences than scenes: long sentences are split at a comma, then at a word boundary.
    parts = fiction.split_narration("Kısa bir cümle, ikinci bir parça ve üçüncü parça burada.", 3)
    assert len(parts) == 3 and " ".join(parts) == "Kısa bir cümle, ikinci bir parça ve üçüncü parça burada."
    assert fiction.split_narration("Tek.", 4) == ["Tek."]


def test_the_plan_style_book_and_prompt_rules(plan):
    assert plan["scene_count"] == 6 and plan["clip_seconds"] == 8 and plan["tool"] == "Google Flow / Veo"
    assert [s["file"] for s in plan["scenes"]] == [f"sahne-{i:02d}.mp4" for i in range(1, 7)]
    style = fiction.style_text(plan["style_book"])
    assert "mining rover: six-wheeled white rover" in style and "black sky" in style
    for scene in plan["scenes"]:
        prompt = scene["prompt"]
        assert prompt.startswith(style)  # the style book opens every prompt
        assert "Vertical 9:16" in prompt and "8 seconds" in prompt and "one continuous shot" in prompt
        for rule in ("No text", "no logos", "no insignia", "no real people", "no recognizable faces"):
            assert rule in prompt
        assert f"Scene {scene['index']} of 6" in prompt and "Camera: slow dolly" in prompt
    assert plan["style_version"] == fiction.style_version(plan["style_book"])
    assert fiction.plan_current(plan, SCRIPT) and not fiction.plan_current(plan, SCRIPT + " Ek.")


def test_a_long_style_field_is_cut_at_a_sentence_end():
    long_text = " ".join(["Shadows are hard and black with no fill light at all."] * 40)
    style = fiction._clean_style({"visual_language": "x", "science": long_text})
    assert 600 < len(style["science"]) <= 1200 and style["science"].endswith("at all.")


def test_an_unreadable_plan_answer_is_asked_again_then_fails():
    answers = iter(["bozuk", plan_answer("narrated over 6 clips")])
    plan = fiction.build_plan("x", SCRIPT, 40.0, ask=lambda prompt: next(answers))
    assert plan["scene_count"] == 6
    with pytest.raises(fiction.FictionError, match="sahne planı üretilemedi"):
        fiction.build_plan("x", SCRIPT, 40.0, ask=lambda prompt: '{"style_book": {}}')


def test_edited_prompts_and_style_book_survive_a_new_plan(plan):
    edited = fiction.apply_edits(plan, {2: "my own prompt"})
    assert edited["scenes"][1]["prompt_edited"] and not plan["scenes"][1].get("prompt_edited")
    restyled = fiction.apply_edits(edited, style_book={**plan["style_book"], "color": "warm orange dust"})
    assert restyled["style_book_edited"] and restyled["style_version"] != plan["style_version"]
    assert "warm orange dust" in restyled["scenes"][0]["prompt"]  # not edited: recomposed
    assert restyled["scenes"][1]["prompt"] == "my own prompt"  # edited: kept
    again = fiction.build_plan("x", SCRIPT + " Yeni bir cümle.", 41.0, ask=plan_answer,
                               style_book=restyled["style_book"], previous=restyled)
    assert again["scenes"][1]["prompt"] == "my own prompt" and again["scenes"][1]["prompt_edited"]
    assert "warm orange dust" in again["scenes"][0]["prompt"] and again["style_book_edited"]


def test_the_prompt_package(plan, tmp_path):
    paths = fiction.write_package(tmp_path, plan, {"scenes": {"2": {"status": fiction.UNSUITABLE, "reason": "kısa"}}})
    assert [p.name for p in paths] == ["sahne-plani.json", "istemler.md", "istem-paketi.html"]
    page = paths[2].read_text(encoding="utf-8")
    assert page.count("copyText('p") == 6 and "copyText('style'" in page and "sahne-03.mp4" in page
    assert "<script>" in page and "http" not in page.replace("http-equiv", "")  # offline: no external links
    assert "uygun değil" in page and "Uygun değil: kısa" in page and "0/6 klip geldi" in page
    markdown = paths[1].read_text(encoding="utf-8")
    assert markdown.count("```text") == 7 and "## Sahne 02 · `sahne-02.mp4` · uygun değil" in markdown
    data = json.loads(paths[0].read_text(encoding="utf-8"))
    assert data["clips"]["2"]["status"] == fiction.UNSUITABLE and data["clips"]["1"]["status"] == fiction.WAITING


# ---------------------------------------------------------------- klipler: yükleme, eşleştirme, doğrulama


@pytest.mark.parametrize(("name", "number"), [
    ("sahne-03.mp4", 3), ("Sahne 3.mp4", 3), ("scene_04.mov", 4), ("klip05.mp4", 5), ("02 ay yuzeyi.mp4", 2),
    ("ay yuzeyi.mp4", None), ("veo3_rover.mp4", None), ("sahne-09.mp4", None), ("1 ve 2.mp4", None),
])
def test_the_scene_number_in_a_file_name(name, number):
    assert fiction.scene_number(name, 6) == number


def test_validation_rules(tmp_path):
    good = fiction.probe(make_clip(tmp_path / "good.mp4"))
    assert fiction.validate_clip(good, 8) == (fiction.ARRIVED, "", [])
    short = fiction.probe(make_clip(tmp_path / "short.mp4", seconds=5))
    status, reason, _ = fiction.validate_clip(short, 8)
    assert status == fiction.UNSUITABLE and "kısa: 5.0 sn < 8 sn" in reason and "uzatılmaz" in reason
    wide = fiction.probe(make_clip(tmp_path / "wide.mp4", size="1920x1080"))
    status, _, warnings = fiction.validate_clip(wide, 8, "blur")
    assert status == fiction.ARRIVED and warnings == ["en-boy 1920×1080 9:16 değil: blur ile sığdırılacak"]
    low = fiction.probe(make_clip(tmp_path / "low.mp4", size="360x640"))
    assert fiction.validate_clip(low, 8)[:2] == (fiction.UNSUITABLE, "çözünürlük düşük: 360×640 (kısa kenar en az 720)")
    loud = fiction.probe(make_clip(tmp_path / "loud.mp4", audio=True))
    assert fiction.validate_clip(loud, 8) == (fiction.ARRIVED, "", ["ses akışı var: atılacak"])
    broken = tmp_path / "broken.mp4"
    broken.write_bytes(b"not a video at all" * 100)
    assert fiction.validate_clip(fiction.probe(broken), 8)[:2] == (fiction.UNSUITABLE, "video akışı okunamadı")


def test_upload_inbox_matching_and_the_render_condition(plan, tmp_path):
    folder = tmp_path / "konu"
    source = make_clip(tmp_path / "flow-export.mp4")
    entry = fiction.accept_upload(folder, plan, 1, "flow-export.mp4", source.read_bytes())
    assert entry["status"] == fiction.ARRIVED and entry["file"] == "sahne-01.mp4" and entry["source"] == "webui"
    assert len(entry["sha256"]) == 64 and entry["uploaded_at"]
    inbox = fiction.inbox(folder)
    make_clip(inbox / "sahne-02.mp4")
    make_clip(inbox / "Flow scene 3.mp4", seconds=5)
    make_clip(inbox / "04 rover.mp4", size="1920x1080")
    make_clip(inbox / "ay yuzeyi.mp4")
    make_clip(inbox / "veo3 export.mp4", audio=True)
    result = fiction.check_inbox(folder, plan)
    assert sorted((m["scene"], m["file"]) for m in result["matched"]) == [
        (2, "sahne-02.mp4"), (3, "Flow scene 3.mp4"), (4, "04 rover.mp4")]
    assert result["unmatched"] == ["ay yuzeyi.mp4", "veo3 export.mp4"]  # never guessed: the user picks
    states = {s["index"]: s for s in fiction.scene_states(folder, plan)}
    assert states[3]["status"] == fiction.UNSUITABLE and states[4]["warnings"]
    assert not fiction.clips_complete(folder, plan)
    with pytest.raises(fiction.FictionError, match="bütün sahnelerin klibi gelmedi"):
        fiction.write_render_plan(folder, plan, "x")
    fiction.assign(folder, plan, "ay yuzeyi.mp4", 5)
    fiction.assign(folder, plan, "veo3 export.mp4", 6)
    fiction.accept_upload(folder, plan, 3, "yeni.mp4", make_clip(tmp_path / "yeni.mp4").read_bytes())
    assert fiction.load_clips(folder)["unmatched"] == [] and fiction.clips_complete(folder, plan)
    assert "6/6 klip geldi" in (folder / fiction.PACKAGE_FILE).read_text(encoding="utf-8")
    # A second check does not redo unchanged files.
    assert fiction.check_inbox(folder, plan)["matched"] == []
    render_plan = json.loads(fiction.write_render_plan(folder, plan, "Ay'da helyum-3").read_text(encoding="utf-8"))
    assert [Path(s["file"]).name for s in render_plan["scenes"]] == [
        "sahne-01.mp4", "sahne-02.mp4", "sahne-03.mp4", "04 rover.mp4", "ay yuzeyi.mp4", "veo3 export.mp4"]
    assert render_plan["scenes"][0]["prompt"] == plan["scenes"][0]["prompt"]
    # A clip changed after the check is refused.
    make_clip(inbox / "sahne-02.mp4", color="red")
    with pytest.raises(fiction.FictionError, match="değişmiş"):
        fiction.write_render_plan(folder, plan, "x")


def test_a_non_video_upload_is_refused(plan, tmp_path):
    with pytest.raises(fiction.FictionError, match="video dosyası değil"):
        fiction.accept_upload(tmp_path, plan, 1, "resim.png", b"x")
    with pytest.raises(fiction.FictionError, match="planda yok"):
        fiction.accept_upload(tmp_path, plan, 9, "a.mp4", b"x")


# ---------------------------------------------------------------- iki aşamalı akış


def fiction_preparer(preset, item, task_id):
    estimate = batch_review.estimate(SCRIPT)
    plan = fiction.build_plan(item.subject, SCRIPT, estimate["tahmini_sn"], preset.fiction, ask=plan_answer)
    return {"script": SCRIPT, "script_sha256": batch_review.script_sha256(SCRIPT), "terms": [], "anchors": [],
            "audit": CLEAN_AUDIT, "scene_plan": plan, **estimate}


@pytest.fixture
def fiction_batch(tmp_path, monkeypatch):
    folder = tmp_path / "batches"
    monkeypatch.setattr(bp, "batches_dir", lambda: folder)
    source = tmp_path / "konular.txt"
    source.write_text("Ya olsaydı: Ay'da helyum-3 madenciliği yapılsaydı\n", encoding="utf-8")
    with patch.dict(config.app, {}):
        assert bp.run_batch(source, PRESET, output=folder / "kurgu", preparer=fiction_preparer,
                            reporter=lambda state, batch_dir: None) == 0
        yield folder / "kurgu"


def _folder(batch_dir):
    return batch_dir / bp.BatchState.load(batch_dir).items[0].folder


def test_preparation_writes_the_plan_and_the_package(fiction_batch):
    folder = _folder(fiction_batch)
    for name in (fiction.PLAN_FILE, fiction.PROMPTS_FILE, fiction.PACKAGE_FILE):
        assert (folder / name).is_file()
    assert (folder / fiction.INBOX).is_dir()
    review = batch_review.load_review(folder)
    expected = fiction.scene_count(batch_review.estimate(SCRIPT)["tahmini_sn"], 8)
    assert review["scene_plan"]["scene_count"] == expected and review["terms"] == []
    assert batch_review.is_clean(review)  # no terms needed for fiction
    assert batch_review.overview(fiction_batch).items[0].status == bp.AWAITING_REVIEW


class PlanServices:
    def __init__(self):
        self.calls = []

    def audit(self, subject, script):
        self.calls.append("audit")
        return CLEAN_AUDIT

    def terms(self, subject, script):
        raise AssertionError("fiction has no search terms")

    def scene_plan(self, subject, script, previous=None):
        self.calls.append("plan")
        style = previous.get("style_book") if previous and previous.get("style_book_edited") else None
        return fiction.build_plan(subject, script, batch_review.estimate(script)["tahmini_sn"], ask=plan_answer,
                                  style_book=style, previous=previous)


def test_a_text_edit_regenerates_the_plan_and_keeps_edited_prompts(fiction_batch):
    folder = _folder(fiction_batch)
    batch_review.save_plan_edits(folder, {"prompts": {2: "elle yazılmış istem"}})
    review = batch_review.load_review(folder)
    assert review["scene_plan"]["scenes"][1]["prompt_edited"]
    assert "elle yazılmış istem" in (folder / fiction.PROMPTS_FILE).read_text(encoding="utf-8")
    edited = SCRIPT + " Belki bir gün bunu göreceğiz."
    assert batch_review.needs_llm(review, script=edited)
    with pytest.raises(batch_review.ReviewError, match="needs_llm|metin değişti"):
        batch_review.approve(folder, script=edited)
    services = PlanServices()
    outcome = batch_review.approve(folder, services, script=edited)
    assert outcome.status == "approved" and services.calls == ["audit", "plan"]
    plan = outcome.review["scene_plan"]
    assert fiction.plan_current(plan, edited) and plan["scenes"][1]["prompt"] == "elle yazılmış istem"


def test_approved_fiction_waits_for_clips_then_renders_with_the_plan(fiction_batch, tmp_path):
    folder = _folder(fiction_batch)
    assert batch_review.approve(folder).status == "approved"
    view = batch_review.overview(fiction_batch)
    assert view.items[0].status == batch_review.AWAITING_CLIPS and view.approved_waiting == 0
    runner_calls = []

    def runner(argv, task_id):
        runner_calls.append(argv)
        return {"videos": ["x.mp4"], "script": SCRIPT}

    def packager(**kwargs):
        return {"kontrol_gerekli": False}

    assert bp.render_batch(fiction_batch, task_runner=runner, packager=packager,
                           reporter=lambda state, batch_dir: None) == 0
    assert runner_calls == []  # waiting for clips: nothing rendered
    plan = batch_review.load_review(folder)["scene_plan"]
    for scene in plan["scenes"]:
        make_clip(fiction.inbox(folder) / scene["file"])
    fiction.check_inbox(folder, plan)
    view = batch_review.overview(fiction_batch)
    assert view.items[0].status == bp.APPROVED and view.approved_waiting == 1
    assert bp.render_batch(fiction_batch, task_runner=runner, packager=packager,
                           reporter=lambda state, batch_dir: None) == 0
    argv = runner_calls[0]
    plan_arg = next(arg for arg in argv if arg.startswith("--ai-manual-plan="))
    assert Path(plan_arg.split("=", 1)[1]).name == fiction.RENDER_PLAN_FILE
    assert not any(arg.startswith("--video-terms") for arg in argv)
    assert "--video-source" in argv and argv[argv.index("--video-source") + 1] == "ai_manual"


def test_the_clips_command(fiction_batch, capsys):
    folder = _folder(fiction_batch)
    assert batch_review.cli_main(["klipler", str(fiction_batch), "1"]) == 2  # not approved yet
    batch_review.approve(folder)
    make_clip(fiction.inbox(folder) / "sahne-01.mp4")
    make_clip(fiction.inbox(folder) / "isimsiz.mp4")
    capsys.readouterr()
    assert batch_review.cli_main(["klipler", str(fiction_batch), "1", "--kontrol"]) == 0
    out = capsys.readouterr().out
    assert "sahne-01.mp4 → sahne 01" in out and "sahne 01: geldi" in out and "eşleşmeyen dosyalar" in out
    assert batch_review.cli_main(["klipler", str(fiction_batch), "1", "--ata", "isimsiz.mp4=2"]) == 0
    assert "sahne 02: geldi" in capsys.readouterr().out


# ---------------------------------------------------------------- görev: klipler, süre, kayıt


def _render_plan(tmp_path, durations, audio=False):
    clips = []
    for index, seconds in enumerate(durations, start=1):
        path = make_clip(tmp_path / f"c{index}.mp4", seconds=seconds, audio=audio and index == 1,
                         color=("blue", "red", "green")[index % 3])
        clips.append({"index": index, "file": str(path), "sha256": fiction._sha256(path),
                      "uploaded_at": "2026-11-14T10:00:00", "prompt": f"prompt {index}", "narration": f"n{index}"})
    plan = tmp_path / "render-plani.json"
    plan.write_text(json.dumps({"tool": fiction.TOOL, "style_version": "abc", "clip_seconds": 8,
                                "scene_label": fiction.SCENE_LABEL, "scenes": clips}), encoding="utf-8")
    return plan


def test_task_clips_are_copied_checked_and_recorded(tmp_path, monkeypatch):
    monkeypatch.setattr(fiction.utils, "task_dir", lambda task_id: str(tmp_path / "task"))
    persisted = {}
    from app.services import material

    monkeypatch.setattr(material, "_persist_material_sources", lambda task_id, records: persisted.update(r=records))
    plan = _render_plan(tmp_path, [8, 8, 8])
    paths = fiction.task_clips("t1", plan, audio_duration=21.0)
    assert [Path(p).name for p in paths] == ["sahne-01.mp4", "sahne-02.mp4", "sahne-03.mp4"]
    copied = Path(paths[0])
    assert copied.parent == tmp_path / "task" / "ai_clips" and not copied.samefile(tmp_path / "c1.mp4")
    record = persisted["r"][0]
    assert record["provider"] == "ai_manual" and record["tool"] == "Google Flow / Veo"
    assert record["prompt"] == "prompt 1" and record["style_version"] == "abc" and len(record["sha256"]) == 64
    assert record["scene_label"] == fiction.SCENE_LABEL and record["ai_generated"] is True
    assert record["uploaded_at"] == "2026-11-14T10:00:00" and record["asset_id"] == "sahne-01"
    assert fiction.scene_seconds(21.0, 3) == pytest.approx(7.0167, abs=1e-3)
    # The narration would need more than a clip per scene: refused (a short clip is never stretched).
    with pytest.raises(fiction.FictionError, match="kısa klip uzatılmaz"):
        fiction.task_clips("t1", plan, audio_duration=26.0)
    # A clip changed after the plan was written.
    make_clip(tmp_path / "c2.mp4", color="white")
    with pytest.raises(fiction.FictionError, match="onaydan sonra değişmiş"):
        fiction.task_clips("t1", plan, audio_duration=21.0)


def test_the_clips_play_in_order_without_their_sound_and_every_scene_is_labelled(tmp_path):
    plan = json.loads(_render_plan(tmp_path, [8, 8, 8], audio=True).read_text(encoding="utf-8"))
    clips = [scene["file"] for scene in plan["scenes"]]
    audio = tmp_path / "voice.mp3"
    subprocess.run([FF, "-y", "-loglevel", "error", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono", "-t", "21",
                    str(audio)], check=True, capture_output=True)
    timeline = []
    combined = tmp_path / "combined-1.mp4"
    video.combine_videos(str(combined), clips, str(audio), max_clip_duration=fiction.scene_seconds(21, 3),
                         video_concat_mode="sequential", video_fit_mode="blur", scene_timeline=timeline)
    info = fiction.probe(combined)
    assert not info["audio"] and info["duration"] == pytest.approx(21.0, abs=0.3)
    assert [Path(scene["source"]).name for scene in timeline] == ["c1.mp4", "c2.mp4", "c3.mp4"]
    from app.services import task

    labelled = {Path(c).name: fiction.SCENE_LABEL for c in clips}
    labels = task.scene_labels_for(timeline, labelled, "tr-TR")
    assert len(labels) == 3 and {label["text"] for label in labels} == {"Canlandırma · yapay zekâ"}


def test_the_label_and_the_subtitles_stay_off_the_veo_watermark():
    preset = bp.load_preset(PRESET)
    params = SimpleNamespace(subtitle_enabled=True, subtitle_position=preset.video["subtitle_position"],
                             custom_position=preset.video["custom_position"], font_size=preset.video["font_size"])
    assert not video._subtitles_in_upper_half(params)  # label goes to the top left corner
    x0, y0, _, _ = fiction.VEO_WATERMARK_BOX
    height, block = 1920, 2 * preset.video["font_size"] * 1.5  # two subtitle lines
    bottom = (height - block) * params.custom_position / 100 + block
    assert bottom < y0 * height  # the subtitles end above the watermark
    assert video.SCENE_LABEL_MARGIN < x0  # the label sits at the left edge, the watermark at the right


# ---------------------------------------------------------------- atıflar


def _records():
    return [{"provider": "ai_manual", "asset_id": f"sahne-0{i}", "tool": "Google Flow / Veo", "local_file": f"{i}.mp4"}
            for i in (1, 2)]


def test_credits_share_text_and_no_nasa_credit_on_screen():
    text = nasa.credits_text(_records())
    assert "1. [Yapay zekâ] Sahne 01 — Google Flow / Veo" in text and nasa.AI_CREDIT_LINE in text
    assert "NASA" not in text.replace("NASA", "", 0) or "onaylanmamıştır" not in text
    assert nasa.overlay_credit_text(_records(), "tr-TR") == ""
    lines = batch_package.credit_lines(_records())
    assert lines == [batch_package.AI_CREDIT, batch_package.AI_NOTE]
    texts = batch_package.social_texts("Ya olsaydı", SCRIPT, _records(), ask=lambda prompt: "{}")
    assert texts["yapay_zeka"] and batch_package.AI_CREDIT in texts["youtube"]["description"]
    page = batch_package.social_markdown("Ya olsaydı", texts)
    assert "Yükleme notu: yapay zekâ etiketi" in page and "Değiştirilmiş veya yapay içerik" in page
    assert batch_package.material_summary(_records()) == {"Yapay zekâ (Flow / Veo)": 2}


# ---------------------------------------------------------------- revizyon: sahne değiştirme


def test_scene_prompt_flags_round_trip():
    args = SimpleNamespace(sahne_degistir=[], sahne_ara=[], sahne_dosya=["v1-s02=C:/k/yeni.mp4"], sahne_kaldir=[],
                           kirpma=[], sahne_istemi=["v1-s02=A new prompt = with equals"])
    ops = scene_edits.from_arguments(args)
    assert ops == [scene_edits.SceneOp("replace", "v1-s02", path="C:/k/yeni.mp4", prompt="A new prompt = with equals")]
    assert scene_edits.to_arguments(ops) == ["--sahne-dosya=v1-s02=C:/k/yeni.mp4",
                                             "--sahne-istemi=v1-s02=A new prompt = with equals"]
    with pytest.raises(scene_edits.SceneEditError, match="--sahne-istemi needs"):
        scene_edits.from_arguments(SimpleNamespace(**{**vars(args), "sahne_dosya": []}))


def test_a_fiction_scene_takes_a_validated_clip_with_its_prompt(tmp_path):
    # The task folder is in the isolated runtime home (test/conftest.py), not the real storage.
    manifest = task_materials.manifest_path("t1")
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps({"schema_version": 1, "materials": [], "terms": [], "scenes": []}), encoding="utf-8")
    scene = {"id": "v1-s02", "duration": 7.4, "search_term": "n2", "term_index": None}
    previous = {"provider": "ai_manual", "asset_id": "sahne-02", "tool": fiction.TOOL, "prompt": "old prompt",
                "style_version": "abc", "scene_label": fiction.SCENE_LABEL}
    with pytest.raises(scene_edits.SceneEditError, match="kısa"):
        scene_edits.add_fiction_clip("t1", "r002", scene, str(make_clip(tmp_path / "s.mp4", seconds=5)), previous)
    added, warnings = scene_edits.add_fiction_clip("t1", "r002", scene,
                                                   str(make_clip(tmp_path / "n.mp4", audio=True)), previous, "new one")
    assert warnings == ["ses akışı var: atılacak"]
    source = added["source"]
    assert source["provider"] == "ai_manual" and source["prompt"] == "new one" and source["asset_id"] == "sahne-02"
    assert source["scene_label"] == fiction.SCENE_LABEL and added["added_by"] == "r002"
    kept, _ = scene_edits.add_fiction_clip("t1", "r003", scene, str(make_clip(tmp_path / "m.mp4")), previous)
    assert kept["source"]["prompt"] == "old prompt"  # no edit: the scene's prompt
