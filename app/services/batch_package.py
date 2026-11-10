"""
Toplu üretimin yükleme paketi, kalite kontrolü ve raporu（见 batch_production）。

每个视频文件夹：video.mp4、paylasim.md、kapak.jpg、credits.txt（有 NASA 或后备素材时）、
script.txt、kalite.json、kareler.jpg。rapor.html 汇总全部视频，离线打开，图片用相对路径。
"""

from __future__ import annotations

import ast
import html
import io
import json
import math
import os
import re
import shutil
import subprocess
from pathlib import Path

import numpy as np
from loguru import logger
from PIL import Image, ImageDraw

from app.services import batch_subtitles, llm, nasa, task_materials, video
from app.utils import ffmpeg_inputs, utils

# Süre ölçümü MoviePy'nin ffmpeg_parse_infos'unu kullanır; onun girdileri de yalnız yerel dosya açabilsin.
ffmpeg_inputs.guard_libraries()

RATINGS = ("D", "B", "Y", "X")
RATING_LABELS = {"D": "doğru", "B": "belirsiz/basitleştirilmiş", "Y": "yanıltıcı", "X": "yanlış"}
FLAGGED_RATINGS = ("Y", "X")
COVER_SIZE = (1080, 1920)
SHEET_FRAMES = 5
PIXABAY_CREDIT = "Ek görüntüler: Pixabay (pixabay.com)"
# Paylaşım metninde stok kaynakların adı ve sitesi (credit_lines).
STOCK_CREDITS = {"pixabay": "Pixabay (pixabay.com)", "coverr": "Coverr (coverr.co)",
                 "pexels": "Pexels (pexels.com)"}
# YouTube Shorts başlığı ≤ 100 karakter; TikTok ve Instagram açıklaması (hashtag'ler ve kredi
# satırı dahil) ≤ 2200 karakter; Instagram'da en çok 30 hashtag.
YOUTUBE_TITLE_MAX = 100
YOUTUBE_DESCRIPTION_MAX = 5000
CAPTION_MAX = 2200
HASHTAG_COUNTS = {"youtube": 3, "tiktok": 5, "instagram": 8}
# Paylaşım metninde bilgiyi kuruma atfeden ya da onay ima eden ifadeler (SOURCE_ATTRIBUTION_RULES).
_ATTRIBUTION_PATTERN = re.compile(
    r"\b(?:NASA|ESA|JPL)\s*['’]?\s*(?:ya|ye|nın|nin|dan|den)?\s+"
    r"(?:göre|açıkla|sun|onay|diyor|söylüyor|paylaştı)|according to NASA|NASA (?:says|presents)",
    re.IGNORECASE,
)

AUDIT_PROMPT = """
# Role: Skeptical science fact-checker

You did not write the text below and you have no stake in it. Check a Turkish short-video
script claim by claim.

## Rules
1. Split the script into its factual claims; one sentence can hold several. Skip pure
   questions, calls to action and figures of speech that state no fact, but a metaphor that
   implies a wrong fact is a claim.
2. Rate every claim with exactly one letter:
   - "D": correct and stated precisely.
   - "B": simplified, imprecise or not verifiable for you. Whenever you are not sure, use B.
   - "Y": misleading: defensible word for word, but leaves the viewer with a wrong idea or
     drops a qualification that matters.
   - "X": wrong.
   Be skeptical and never give D when you are not sure. Check named objects, numbers, dates
   and events in particular.
3. "reason": one short Turkish sentence (at most 25 words). For B, Y and X say what is wrong
   or uncertain.
4. Respond ONLY with one minified JSON object, no markdown:
{{"claims":[{{"claim":"<the claim, quoted from the script>","rating":"D","reason":"..."}}]}}

## Topic
{subject}

## Script
{script}
""".strip()

SOCIAL_PROMPT = """
# Görev: kısa video paylaşım metinleri

Aşağıdaki Türkçe kısa bilim videosu için YouTube Shorts, TikTok ve Instagram Reels paylaşım
metinleri yaz.

## Kurallar
1. Türkçe yaz. Merak uyandır ama abartma: tık tuzağı ("inanılmaz", "şok", "kimse bilmiyor"),
   büyük harfle bağırma ve metinde olmayan bir iddia yok.
2. Bilgiyi hiçbir kuruma atfetme ve bir kurumun videoyu onayladığını ya da sunduğunu ima etme
   ("NASA'ya göre", "NASA açıkladı", "NASA sunar" ve benzerleri yok).
3. Kaynak ya da kredi satırı yazma; o satır ayrıca eklenecek.
4. youtube.title en çok 90 karakter, youtube.description 2-3 cümle, youtube.tags tam 3 hashtag.
5. tiktok.caption 1-2 cümle, tiktok.hashtags tam 5 hashtag.
6. instagram.caption 2-3 cümle, instagram.hashtags tam 8 hashtag.
7. Hashtag'ler # ile başlar, boşluk içermez ve konuyla ilgilidir.
8. YALNIZCA tek satırlık geçerli bir JSON nesnesi döndür, markdown yok:
{{"youtube":{{"title":"...","description":"...","tags":["#..."]}},"tiktok":{{"caption":"...","hashtags":["#..."]}},"instagram":{{"caption":"...","hashtags":["#..."]}}}}
{reminder}
## Konu
{subject}

## Metin
{script}
""".strip()


# ---------------------------------------------------------------- yardımcılar

def _json_object(response: str) -> dict:
    """LLM yanıtındaki JSON nesnesi (kod çiti ve çevresindeki metin atılır)."""
    text = llm._strip_code_fence(str(response or ""))
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data = llm._extract_embedded_json(text, "{")
    if not isinstance(data, dict):
        raise ValueError("yanıt bir JSON nesnesi değil")
    return data


def _ask(prompt: str) -> str:
    response = llm._generate_response(prompt)
    if not isinstance(response, str) or response.startswith("Error: "):
        raise RuntimeError(str(response)[:300])
    return response


def _media_duration(path: Path) -> float:
    from moviepy.video.io.ffmpeg_reader import ffmpeg_parse_infos

    return float(ffmpeg_parse_infos(str(path)).get("duration") or 0.0)


def _frame(path: Path, seconds: float, size: tuple[int, int] | None = None) -> Image.Image | None:
    """Videodan bir kare (PNG olarak borudan okunur); size verilirse oran korunarak doldurulur ve ortadan kırpılır."""
    filters = []
    if size:
        width, height = size
        filters = ["-vf", f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height}"]
    result = subprocess.run(
        [utils.get_ffmpeg_binary(), "-nostdin", "-hide_banner", "-loglevel", "error", "-ss", f"{max(0.0, seconds):.3f}",
         *ffmpeg_inputs.ffmpeg_input(path), *filters, "-frames:v", "1", "-f", "image2pipe", "-c:v", "png", "-"],
        capture_output=True, check=False, timeout=300,
    )
    if result.returncode != 0 or not result.stdout:
        return None
    with Image.open(io.BytesIO(result.stdout)) as image:
        return image.convert("RGB")


# ---------------------------------------------------------------- doğruluk denetimi

def parse_audit(response: str) -> list[dict]:
    """Denetçi JSON'u → [{"claim", "rating", "reason"}]; geçersiz yanıt ValueError verir."""
    data = _json_object(response)
    claims = data.get("claims")
    if not isinstance(claims, list) or not claims:
        raise ValueError("yanıtta iddia listesi yok")
    parsed = []
    for entry in claims:
        if not isinstance(entry, dict):
            raise ValueError("iddia bir nesne değil")
        rating = str(entry.get("rating") or "").strip().upper()[:1]
        if rating not in RATINGS:
            raise ValueError(f"geçersiz değerlendirme: {entry.get('rating')!r}")
        claim = str(entry.get("claim") or "").strip()
        if not claim:
            raise ValueError("boş iddia")
        parsed.append({"claim": claim, "rating": rating, "reason": str(entry.get("reason") or "").strip()})
    return parsed


def audit_script(subject: str, script: str, ask=_ask) -> dict:
    """Metni yazan istemden bağımsız, şüpheci bir denetçiyle iddia iddia D/B/Y/X değerlendirmesi."""
    prompt = AUDIT_PROMPT.format(subject=subject, script=script)
    last_error = ""
    for _ in range(2):
        try:
            claims = parse_audit(ask(prompt))
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            logger.warning(f"doğruluk denetimi okunamadı: {last_error}")
            continue
        counts = {rating: sum(c["rating"] == rating for c in claims) for rating in RATINGS}
        return {"durum": "tamam", "iddialar": claims, "sayim": counts,
                "isaretli": [c for c in claims if c["rating"] in FLAGGED_RATINGS]}
    return {"durum": "yapılamadı", "hata": last_error, "iddialar": [], "sayim": {}, "isaretli": []}


# ---------------------------------------------------------------- paylaşım metni

def credit_lines(material_sources) -> list[str]:
    """Paylaşım metninin atıf satırları: NASA (kullanım yönergesi), ek stok kaynaklar.

    Coverr'ın ücretsiz (Demo) API katmanı Coverr'ı ve varsa üreticiyi coverr.co bağlantısıyla
    anmayı şart koşar (https://coverr.co/license); Pixabay ve Pexels atıf istemez, kaynak
    yine yazılır (Pexels API yönergesi Pexels'e görünür bağlantı ister)."""
    lines = []
    attribution = nasa.social_attribution(material_sources, "tr-TR")
    if attribution:
        lines.append(attribution)
    providers = [str((record or {}).get("provider") or "").lower() for record in material_sources or []]
    others = []
    for provider in ("pixabay", "coverr", "pexels"):
        if provider not in providers:
            continue
        label = STOCK_CREDITS[provider]
        if provider == "coverr":
            creators = sorted({str(((record or {}).get("creator") or {}).get("name") or "").strip()
                               for record in material_sources or []
                               if str((record or {}).get("provider") or "").lower() == "coverr"} - {""})
            if creators:
                label = f"{label}: {', '.join(creators)}"
        others.append(label)
    if others == [STOCK_CREDITS["pixabay"]]:
        lines.append(PIXABAY_CREDIT)
    elif others:
        lines.append("Ek görüntüler: " + "; ".join(others))
    return lines


def _caption(text: str, hashtags: list[str], credits: list[str], limit: int) -> str:
    """Açıklama + kredi satırları + hashtag'ler, toplam sınırı aşmadan (gerekirse açıklama kısalır)."""
    tail = "\n\n".join(part for part in ("\n".join(credits), " ".join(hashtags)) if part)
    room = limit - len(tail) - (2 if tail else 0)
    body = llm._clamp_text(text, max(0, room))
    return "\n\n".join(part for part in (body, tail) if part)


def social_texts(subject: str, script: str, material_sources, ask=_ask) -> dict:
    """Üç platform için paylaşım metni; atıf yasağına uymayan yanıt bir kez yeniden istenir."""
    credits = credit_lines(material_sources)
    reminder = ""
    data = None
    for _ in range(2):
        try:
            candidate = _json_object(ask(SOCIAL_PROMPT.format(subject=subject, script=script, reminder=reminder)))
        except Exception as exc:
            logger.warning(f"paylaşım metni okunamadı: {type(exc).__name__}: {exc}")
            continue
        if _ATTRIBUTION_PATTERN.search(json.dumps(candidate, ensure_ascii=False)):
            logger.warning("paylaşım metni bilgiyi kuruma atfediyor, yeniden isteniyor")
            reminder = "\nÖnceki yanıt bilgiyi bir kuruma atfetti; 2. kurala kesinlikle uy.\n"
            continue
        data = candidate
        break
    fallback = data is None
    data = data or {}
    youtube, tiktok, instagram = (data.get(key) if isinstance(data.get(key), dict) else {}
                                  for key in ("youtube", "tiktok", "instagram"))
    first_sentence = re.split(r"(?<=[.!?])\s+", script.strip(), maxsplit=1)[0]
    texts = {
        "youtube": {
            "title": llm._clamp_text(youtube.get("title") or subject, YOUTUBE_TITLE_MAX),
            "description": _caption(youtube.get("description") or first_sentence, [], credits, YOUTUBE_DESCRIPTION_MAX),
            "tags": llm._normalize_hashtags(youtube.get("tags") or ["#bilim", "#uzay", "#shorts"], HASHTAG_COUNTS["youtube"]),
        },
    }
    for name, source in (("tiktok", tiktok), ("instagram", instagram)):
        hashtags = llm._normalize_hashtags(source.get("hashtags") or ["#bilim", "#uzay", "#astronomi"],
                                           HASHTAG_COUNTS[name])
        texts[name] = {"caption": _caption(source.get("caption") or first_sentence, hashtags, credits, CAPTION_MAX),
                       "hashtags": hashtags}
    texts["krediler"] = credits
    texts["yedek_metin"] = fallback
    return texts


def social_markdown(subject: str, texts: dict, subtitle_files: list[str] | None = None) -> str:
    youtube = texts["youtube"]
    lines = [
        f"# {subject}", "",
        "## YouTube Shorts", "",
        f"**Başlık** ({len(youtube['title'])}/{YOUTUBE_TITLE_MAX}):", "", youtube["title"], "",
        "**Açıklama:**", "", youtube["description"], "",
        f"**Etiketler:** {' '.join(youtube['tags'])}", "",
    ]
    if subtitle_files:
        lines += [f"**Altyazı dosyaları:** {', '.join(subtitle_files)} (YouTube Studio > Altyazılar > Dosya yükle)", ""]
    lines += [
        "## TikTok", "",
        f"**Açıklama** ({len(texts['tiktok']['caption'])}/{CAPTION_MAX}, hashtag'ler dahil):", "",
        texts["tiktok"]["caption"], "",
        "## Instagram Reels", "",
        f"**Açıklama** ({len(texts['instagram']['caption'])}/{CAPTION_MAX}, hashtag'ler dahil):", "",
        texts["instagram"]["caption"], "",
    ]
    if texts.get("yedek_metin"):
        lines += ["> Not: LLM yanıtı kullanılamadı; metin konudan ve ilk cümleden üretildi, elle düzenleyin.", ""]
    return "\n".join(lines)


# ---------------------------------------------------------------- kapak ve kareler

def _image_score(image: Image.Image) -> float:
    """Keskinlik (Laplace varyansı) × kontrast; çok karanlık ya da çok açık görseller cezalı."""
    gray = np.asarray(image.convert("L").resize((270, 480)), dtype=float)
    laplace = gray[1:-1, 1:-1] * 4 - gray[:-2, 1:-1] - gray[2:, 1:-1] - gray[1:-1, :-2] - gray[1:-1, 2:]
    score = math.log1p(laplace.var()) * gray.std()
    if not 25 <= gray.mean() <= 235:
        score *= 0.3
    return float(score)


def _material_file(record: dict, task_dir: Path) -> Path | None:
    name = str(record.get("local_file") or "")
    for directory in (Path(utils.storage_dir("cache_videos")), task_dir):
        candidate = directory / name
        if name and candidate.is_file():
            return candidate
    return None


COVER_RULE = ("asıl kaynağın klipleri önce (yedek kaynak yalnız asıl kaynaktan aday yoksa); aralarında "
              "altyazısız kaynak karesi en keskin ve en kontrastlı olan")


def choose_cover(material_sources, task_dir: Path, final_video: Path,
                 primary_source: str = "") -> tuple[Image.Image | None, dict]:
    """
    Kapak: kullanılan kliplerin altyazısız kaynak karelerinden biri. Asıl kaynağın (ör. NASA)
    klipleri önce gelir; yedek kaynak (Pixabay) yalnız asıl kaynaktan aday yoksa seçilir, çünkü
    yedek, NASA'nın karşılayamadığı terimlerde devreye girer ve çoğunlukla CGI getirir. NASA görsel
    klibinde ilk kare (hazırlanmış 9:16 kırpımın tamamı), video kliplerinde orta kare 9:16'ya
    kırpılır. Puan keskinlik × kontrast, çok karanlık/açık kare cezalı; eşitlikte videoda önce
    gelen. Kaynak bulunamazsa son videonun orta karesi (altyazılı) kullanılır.
    """
    primary_source = str(primary_source or "").lower()
    best, best_key, info = None, None, {}
    for position, record in enumerate(material_sources or []):
        if not isinstance(record, dict):
            continue
        path = _material_file(record, task_dir)
        if path is None:
            continue
        provider = str(record.get("provider") or "").lower()
        seconds = 0.0 if provider == "nasa" else _media_duration(path) / 2
        image = _frame(path, seconds, COVER_SIZE)
        if image is None:
            continue
        score = _image_score(image)
        key = (not primary_source or provider == primary_source, score)
        if best_key is None or key > best_key:
            best, best_key = image, key
            info = {"kaynak": record.get("asset_id") or record.get("local_file"), "saglayici": record.get("provider"),
                    "terim": record.get("search_term"), "sira": position + 1, "puan": round(score, 1)}
    if best is None:
        best = _frame(final_video, _media_duration(final_video) / 2, COVER_SIZE)
        info = {"kaynak": "son videonun orta karesi (altyazılı)", "puan": None}
    return best, info


def contact_sheet(final_video: Path, target: Path) -> list[float]:
    duration = _media_duration(final_video)
    moments = [round(duration * (index + 0.5) / SHEET_FRAMES, 2) for index in range(SHEET_FRAMES)]
    width, height = 270, 480
    sheet = Image.new("RGB", (width * SHEET_FRAMES, height + 24), "white")
    draw = ImageDraw.Draw(sheet)
    for column, moment in enumerate(moments):
        frame = _frame(final_video, moment)
        if frame is not None:
            sheet.paste(frame.resize((width, height)), (column * width, 24))
        draw.text((column * width + 6, 5), f"{moment:.1f} sn", fill="black")
    sheet.save(target, "JPEG", quality=88)
    return moments


# ---------------------------------------------------------------- teknik kontrol

_SEARCH_LINE = re.compile(r"nasa (?:image|video) search: term=(?P<term>'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\").*?"
                          r"kept=(?P<kept>\d+), skipped=(?P<skipped>\{[^}]*\})")
_FALLBACK_LINE = re.compile(r"material fallback: term=(?P<term>'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"), "
                            r"(?P<primary>\w+)=\d+/(?P<primary_count>\d+) qualified, "
                            r"(?P<fallback>\w+)=\d+/(?P<fallback_count>\d+) qualified, .*using=(?P<using>\w+)")
# Kaynak zincirinin satırı (source_chain): tried=<kaynak>:<aday>/<uygun|->[:429|:skipped],...
_CHAIN_LINE = re.compile(r"material source chain: term=(?P<term>'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"), "
                         r"tried=(?P<tried>\S+), using=(?P<using>\w+), reason=(?P<reason>\w+)")
# nasa.search_images 和 nasa_video.search_videos 的 skipped 计数键 → 报告里的淘汰原因。
_SKIP_REASONS = {
    "person": "kisi", "text_graphic": "yazi_metaveri", "copyright": "telif",
    "third_party_credit": "kredi", "credit": "kredi", "resolution": "cozunurluk",
    "illustration": "cizim", "format": "program",
}


def log_summary(log_lines: list[str], terms: list[str]) -> dict:
    """Görev günlüğünden boş ve yedeğe geçen terimler ile filtre elemeleri."""
    searched, fallback = {}, {}
    eliminations = {"yazi_onizleme": 0, "yuz": 0, "kisi": 0, "yazi_metaveri": 0, "telif": 0,
                    "kredi": 0, "cozunurluk": 0, "diger": 0}
    for line in log_lines:
        if match := _SEARCH_LINE.search(line):
            term = ast.literal_eval(match["term"])
            # NASA görsel ve video araması aynı terim için ayrı satır yazar.
            searched[term] = searched.get(term, 0) + int(match["kept"])
            for reason, count in ast.literal_eval(match["skipped"]).items():
                key = _SKIP_REASONS.get(reason, "diger")
                eliminations[key] = eliminations.get(key, 0) + int(count)
        elif match := _CHAIN_LINE.search(line):
            term = ast.literal_eval(match["term"])
            tried = [part.split(":") for part in match["tried"].split(",")]
            counts = {part[0]: int(part[1].split("/")[0]) for part in tried if len(part) > 1}
            primary = tried[0][0] if tried else ""
            using = match["using"]
            fallback[term] = {"using": using, "fallback": using not in ("None", primary),
                              "count": counts.get(using, 0)}
        elif match := _FALLBACK_LINE.search(line):
            term = ast.literal_eval(match["term"])
            chosen = match["fallback_count"] if match["using"] == match["fallback"] else match["primary_count"]
            fallback[term] = {"using": match["using"], "fallback": match["using"] == match["fallback"],
                              "count": int(chosen)}
        elif line.startswith("nasa text filter: skipped"):
            eliminations["yazi_onizleme"] += 1
        elif line.startswith("nasa face filter: skipped"):
            eliminations["yuz"] += 1
        elif line.startswith("nasa video filter: skipped"):
            eliminations["temiz_bolum"] = eliminations.get("temiz_bolum", 0) + 1
    empty, switched = [], []
    for term in terms:
        if term in fallback:
            if fallback[term]["fallback"]:
                switched.append(term)
            if fallback[term]["count"] == 0:
                empty.append(term)
        elif searched.get(term) == 0:
            empty.append(term)
    return {
        "bos_terimler": empty,
        "yedege_gecen_terimler": switched,
        "filtre_elemeleri": eliminations,
        # Önbellekten gelen aramalar günlüğe yazılmaz; elemeler yalnız yeni aramalar için sayılır.
        "onbellekten_gelen_aramalar": sum(1 for term in terms if term not in searched and term not in fallback),
    }


def _subtitle_text(subtitle_path: Path) -> str:
    if not subtitle_path.is_file():
        return ""
    lines = subtitle_path.read_text(encoding="utf-8-sig").splitlines()
    return " ".join(line for line in lines if line.strip() and "-->" not in line and not line.strip().isdigit())


def missing_glyphs(text: str, font_path: str) -> list[str]:
    characters = sorted({char for char in text if not char.isspace()})
    return [char for char in characters if not video._subtitle_font_supports_sample(font_path, char)]


def credit_problems(material_sources) -> list[str]:
    return [
        f"{record.get('asset_id')}: {record.get('credit')}"
        for record in nasa.used_sources(material_sources)
        if not nasa.free_use_credit(record.get("credit") or "")
    ]


MATERIAL_LABELS = {"pixabay": "Pixabay", "coverr": "Coverr", "pexels": "Pexels"}


def material_summary(material_sources) -> dict:
    """Kullanılan görüntülerin kaynağa ve türe göre sayısı, ilk görülme sırasıyla
    (ör. {"NASA video": 4, "NASA görsel": 2, "Pixabay": 1})."""
    counts: dict = {}
    for record in material_sources or []:
        if not isinstance(record, dict):
            continue
        provider = str(record.get("provider") or "").lower()
        if provider == "nasa":
            label = "NASA video" if record.get("media_type") == "video" else "NASA görsel"
        else:
            label = MATERIAL_LABELS.get(provider, provider or "yerel")
        counts[label] = counts.get(label, 0) + 1
    return counts


def technical_checks(task_dir: Path, final_video: Path, script: str, params: dict, terms: list[str],
                     material_sources, log_lines: list[str], preset) -> dict:
    duration = round(_media_duration(final_video), 1)
    words = len(script.split())
    font_name = params.get("font_name") or "STHeitiMedium.ttc"
    font_path = os.path.join(utils.font_dir(), font_name)
    checks = {
        "sure_sn": duration,
        "sure_hedef": list(preset.target_duration) if preset.target_duration else None,
        "sure_uygun": (preset.target_duration is None
                       or preset.target_duration[0] <= duration <= preset.target_duration[1]),
        "kelime": words,
        "kelime_hedef": list(preset.target_words) if preset.target_words else None,
        "kelime_uygun": preset.target_words is None or preset.target_words[0] <= words <= preset.target_words[1],
        "glifsiz_karakterler": missing_glyphs(_subtitle_text(task_dir / "subtitle.srt") or script, font_path),
        "yazi_tipi": font_name,
        "kredi_sorunlari": credit_problems(material_sources),
        "goruntuler": material_summary(material_sources),
    }
    checks.update(log_summary(log_lines, terms))
    return checks


def review_reasons(audit: dict, checks: dict) -> list[str]:
    reasons = []
    if audit.get("durum") != "tamam":
        reasons.append("doğruluk denetimi yapılamadı")
    for rating in FLAGGED_RATINGS:
        if audit.get("sayim", {}).get(rating):
            reasons.append(f"doğruluk: {audit['sayim'][rating]} {rating} ({RATING_LABELS[rating]})")
    if checks.get("glifsiz_karakterler"):
        reasons.append(f"yazı tipinde glifi olmayan karakter: {' '.join(checks['glifsiz_karakterler'])}")
    if checks.get("kredi_sorunlari"):
        reasons.append(f"NASA/izinli ortak dışı kredi: {len(checks['kredi_sorunlari'])}")
    return reasons


def check_warnings(checks: dict) -> list[str]:
    """Hedef dışı süre ve kelime sayısı: raporda uyarı olarak görünür, tek başına kontrol gerektirmez."""
    warnings = []
    if not checks.get("sure_uygun", True):
        low, high = checks["sure_hedef"]
        warnings.append(f"süre {checks['sure_sn']} sn (hedef {low:g}-{high:g})")
    if not checks.get("kelime_uygun", True):
        low, high = checks["kelime_hedef"]
        warnings.append(f"kelime sayısı {checks['kelime']} (hedef {low}-{high})")
    return warnings


# ---------------------------------------------------------------- paket

def _place_video(source: Path, target: Path) -> str:
    if target.exists():
        target.unlink()
    try:
        os.link(source, target)
        return "sabit bağlantı"
    except OSError:
        shutil.copy2(source, target)
        return "kopya"


def build_package(item, result: dict, preset, folder: Path, log_lines: list[str], ask=_ask,
                  review: dict | None = None, revision: dict | None = None) -> dict:
    """
    Bir videonun yükleme paketini ve kalite dosyasını yazar; durum için kısa bir özet döner. review (iki aşamalı
    akış, batch_review.approved_copy): metin yeniden denetlenmez, onaylanan metnin son denetimi kullanılır.
    revision (task_revisions.refresh_package): paket bir revizyon için; altyazı, kaynaklar, ayarlar ve denetim
    revizyondan gelir (metin değişmediyse özgün paketin denetimi), metin yeniden denetlenmez.
    """
    folder.mkdir(parents=True, exist_ok=True)
    final_video = Path(result["videos"][0])
    task_dir = Path(utils.task_dir(item.task_id))
    data = json.loads((task_dir / "script.json").read_text(encoding="utf-8"))
    script = str(result.get("script") or data.get("script") or "").strip()
    terms = list(result.get("terms") or data.get("search_terms") or [])
    material_sources = data.get("material_sources") or []
    params = data.get("params") or {}
    subtitle_source = task_dir / "subtitle.srt"
    if revision is not None:
        material_sources = list(revision.get("material_sources") or [])
        params = revision.get("params") or params
        subtitle_source = Path(revision["subtitle"]) if revision.get("subtitle") else folder / ".altyazi-yok.srt"

    placement = _place_video(final_video, folder / "video.mp4")
    (folder / "script.txt").write_text(script + "\n", encoding="utf-8")
    credits = (nasa.credits_text(material_sources) if revision is not None
               else task_materials.credits_text(task_dir))
    if credits:
        # materials.json keeps the records credits.txt is made from (the same text).
        (folder / "credits.txt").write_text(credits, encoding="utf-8", newline="\n")
    elif revision is None and (task_dir / nasa.CREDITS_FILE).is_file():
        shutil.copy2(task_dir / nasa.CREDITS_FILE, folder / "credits.txt")

    # Altyazılar önce: paylasim.md dosya adlarını anar.
    source_language = str(params.get("video_language") or "tr").split("-")[0].lower() or "tr"
    subtitles = batch_subtitles.write_subtitles(
        folder, subtitle_source, utils.normalize_script_for_subtitle_matching(script),
        int(_media_duration(final_video) * 1000), source_language,
        [code for code in preset.subtitle_translations if code.split("-")[0] != source_language], ask,
    )
    texts = social_texts(item.subject, script, material_sources, ask=ask)
    (folder / "paylasim.md").write_text(social_markdown(item.subject, texts, subtitles["dosyalar"]), encoding="utf-8")
    cover, cover_info = choose_cover(material_sources, task_dir, final_video, params.get("video_source", ""))
    if cover is not None:
        cover.save(folder / "kapak.jpg", "JPEG", quality=90)
    moments = contact_sheet(final_video, folder / "kareler.jpg")

    if revision is not None:
        audit = revision.get("audit") or {"durum": "kapalı", "iddialar": [], "sayim": {}, "isaretli": []}
    elif review is not None:
        audit = review.get("audit") or {"durum": "kapalı", "iddialar": [], "sayim": {}, "isaretli": []}
    elif preset.audit:
        audit = audit_script(item.subject, script, ask=ask)
    else:
        audit = {"durum": "kapalı", "iddialar": [], "sayim": {}, "isaretli": []}
    # The glyph check reads <folder>/subtitle.srt: a revision's own folder has one too.
    checks_dir = subtitle_source.parent if revision is not None else task_dir
    checks = technical_checks(checks_dir, final_video, script, params, terms, material_sources, log_lines, preset)
    audited = audit.get("durum") != "kapalı"
    reasons = review_reasons(audit, checks) if audited else review_reasons({"durum": "tamam"}, checks)
    warnings = check_warnings(checks) + subtitles["uyarilar"]
    quality = {
        "konu": item.subject,
        "gorev": item.task_id,
        "kontrol_gerekli": bool(reasons),
        "nedenler": reasons,
        "uyarilar": warnings,
        "dogruluk": audit,
        "teknik": checks,
        "altyazi": subtitles,
        "kapak": {**cover_info, "kural": COVER_RULE},
        "kareler_sn": moments,
        "video": placement,
        "paylasim": texts,
        "terimler": terms,
    }
    if review is not None:
        quality["inceleme"] = {key: review.get(key) for key in (
            "approved_at", "approved_despite_flags", "script_edited", "terms_edited", "regenerations")}
        if review.get("approved_despite_flags"):
            reasons.append("Y/X ile yine de onaylandı")
            quality["kontrol_gerekli"] = True
    if revision is not None:
        quality["revizyon"] = revision.get("name")
    (folder / "kalite.json").write_text(json.dumps(quality, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"kontrol_gerekli": quality["kontrol_gerekli"], "nedenler": reasons, "uyarilar": warnings,
            "sure_sn": checks["sure_sn"], "kelime": checks["kelime"], "sayim": audit.get("sayim", {}),
            "altyazilar": subtitles["dosyalar"]}


# ---------------------------------------------------------------- rapor

_STYLE = """
:root { --bg:#f6f7f9; --card:#fff; --ink:#1d2330; --muted:#5d6675; --line:#dde1e7; --warn:#b54708;
        --warn-bg:#fff4e5; --bad:#b42318; --bad-bg:#fdecea; --ok:#067647; --ok-bg:#e7f6ec; }
@media (prefers-color-scheme: dark) { :root { --bg:#14171c; --card:#1d2128; --ink:#e7eaf0; --muted:#9aa3b2;
        --line:#2e343e; --warn:#f5a623; --warn-bg:#3a2a12; --bad:#ff6b5e; --bad-bg:#3a1714; --ok:#4cc38a;
        --ok-bg:#12301f; } }
* { box-sizing:border-box; } body { margin:0; padding:16px; background:var(--bg); color:var(--ink);
  font:15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }
main { max-width:1100px; margin:0 auto; } h1 { font-size:24px; margin:8px 0 4px; }
.summary { display:flex; flex-wrap:wrap; gap:8px; margin:12px 0 20px; }
.stat { background:var(--card); border:1px solid var(--line); border-radius:8px; padding:8px 14px; }
.stat b { display:block; font-size:20px; } .card { background:var(--card); border:1px solid var(--line);
  border-radius:10px; padding:16px; margin:0 0 16px; } .card h2 { margin:0 0 6px; font-size:19px; }
.badge { display:inline-block; border-radius:999px; padding:1px 10px; font-size:13px; font-weight:600; }
.ok { color:var(--ok); background:var(--ok-bg); } .warn { color:var(--warn); background:var(--warn-bg); }
.bad { color:var(--bad); background:var(--bad-bg); } .meta { color:var(--muted); font-size:13px; }
img.sheet { width:100%; height:auto; border-radius:6px; border:1px solid var(--line); }
table { border-collapse:collapse; width:100%; font-size:14px; } td, th { border-bottom:1px solid var(--line);
  padding:4px 6px; text-align:left; vertical-align:top; } pre { white-space:pre-wrap; background:var(--bg);
  border:1px solid var(--line); border-radius:6px; padding:8px; font-size:13px; }
details { margin-top:8px; } summary { cursor:pointer; font-weight:600; } a { color:inherit; }
h3 { font-size:15px; margin:16px 0 4px; } .note { color:var(--warn); }
.row { display:grid; grid-template-columns: 1fr 160px; gap:12px; } img.cover { width:160px; border-radius:6px; }
@media (max-width:640px) { .row { grid-template-columns:1fr; } img.cover { width:120px; } }
"""


def _load_quality(batch_dir: Path, item) -> dict:
    try:
        return json.loads((batch_dir / item.folder / "kalite.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


# kalite.json'daki filtre eleme anahtarlarının rapordaki adları.
ELIMINATION_LABELS = {
    "yazi_onizleme": "önizlemede yazı", "yuz": "yüz", "kisi": "kişi (metaveri)", "yazi_metaveri": "yazı/grafik (metaveri)",
    "telif": "telif", "kredi": "üçüncü taraf kredisi", "cozunurluk": "çözünürlük", "diger": "diğer",
    "cizim": "çizim/canlandırma", "program": "program biçimi (video)", "temiz_bolum": "temiz bölüm yok (video)",
}


def subtitle_files(folder: Path) -> list[str]:
    """Paketteki .srt dosyaları; kaynak dil (Türkçe) önce, sonra çeviriler alfabetik."""
    names = sorted(path.name for path in folder.glob("altyazi.*.srt"))
    return sorted(names, key=lambda name: name != "altyazi.tr.srt")


def _claims_table(claims: list[dict]) -> str:
    esc = html.escape
    rows = "".join(
        f"<tr><td>{esc(c['rating'])}</td><td>{esc(c['claim'])}</td><td>{esc(c['reason'])}</td></tr>" for c in claims
    )
    return f"<table><tr><th>değ.</th><th>iddia</th><th>gerekçe</th></tr>{rows}</table>"


def _item_card(batch_dir: Path, item) -> str:
    esc = html.escape
    # kalite.json yoksa (paket yazılamadıysa) state.json'daki kısa özet kullanılır.
    quality = (_load_quality(batch_dir, item) or item.quality) if item.status == "done" else {}
    waiting_labels = {"awaiting_review": "onay bekliyor", "approved": "onaylandı", "skipped": "atlandı",
                      "preparing": "hazırlanıyor", "rendering": "render ediliyor"}
    if item.status == "failed":
        badge = '<span class="badge bad">başarısız</span>'
    elif item.status != "done":
        badge = f'<span class="badge">{waiting_labels.get(item.status, "bekliyor")}</span>'
    elif quality.get("kontrol_gerekli"):
        badge = '<span class="badge warn">kontrol gerekli</span>'
    else:
        badge = '<span class="badge ok">hazır</span>'
    parts = [f'<section class="card"><h2>{item.index}. {esc(item.subject)} {badge}</h2>']
    meta = [f"üretim {item.seconds:.0f} sn", f"deneme {item.attempts}"]
    if item.task_id:
        meta.append(f"görev {esc(item.task_id)}")
    folder = batch_dir / item.folder
    if folder.is_dir():
        meta.append(f'<a href="{esc(item.folder)}/">{esc(item.folder)}/</a>')
    if (folder / "video.mp4").exists():
        meta.append(f'<a href="{esc(item.folder)}/video.mp4">video.mp4</a>')
    for name in subtitle_files(folder):
        meta.append(f'<a href="{esc(item.folder)}/{esc(name)}">{esc(name)}</a>')
    parts.append(f'<div class="meta">{" · ".join(meta)}</div>')
    if item.status == "failed":
        stage = f" (aşama: {esc(item.failed_stage)})" if item.failed_stage else ""
        parts.append(f"<p><b>Hata{stage}:</b> {esc(item.error)}</p></section>")
        return "".join(parts)
    if item.status == "skipped":
        parts.append("<p>Bu konu atlandı; videosu üretilmeyecek.</p></section>")
        return "".join(parts)
    if item.status != "done":
        parts.append("<p>Bu konu henüz üretilmedi.</p></section>")
        return "".join(parts)
    reasons = quality.get("nedenler", [])
    if reasons:
        parts.append("<p><b>Kontrol nedenleri:</b> " + "; ".join(esc(r) for r in reasons) + "</p>")
    warnings = quality.get("uyarilar", [])
    if warnings:
        parts.append('<p class="note"><b>Uyarılar:</b> ' + "; ".join(esc(w) for w in warnings) + "</p>")
    if "teknik" not in quality:  # paket yazılamadı
        parts.append("</section>")
        return "".join(parts)
    checks = quality["teknik"]
    audit = quality.get("dogruluk", {})
    cover = f'<img class="cover" src="{esc(item.folder)}/kapak.jpg" alt="kapak">' \
        if (batch_dir / item.folder / "kapak.jpg").exists() else ""
    script = (batch_dir / item.folder / "script.txt").read_text(encoding="utf-8") \
        if (batch_dir / item.folder / "script.txt").exists() else ""
    parts.append(f'<div class="row"><div><p>{esc(script)}</p>')
    flagged = [c for c in audit.get("iddialar", []) if c["rating"] in FLAGGED_RATINGS]
    if flagged:
        parts.append("<h3>İşaretli iddialar</h3>" + _claims_table(flagged))
    counts = audit.get("sayim") or {}
    audit_text = (" / ".join(f"{k} {counts.get(k, 0)}" for k in RATINGS) if audit.get("durum") == "tamam"
                  else esc(audit.get("durum", "")))
    elim = checks.get("filtre_elemeleri", {})
    rows = [
        ("Doğruluk (D/B/Y/X)", audit_text),
        ("Video süresi", f"{checks.get('sure_sn')} sn" + (f" (hedef {checks['sure_hedef'][0]:g}-{checks['sure_hedef'][1]:g})" if checks.get("sure_hedef") else "")),
        ("Kelime", f"{checks.get('kelime')}" + (f" (hedef {checks['kelime_hedef'][0]}-{checks['kelime_hedef'][1]})" if checks.get("kelime_hedef") else "")),
        ("Glifsiz karakter", " ".join(checks.get("glifsiz_karakterler", [])) or "yok"),
        ("Kredi sorunu", "; ".join(checks.get("kredi_sorunlari", [])) or "yok"),
        ("Görüntüler", " · ".join(f"{k} {v}" for k, v in (checks.get("goruntuler") or {}).items()) or "yok"),
        ("Boş terim", ", ".join(checks.get("bos_terimler", [])) or "yok"),
        ("Yedeğe geçen terim", ", ".join(checks.get("yedege_gecen_terimler", [])) or "yok"),
        ("Filtre elemeleri", ", ".join(f"{ELIMINATION_LABELS.get(k, k)} {v}" for k, v in elim.items() if v) or "yok"),
        ("Altyazı dosyaları", ", ".join(subtitle_files(batch_dir / item.folder)) or "yok"),
    ]
    parts.append("<h3>Kontroller</h3><table>"
                 + "".join(f"<tr><th>{esc(k)}</th><td>{esc(str(v))}</td></tr>" for k, v in rows) + "</table>")
    parts.append(f"</div><div>{cover}</div></div>")
    if (batch_dir / item.folder / "kareler.jpg").exists():
        parts.append(f'<p><img class="sheet" src="{esc(item.folder)}/kareler.jpg" alt="kareler"></p>')
    if audit.get("iddialar"):
        parts.append(f"<details><summary>Bütün iddialar</summary>{_claims_table(audit['iddialar'])}</details>")
    share = batch_dir / item.folder / "paylasim.md"
    if share.exists():
        parts.append(f"<details><summary>Paylaşım metni</summary><pre>{esc(share.read_text(encoding='utf-8'))}</pre></details>")
    parts.append("</section>")
    return "".join(parts)


def _priority(item) -> tuple:
    """Başarısızlar, sonra kontrol gerekenler, sonra hazırlar, en sonda bekleyenler."""
    if item.status == "failed":
        return (0, item.index)
    if item.status == "done":
        return (1 if item.quality.get("kontrol_gerekli") else 2, item.index)
    return (4 if item.status == "skipped" else 3, item.index)


def write_report(state, batch_dir: Path) -> Path:
    items = state.items
    done = [i for i in items if i.status == "done"]
    failed = [i for i in items if i.status == "failed"]
    review = [i for i in done if i.quality.get("kontrol_gerekli")]
    skipped = [i for i in items if i.status == "skipped"]
    minutes, seconds = divmod(int(state.run_seconds), 60)
    stats = [("Toplam", len(items)), ("Başarılı", len(done)), ("Başarısız", len(failed)),
             ("Kontrol gerekli", len(review)), ("Bekleyen", len(items) - len(done) - len(failed) - len(skipped)),
             *([("Atlanan", len(skipped))] if skipped else []), ("Toplam süre", f"{minutes} dk {seconds} sn")]
    status = "tamamlandı" if state.finished else "sürüyor ya da durduruldu"
    cards = "".join(_item_card(batch_dir, item) for item in sorted(items, key=_priority))
    document = f"""<!doctype html>
<html lang="tr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Toplu üretim raporu</title><style>{_STYLE}</style></head><body><main>
<h1>Toplu üretim raporu</h1>
<div class="meta">{html.escape(Path(state.input).name)} · reçete {html.escape(Path(state.preset).name)} ·
başlangıç {html.escape(state.created)} · durum: {status}</div>
<div class="summary">{"".join(f'<div class="stat"><b>{html.escape(str(v))}</b>{html.escape(k)}</div>' for k, v in stats)}</div>
{cards}
</main></body></html>
"""
    target = batch_dir / "rapor.html"
    target.write_text(document, encoding="utf-8")
    return target
