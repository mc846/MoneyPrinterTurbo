"""Toplu üretim paketi：审核员 JSON 解析与标记规则、发布文案、技术检查、封面选择、发布包内容和 rapor.html。LLM 用假函数替代。"""

import json
import re
import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from PIL import Image

from app.services import batch_package as bpk
from app.services import batch_production as bp
from app.services import batch_subtitles as bst
from app.utils import utils

FF = utils.get_ffmpeg_binary()
FONT = Path(__file__).resolve().parents[2] / "resource" / "fonts" / "STHeitiMedium.ttc"
PRESET = Path(__file__).resolve().parents[2] / "presets" / "uzay.toml"
NASA = {"provider": "nasa", "asset_id": "PIA14871", "credit": "NASA/JPL-Caltech/UCLA", "search_term": "exploding star"}
PIXABAY = {"provider": "pixabay", "asset_id": "156133", "search_term": "binary star system"}

AUDIT = {"claims": [
    {"claim": "Nötron yıldızları süpernova kalıntısıdır.", "rating": "D", "reason": "Doğru."},
    {"claim": "Bir çay kaşığı bir dağ kadar ağırdır.", "rating": "B", "reason": "Yaklaşık bir benzetme."},
]}
SOCIAL = {
    "youtube": {"title": "Bir kaşığı dağ kadar ağır yıldız", "description": "Nötron yıldızlarını anlatıyoruz.",
                "tags": ["#uzay", "#bilim", "#shorts"]},
    "tiktok": {"caption": "Bir çay kaşığı nötron yıldızı ne kadar ağır?", "hashtags": ["#uzay", "bilim"]},
    "instagram": {"caption": "Süpernovadan geriye kalanlar.", "hashtags": ["#uzay", "#astronomi"]},
}


def asker(*responses):
    """Sırayla yanıt veren sahte LLM; denetçi, çeviri ve paylaşım istemlerini ayrı sıralarla yanıtlar."""
    queues = {"audit": [], "social": [], "translate": []}
    for response in responses:
        kind, text = response
        queues[kind].append(text)

    def ask(prompt):
        kind = ("audit" if "fact-checker" in prompt else "translate" if "Subtitle translator" in prompt
                else "social")
        ask.prompts.append((kind, prompt))
        return queues[kind].pop(0)

    ask.prompts = []
    return ask


# ---------------------------------------------------------------- denetçi

@pytest.mark.parametrize("response", [
    json.dumps(AUDIT, ensure_ascii=False),
    "```json\n" + json.dumps(AUDIT, ensure_ascii=False) + "\n```",
    "Değerlendirme aşağıda:\n" + json.dumps(AUDIT, ensure_ascii=False) + "\nBaşka not yok.",
])
def test_parse_audit_accepts_fenced_and_embedded_json(response):
    claims = bpk.parse_audit(response)
    assert [c["rating"] for c in claims] == ["D", "B"]
    assert claims[1]["reason"] == "Yaklaşık bir benzetme."


def test_parse_audit_normalizes_rating_letters():
    claims = bpk.parse_audit('{"claims":[{"claim":"a","rating":"x (yanlış)","reason":""}]}')
    assert claims == [{"claim": "a", "rating": "X", "reason": ""}]


@pytest.mark.parametrize("response, message", [
    ('{"claims": []}', "iddia listesi yok"),
    ('{"iddialar": [{"claim": "a", "rating": "D"}]}', "iddia listesi yok"),
    ('{"claims": [{"claim": "a", "rating": "E"}]}', "geçersiz değerlendirme"),
    ('{"claims": [{"claim": "", "rating": "D"}]}', "boş iddia"),
    ('{"claims": ["a"]}', "nesne değil"),
    ("bilmiyorum", "JSON"),
])
def test_parse_audit_rejects_invalid_responses(response, message):
    with pytest.raises(ValueError, match=message):
        bpk.parse_audit(response)


def test_audit_flags_misleading_and_wrong_claims():
    audit = {"claims": AUDIT["claims"] + [
        {"claim": "Hepsi pulsardır.", "rating": "Y", "reason": "Her nötron yıldızı pulsar olarak görünmez."},
        {"claim": "Güneş bir gün nötron yıldızı olacak.", "rating": "X", "reason": "Güneş beyaz cüce olacak."},
    ]}
    result = bpk.audit_script("Nötron yıldızları", "metin", ask=asker(("audit", json.dumps(audit))))
    assert result["sayim"] == {"D": 1, "B": 1, "Y": 1, "X": 1}
    assert [c["rating"] for c in result["isaretli"]] == ["Y", "X"]
    assert bpk.review_reasons(result, {}) == ["doğruluk: 1 Y (yanıltıcı)", "doğruluk: 1 X (yanlış)"]


def test_audit_with_only_correct_and_uncertain_claims_needs_no_review():
    result = bpk.audit_script("k", "metin", ask=asker(("audit", json.dumps(AUDIT))))
    assert result["durum"] == "tamam" and result["isaretli"] == []
    assert bpk.review_reasons(result, {}) == []


def test_audit_retries_once_and_failure_needs_review():
    ask = asker(("audit", "bozuk"), ("audit", json.dumps(AUDIT)))
    assert bpk.audit_script("k", "metin", ask=ask)["durum"] == "tamam"
    assert len(ask.prompts) == 2
    failed = bpk.audit_script("k", "metin", ask=asker(("audit", "bozuk"), ("audit", "yine bozuk")))
    assert failed["durum"] == "yapılamadı" and "JSON" in failed["hata"]
    assert bpk.review_reasons(failed, {}) == ["doğruluk denetimi yapılamadı"]


def test_audit_prompt_is_independent_and_skeptical():
    ask = asker(("audit", json.dumps(AUDIT)))
    bpk.audit_script("Zombi yıldızlar", "Zombi yıldızlar ölmez.", ask=ask)
    prompt = ask.prompts[0][1]
    assert "You did not write the text" in prompt and "Whenever you are not sure, use B" in prompt
    assert prompt.endswith("Zombi yıldızlar ölmez.")


def test_missing_glyphs_and_credit_problems_need_review_while_targets_only_warn():
    checks = {"sure_sn": 72.0, "sure_hedef": [35.0, 60.0], "sure_uygun": False, "kelime": 130,
              "kelime_hedef": [90, 100], "kelime_uygun": False, "glifsiz_karakterler": ["☄"],
              "kredi_sorunlari": ["X: ©Getty"]}
    assert bpk.review_reasons({"durum": "tamam", "sayim": {"D": 3}}, checks) == [
        "yazı tipinde glifi olmayan karakter: ☄", "NASA/izinli ortak dışı kredi: 1"]
    assert bpk.check_warnings(checks) == ["süre 72.0 sn (hedef 35-60)", "kelime sayısı 130 (hedef 90-100)"]
    assert bpk.check_warnings({"sure_uygun": True, "kelime_uygun": True}) == []


# ---------------------------------------------------------------- paylaşım metni

def test_social_texts_add_credit_lines_and_respect_limits():
    response = dict(SOCIAL, youtube=dict(SOCIAL["youtube"], title="Ç" * 150))
    texts = bpk.social_texts("Nötron yıldızları", "Metin.", [NASA, PIXABAY],
                             ask=asker(("social", json.dumps(response, ensure_ascii=False))))
    assert len(texts["youtube"]["title"]) == bpk.YOUTUBE_TITLE_MAX
    assert texts["krediler"] == ["Görüntüler: NASA ve ortakları (images.nasa.gov)", bpk.PIXABAY_CREDIT]
    for caption in (texts["youtube"]["description"], texts["tiktok"]["caption"], texts["instagram"]["caption"]):
        assert "Görüntüler: NASA ve ortakları (images.nasa.gov)\nEk görüntüler: Pixabay (pixabay.com)" in caption
    assert texts["tiktok"]["hashtags"] == ["#uzay", "#bilim"]
    assert texts["tiktok"]["caption"].endswith("#uzay #bilim")
    assert not texts["yedek_metin"]


def test_long_caption_is_shortened_to_keep_credits_and_hashtags():
    response = dict(SOCIAL, tiktok={"caption": "uzay " * 600, "hashtags": ["#uzay"]})
    texts = bpk.social_texts("k", "Metin.", [NASA], ask=asker(("social", json.dumps(response))))
    caption = texts["tiktok"]["caption"]
    assert len(caption) <= bpk.CAPTION_MAX and caption.endswith("(images.nasa.gov)\n\n#uzay")


def test_no_credit_line_without_nasa_or_pixabay_material():
    texts = bpk.social_texts("k", "Metin.", [{"provider": "pexels"}], ask=asker(("social", json.dumps(SOCIAL))))
    assert texts["krediler"] == [] and "Görüntüler" not in texts["tiktok"]["caption"]


@pytest.mark.parametrize("phrase", ["NASA'ya göre bu yıldızlar", "NASA açıkladı:", "NASA sunar", "according to NASA"])
def test_attribution_to_an_agency_is_asked_again(phrase):
    bad = dict(SOCIAL, instagram={"caption": f"{phrase} çok yoğun.", "hashtags": ["#uzay"]})
    ask = asker(("social", json.dumps(bad, ensure_ascii=False)), ("social", json.dumps(SOCIAL, ensure_ascii=False)))
    texts = bpk.social_texts("k", "Metin.", [NASA], ask=ask)
    assert len(ask.prompts) == 2 and "2. kurala kesinlikle uy" in ask.prompts[1][1]
    assert phrase not in texts["instagram"]["caption"]


def test_social_fallback_when_llm_keeps_failing():
    texts = bpk.social_texts("Nötron yıldızları", "İlk cümle burada. İkinci cümle.", [NASA],
                             ask=asker(("social", "bozuk"), ("social", "yine bozuk")))
    assert texts["yedek_metin"] and texts["youtube"]["title"] == "Nötron yıldızları"
    assert texts["tiktok"]["caption"].startswith("İlk cümle burada.")
    assert "elle düzenleyin" in bpk.social_markdown("Nötron yıldızları", texts)


def test_social_prompt_forbids_attribution_and_credit_lines():
    ask = asker(("social", json.dumps(SOCIAL)))
    bpk.social_texts("k", "Metin.", [NASA], ask=ask)
    prompt = ask.prompts[0][1]
    assert "Bilgiyi hiçbir kuruma atfetme" in prompt and "Kaynak ya da kredi satırı yazma" in prompt


# ---------------------------------------------------------------- teknik kontrol

LOG = [
    "nasa image search: term='neutron star', total_hits=40, results=40, checked=12, kept=5, "
    "skipped={'person': 2, 'text_graphic': 3, 'resolution': 1, 'no_size': 1}, unchecked=0",
    "nasa image search: term=\"pulsar's beam\", total_hits=0, results=0, checked=0, kept=0, skipped={}, unchecked=0",
    "nasa image search: term='magnetar', total_hits=3, results=3, checked=3, kept=0, skipped={'copyright': 3}, "
    "unchecked=0",
    "material fallback: term='magnetar', nasa=0/0 qualified, pixabay=4/9 qualified, min_relevance=0.5, "
    "min_candidates=1, using=pixabay",
    "material fallback: term='crab nebula', nasa=0/2 qualified, pixabay=0/0 qualified, min_relevance=0.5, "
    "min_candidates=1, using=nasa",
    "material fallback: term='star dust', nasa=0/0 qualified, pixabay=0/0 qualified, min_relevance=0.5, "
    "min_candidates=1, using=nasa",
    "nasa text filter: skipped PIA1 for 'neutron star', text=0.31",
    "nasa face filter: skipped 6758008, faces=1",
]


def test_log_summary_counts_empty_and_fallback_terms_and_eliminations():
    summary = bpk.log_summary(LOG, ["neutron star", "pulsar's beam", "magnetar", "crab nebula", "star dust", "galaxy"])
    assert summary["bos_terimler"] == ["pulsar's beam", "star dust"]
    assert summary["yedege_gecen_terimler"] == ["magnetar"]
    assert summary["filtre_elemeleri"] == {"yazi_onizleme": 1, "yuz": 1, "kisi": 2, "yazi_metaveri": 3, "telif": 3,
                                           "kredi": 0, "cozunurluk": 1, "diger": 1}
    assert summary["onbellekten_gelen_aramalar"] == 1  # "galaxy"


def test_missing_glyphs_are_reported():
    assert bpk.missing_glyphs("Çığ şölen İĞÜ, 1.5 km?", str(FONT)) == []
    assert bpk.missing_glyphs("Yıldız \U0001F680", str(FONT)) == ["\U0001F680"]


def test_credit_problems_list_non_nasa_credits():
    records = [NASA, {"provider": "nasa", "asset_id": "B", "credit": "Getty Images"}, PIXABAY]
    assert bpk.credit_problems(records) == ["B: Getty Images"]


# ---------------------------------------------------------------- paket

def make_video(path: Path, source: str, seconds: float = 3):
    spec = f"{source}{':' if '=' in source else '='}s=180x320:r=30"
    subprocess.run([FF, "-y", "-loglevel", "error", "-f", "lavfi", "-i", spec,
                    "-t", str(seconds), "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)],
                   check=True, capture_output=True)
    return path


@pytest.fixture
def finished_task(tmp_path, monkeypatch):
    """Bitmiş bir görevin klasörü: script.json, altyazı, credits.txt, son video ve önbellekte iki klip."""
    cache = tmp_path / "cache_videos"
    cache.mkdir()
    task_root = tmp_path / "tasks"
    monkeypatch.setattr(bpk.utils, "storage_dir", lambda sub="", create=False: str(tmp_path / sub))
    monkeypatch.setattr(bpk.utils, "task_dir", lambda sub="": str(task_root / sub))
    monkeypatch.setattr(bpk.utils, "font_dir", lambda sub="": str(FONT.parent))
    task = task_root / "t1"
    task.mkdir(parents=True)
    make_video(cache / "flat.mp4", "color=c=gray")
    make_video(cache / "detail.mp4", "testsrc2")
    make_video(cache / "stock.mp4", "mandelbrot")
    script = "Bir çay kaşığı nötron yıldızı bir dağ kadar çok ağırdır. " * 10  # 100 kelime
    sources = [dict(NASA, local_file="flat.mp4"), dict(NASA, asset_id="PIA2", local_file="detail.mp4"),
               dict(PIXABAY, local_file="stock.mp4")]
    (task / "script.json").write_text(json.dumps({
        "script": script, "search_terms": ["neutron star", "pulsar's beam"],
        "params": {"font_name": "STHeitiMedium.ttc", "video_source": "nasa"}, "material_sources": sources,
    }), encoding="utf-8")
    # Görevin altyazısı, pipeline'ın yaptığı gibi cümle sonu noktalamasız: 10 cümle 4 sn'ye.
    cues = [f"{index + 1}\n00:00:0{index * 4 // 10},{index * 4 % 10}00 --> 00:00:0{index * 4 // 10},"
            f"{index * 4 % 10 + 3}50\nBir çay kaşığı nötron yıldızı bir dağ kadar çok ağırdır\n" for index in range(10)]
    (task / "subtitle.srt").write_text("\n".join(cues), encoding="utf-8")
    (task / "credits.txt").write_text("NASA PIA14871\n", encoding="utf-8")
    make_video(task / "final-1.mp4", "testsrc", seconds=4)
    return task


TRANSLATION = json.dumps(["A teaspoon of neutron star weighs as much as a mountain."] * 10)


def build(task, folder, responses, preset=None):
    preset = preset or bp.load_preset(PRESET)
    item = bp.ItemState(index=1, subject="Nötron yıldızları", task_id=task.name, folder=folder.name)
    data = json.loads((task / "script.json").read_text(encoding="utf-8"))
    result = {"videos": [str(task / "final-1.mp4")], "script": data["script"], "terms": data["search_terms"]}
    return bpk.build_package(item, result, preset, folder, LOG, ask=asker(*responses))


def test_package_contents(finished_task, tmp_path):
    folder = tmp_path / "batch" / "01-notron-yildizlari"
    summary = build(finished_task, folder, [("social", json.dumps(SOCIAL)), ("audit", json.dumps(AUDIT)),
                                            ("translate", TRANSLATION)])
    assert sorted(p.name for p in folder.iterdir()) == [
        "altyazi.en.srt", "altyazi.tr.srt", "credits.txt", "kalite.json", "kapak.jpg", "kareler.jpg", "paylasim.md",
        "script.txt", "video.mp4"]
    turkish = (folder / "altyazi.tr.srt").read_bytes()
    assert turkish.startswith(b"1\r\n00:00:00,000 --> 00:00:00,350\r\nBir \xc3\xa7ay")
    assert bst.validate_srt(turkish.decode("utf-8"), 4000) == []
    # Noktalama seslendirilen metinden geri geldi; İngilizce aynı zamanlamada.
    assert [cue.text for cue in bst.parse_srt(turkish.decode())][0].endswith("ağırdır.")
    english = bst.parse_srt((folder / "altyazi.en.srt").read_text(encoding="utf-8"))
    assert [(cue.start, cue.end) for cue in english] == [(cue.start, cue.end) for cue in bst.parse_srt(turkish.decode())]
    assert (folder / "video.mp4").read_bytes() == (finished_task / "final-1.mp4").read_bytes()
    assert (folder / "script.txt").read_text(encoding="utf-8").startswith("Bir çay kaşığı")
    assert Image.open(folder / "kapak.jpg").size == bpk.COVER_SIZE
    assert Image.open(folder / "kareler.jpg").size == (270 * bpk.SHEET_FRAMES, 504)
    share = (folder / "paylasim.md").read_text(encoding="utf-8")
    assert "## YouTube Shorts" in share and "## TikTok" in share and "## Instagram Reels" in share
    assert "Görüntüler: NASA ve ortakları (images.nasa.gov)" in share
    assert ("**Altyazı dosyaları:** altyazi.tr.srt, altyazi.en.srt (YouTube Studio > Altyazılar > Dosya yükle)"
            in share.split("## TikTok")[0])
    quality = json.loads((folder / "kalite.json").read_text(encoding="utf-8"))
    assert quality["altyazi"]["dosyalar"] == ["altyazi.tr.srt", "altyazi.en.srt"]
    assert quality["altyazi"]["tr"]["noktalama_geri_geldi"] and quality["altyazi"]["en"]["yontem"] == "tek çağrı"
    # 100 kelime hedefin içinde, 4 sn süre hedefin dışında.
    assert quality["teknik"]["kelime"] == 100 and quality["teknik"]["kelime_uygun"]
    assert quality["teknik"]["sure_sn"] == 4.0 and not quality["teknik"]["sure_uygun"]
    assert quality["teknik"]["bos_terimler"] == ["pulsar's beam"]
    # Hedef dışı süre yalnız uyarıdır; D ve B iddialarla kontrol gerekmez.
    assert quality["nedenler"] == [] and quality["uyarilar"] == ["süre 4.0 sn (hedef 35-60)"]
    assert summary == {"kontrol_gerekli": False, "nedenler": [], "uyarilar": quality["uyarilar"], "sure_sn": 4.0,
                       "kelime": 100, "sayim": {"D": 1, "B": 1, "Y": 0, "X": 0},
                       "altyazilar": ["altyazi.tr.srt", "altyazi.en.srt"]}
    moments = quality["kareler_sn"]
    assert len(moments) == bpk.SHEET_FRAMES and moments == sorted(moments) and 0 < moments[0] and moments[-1] < 4


def test_package_for_a_revision_uses_its_subtitles_sources_and_audit(finished_task, tmp_path):
    """task_revisions.refresh_package: the revision's own subtitle, sources and audit; the text is not re-audited."""
    revision = finished_task / "revisions" / "r002"
    revision.mkdir(parents=True)
    shutil.copy2(finished_task / "subtitle.srt", revision / "subtitle.srt")
    make_video(revision / "final.mp4", "testsrc2", seconds=4)
    folder = tmp_path / "batch" / "01-notron-yildizlari" / "revizyonlar" / "r002"
    audit = {"durum": "tamam", "iddialar": [], "sayim": {"D": 3, "B": 0, "Y": 0, "X": 0}, "isaretli": []}
    item = bp.ItemState(index=1, subject="Nötron yıldızları", task_id=finished_task.name, folder="01-notron-yildizlari")
    script = (finished_task / "script.json").read_text(encoding="utf-8")
    script = json.loads(script)["script"]
    ask = asker(("social", json.dumps(SOCIAL)), ("translate", TRANSLATION))
    summary = bpk.build_package(
        item, {"videos": [str(revision / "final.mp4")], "script": script}, bp.load_preset(PRESET), folder, LOG,
        ask=ask, revision={"name": "r002", "subtitle": str(revision / "subtitle.srt"),
                           "material_sources": [dict(PIXABAY, local_file="stock.mp4")],
                           "params": {"font_name": "STHeitiMedium.ttc", "video_source": "nasa"}, "audit": audit},
    )
    assert [kind for kind, _prompt in ask.prompts] == ["translate", "social"]  # no audit call
    assert (folder / "video.mp4").read_bytes() == (revision / "final.mp4").read_bytes()
    # Only Pixabay footage in this revision: no NASA credits file, unlike the original package.
    assert not (folder / "credits.txt").exists()
    quality = json.loads((folder / "kalite.json").read_text(encoding="utf-8"))
    assert quality["revizyon"] == "r002" and quality["dogruluk"] == audit
    assert quality["altyazi"]["dosyalar"] == ["altyazi.tr.srt", "altyazi.en.srt"]
    assert summary["sayim"] == audit["sayim"]


def test_frame_extraction_never_reads_the_console(finished_task):
    real = subprocess.run
    with patch.object(bpk.subprocess, "run", side_effect=real) as run:
        assert bpk._frame(finished_task / "final-1.mp4", 1.0) is not None
    command = run.call_args.args[0]
    assert "-nostdin" in command[: command.index("-i")]


def test_cover_prefers_primary_source_then_sharpest_frame(finished_task, tmp_path):
    data = json.loads((finished_task / "script.json").read_text(encoding="utf-8"))
    final = finished_task / "final-1.mp4"
    image, info = bpk.choose_cover(data["material_sources"], finished_task, final, "nasa")
    assert info["kaynak"] == "PIA2" and info["saglayici"] == "nasa" and image.size == bpk.COVER_SIZE
    # Asıl kaynaktan aday yoksa yedek kaynak seçilir.
    _, info = bpk.choose_cover(data["material_sources"][2:], finished_task, final, "nasa")
    assert info["kaynak"] == "156133"
    # Hiç kaynak dosyası yoksa son videonun orta karesi.
    image, info = bpk.choose_cover([dict(NASA, local_file="yok.mp4")], finished_task, final, "nasa")
    assert image.size == bpk.COVER_SIZE and "son videonun" in info["kaynak"]


def test_flagged_claim_makes_package_need_review(finished_task, tmp_path):
    preset = bp.load_preset(PRESET)
    preset.target_duration = None
    audit = {"claims": [{"claim": "Güneş nötron yıldızı olacak.", "rating": "X", "reason": "Kütlesi yetmez."}]}
    summary = build(finished_task, tmp_path / "02-x", [("social", json.dumps(SOCIAL)), ("audit", json.dumps(audit)),
                                                       ("translate", TRANSLATION)], preset)
    assert summary["kontrol_gerekli"] and summary["nedenler"] == ["doğruluk: 1 X (yanlış)"]


def test_audit_off_skips_the_auditor(finished_task, tmp_path):
    preset = bp.load_preset(PRESET)
    preset.audit, preset.target_duration, preset.subtitle_translations = False, None, ()
    summary = build(finished_task, tmp_path / "03-x", [("social", json.dumps(SOCIAL))], preset)
    assert summary == {"kontrol_gerekli": False, "nedenler": [], "uyarilar": [], "sure_sn": 4.0, "kelime": 100,
                       "sayim": {}, "altyazilar": ["altyazi.tr.srt"]}


def test_translation_warnings_reach_the_quality_file_and_the_report(finished_task, tmp_path):
    preset = bp.load_preset(PRESET)
    preset.target_duration = None
    untranslated = json.dumps(["Bir çay kaşığı nötron yıldızı bir dağ kadar çok ağırdır."] + ["A mountain."] * 9)
    folder = tmp_path / "batch" / "04-x"
    summary = build(finished_task, folder, [("social", json.dumps(SOCIAL)), ("audit", json.dumps(AUDIT)),
                                            ("translate", untranslated)], preset)
    assert not summary["kontrol_gerekli"]
    assert summary["uyarilar"][0].startswith("altyazi.en.srt: 1. altyazı: çevrilmemiş görünüyor")
    assert "altyazi.en.srt: 2. altyazı: uzunluk oranı 0.20" in summary["uyarilar"]  # 11 / 56 karakter
    item = bp.ItemState(index=1, subject="x", folder="04-x", status="done", quality=summary)
    state = bp.BatchState(input="a.txt", input_sha256="", preset="r.toml", preset_sha256="", created="", items=[item])
    page = bpk.write_report(state, tmp_path / "batch").read_text(encoding="utf-8")
    assert '<a href="04-x/altyazi.tr.srt">altyazi.tr.srt</a> · <a href="04-x/altyazi.en.srt">altyazi.en.srt</a>' in page
    assert "<th>Altyazı dosyaları</th><td>altyazi.tr.srt, altyazi.en.srt</td>" in page
    assert "çevrilmemiş görünüyor" in page


# ---------------------------------------------------------------- rapor

def report_state(tmp_path):
    batch = tmp_path / "batch"
    for folder, reasons in (("01-hazir", []), ("02-supheli", ["doğruluk: 1 Y (yanıltıcı)"])):
        (batch / folder).mkdir(parents=True)
        (batch / folder / "kapak.jpg").write_bytes(b"jpg")
        (batch / folder / "kareler.jpg").write_bytes(b"jpg")
        (batch / folder / "script.txt").write_text("Metin <b>kalın değil</b>", encoding="utf-8")
        (batch / folder / "paylasim.md").write_text("# Paylaşım\n", encoding="utf-8")
        claims = [{"claim": "Hepsi pulsardır.", "rating": "Y", "reason": "Hepsi değil."}] if reasons else []
        (batch / folder / "kalite.json").write_text(json.dumps({
            "kontrol_gerekli": bool(reasons), "nedenler": reasons,
            "uyarilar": [] if reasons else ["kelime sayısı 86 (hedef 90-100)"],
            "dogruluk": {"durum": "tamam", "iddialar": claims, "sayim": {"Y": len(claims)}},
            "teknik": {"sure_sn": 44.2, "sure_hedef": [35, 60], "kelime": 94, "kelime_hedef": [90, 100],
                       "filtre_elemeleri": {"yuz": 1, "kisi": 0}},
        }), encoding="utf-8")
    items = [
        bp.ItemState(index=1, subject="Hazır konu", folder="01-hazir", status="done", seconds=61,
                     quality={"kontrol_gerekli": False}),
        bp.ItemState(index=2, subject="Şüpheli konu", folder="02-supheli", status="done", seconds=70,
                     quality={"kontrol_gerekli": True}),
        bp.ItemState(index=3, subject="Bozuk <ses>", folder="03-bozuk-ses", status="failed", attempts=2, failures=2,
                     error="voice not found: tr-TR-Yok", failed_stage="audio"),
        bp.ItemState(index=4, subject="Sıradaki", folder="04-siradaki"),
    ]
    state = bp.BatchState(input=str(tmp_path / "konular.csv"), input_sha256="", preset=str(PRESET),
                          preset_sha256="", created="2026-11-05T10:00:00", items=items, run_seconds=754)
    return state, batch


def test_report_summary_order_and_relative_paths(tmp_path):
    state, batch = report_state(tmp_path)
    page = bpk.write_report(state, batch).read_text(encoding="utf-8")
    stats = dict((label, value) for value, label in re.findall(r'<div class="stat"><b>([^<]*)</b>([^<]*)</div>', page))
    assert stats == {"Toplam": "4", "Başarılı": "2", "Başarısız": "1", "Kontrol gerekli": "1", "Bekleyen": "1",
                     "Toplam süre": "12 dk 34 sn"}
    headings = re.findall(r"<h2>(\d+)\. ", page)
    assert headings == ["3", "2", "1", "4"]  # başarısız, kontrol gerekli, hazır, bekleyen
    assert 'src="02-supheli/kapak.jpg"' in page and 'src="02-supheli/kareler.jpg"' in page
    assert str(tmp_path) not in page


def test_report_shows_failure_flags_and_escapes_text(tmp_path):
    state, batch = report_state(tmp_path)
    page = bpk.write_report(state, batch).read_text(encoding="utf-8")
    assert "Bozuk &lt;ses&gt;" in page and "<b>Hata (aşama: audio):</b> voice not found: tr-TR-Yok" in page
    assert "Metin &lt;b&gt;kalın değil&lt;/b&gt;" in page
    assert "<b>Kontrol nedenleri:</b> doğruluk: 1 Y (yanıltıcı)" in page
    assert "<td>Y</td><td>Hepsi pulsardır.</td><td>Hepsi değil.</td>" in page
    assert "<b>Uyarılar:</b> kelime sayısı 86 (hedef 90-100)" in page
    assert "<th>Filtre elemeleri</th><td>yüz 1</td>" in page
    assert "Bu konu henüz üretilmedi." in page and "durum: sürüyor ya da durduruldu" in page


def test_report_uses_state_summary_when_package_failed(tmp_path):
    batch = tmp_path / "batch"
    batch.mkdir()
    item = bp.ItemState(index=1, subject="A", folder="01-a", status="done",
                        quality={"kontrol_gerekli": True, "nedenler": ["paket hazırlanamadı: disk dolu"]})
    state = bp.BatchState(input="a.txt", input_sha256="", preset="r.toml", preset_sha256="", created="", items=[item],
                          finished="2026-11-05T10:00:00")
    page = bpk.write_report(state, batch).read_text(encoding="utf-8")
    assert "kontrol gerekli</span>" in page and "paket hazırlanamadı: disk dolu" in page
    assert "durum: tamamlandı" in page
