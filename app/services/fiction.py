"""
Kurgu kanalı ("Ya olsaydı"): sahne planı, Veo istem paketi, klip alımı ve doğrulama, ai_manual kaynağı.

Akış (iki aşamalı toplu üretimde, reçetede ``[script] content = "kurgu"`` ve ``[video] video_source =
"ai_manual"``):

1. Hazırlık: metin + denetimden sonra sahne planı (``build_plan``). Sahne sayısı tahmini seslendirme süresinden
   (``batch_review.estimate``'in harf formülü) ve klip süresinden (8 sn); metin cümle sınırlarından sahnelere
   bölünür. Tek LLM çağrısı video başına bir **stil kitabı** (görsel dil, ışık, renk, dönem, tekrarlayan nesneler,
   bilimsel makullük) ve sahne başına Türkçe tarif + İngilizce çekim tarifi verir; Veo istemi kodla kurulur:
   stil kitabı + sahnenin çekimi + sabit kurallar (``FIXED_RULES``: 9:16, 8 sn, tek çekim; yazı, logo, amblem,
   gerçek kişi yok).
2. Konu klasörüne istem paketi: ``istem-paketi.html`` (çevrimdışı, kopyala düğmeleri, durum),
   ``istemler.md``, ``sahne-plani.json``.
3. Onaydan sonra klipler: WebUI'dan sahne kartına yükleme ya da ``gelen/`` klasörüne bırakma + "Kontrol et".
   Dosya adındaki sahne numarası eşleşmeyi verir; numarasız (ya da belirsiz) dosyaları kullanıcı eşleştirir.
   Doğrulama (``validate_clip``): video akışı okunur, süre ≥ klip süresi (kısa klip uzatılmaz), kısa kenar
   ≥ 720; 9:16 olmayan klip uyarıyla sığdırılır (reçetenin ``video_fit_mode``'u), ses her zaman atılır.
   Durumlar ``klipler.json``'da: bekliyor / geldi / uygun değil (neden).
4. Bütün sahneler gelince render: ``render-plani.json`` (dosyalar ve SHA-256'ları) göreve ``--ai-manual-plan``
   ile verilir; görev klipleri kendi klasörüne kopyalar (kullanıcının dosyası sonradan değişebilir), her sahneye
   eşit süre verir (ses / sahne sayısı ≤ klip süresi) ve kaynak kaydına ``ai_manual`` + araç, istem, stil
   kitabı sürümü, SHA-256, yüklenme zamanı ve "Canlandırma · yapay zekâ" etiketini yazar.
"""

from __future__ import annotations

import hashlib
import html
import json
import math
import os
import re
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from loguru import logger

from app.utils import ffmpeg_inputs, utils

ffmpeg_inputs.guard_libraries()

PROVIDER = "ai_manual"
TOOL = "Google Flow / Veo"
SCENE_LABEL = {"tr": "Canlandırma · yapay zekâ", "en": "Animation · AI-generated"}
CONTENT = "kurgu"
PLAN_FILE = "sahne-plani.json"
PROMPTS_FILE = "istemler.md"
PACKAGE_FILE = "istem-paketi.html"
CLIPS_FILE = "klipler.json"
RENDER_PLAN_FILE = "render-plani.json"
INBOX = "gelen"
PLAN_VERSION = 2  # 2: scenes split by the estimated narration (narration_groups), "timing": "narration"
# 2: compact style book, per-scene "elements" (E2E: 4000-character prompts). 3: rules from the first real Veo
# video (a US flag on a suit although "no flags" was asked; Earth 15x too large; a lit "night"): positive,
# plain descriptions without the words flag/patch, people far or from behind, Earth's size and phase, night lights.
# 4: day and night lighting in the style book, a scene gets only its own ("Single harsh white sunlight", sent with
# every clip, outweighed the night scene's light: helyum-3 scene 6 came out lit like day twice); the sky (Earth's
# size, place and phase) defined once in the style book, the same in every clip; every scene has a "time".
PLAN_PROMPT_VERSION = 4
# Words the video model draws when it reads them, even negated ("no flags"): removed from the LLM's fields.
BANNED_PROMPT_WORDS = re.compile(
    r"(?:\s*,\s*|\s+(?:and|or|nor|with)\s+)no\s+(?:flags?|patch(?:es)?)\b"  # "…visor and no patches"
    r"|\bno\s+(?:flags?|patch(?:es)?)\b(?:\s*(?:,|\bor\b|\band\b|\bnor\b))?\s*"  # "No flags or …"
    r"|(?:\s*,\s*|\s+(?:or|and|nor)\s+)?\b(?:flags?|patch(?:es)?)\b", re.IGNORECASE)  # "…insignia or flags"
# Anlatıma göre sahne süreleri. Planlama: cümle sınırlarından, sahne başına tahmini anlatım ≤ MAX_NARRATION_SECONDS
# (Edge TR, EmelNeural 1.0, helyum-3 ölçümü: konuşma ≈ 0,066 sn/harf, cümleler arası duraklama 1,1–1,5 sn).
# Render: sahne geçişleri TTS'in gerçek zamanlarına (altyazı) hizalanır; anlatım klipten uzunsa en çok
# MAX_SLOWDOWN kat yavaşlatılır, klip uzunsa kesilir.
SPEECH_SECONDS_PER_LETTER = 0.066
SENTENCE_GAP_SECONDS = 1.3
MAX_NARRATION_SECONDS = 7.5
MAX_SLOWDOWN = 1.2
TIMING_NARRATION, TIMING_EQUAL = "narration", "equal"
TIMING_FILE = "zamanlama.json"
RENDER_TAIL_SECONDS = 0.1  # combine_videos sesten 0,1 sn uzun video ister
DEFAULT_CLIP_SECONDS = 8
# Sahne sayısı tahmini süreye bu payla hesaplanır: Edge TR tahmininin en büyük sapması 3,1 sn (batch_review).
SCENE_MARGIN_SECONDS = 3.0
MIN_SHORT_SIDE = 720
PORTRAIT_RATIO = 9 / 16
RATIO_TOLERANCE = 0.03
DURATION_TOLERANCE = 0.1
VIDEO_SUFFIXES = (".mp4", ".mov", ".m4v", ".webm", ".mkv")
WAITING, ARRIVED, UNSUITABLE = "bekliyor", "geldi", "uygun_degil"
# Klip taraması (fiction_screen): kontrol = bayrak/arma/logo/yazı/yüz; render "yine de kullan" onayı ister.
SCREEN_CLEAN, SCREEN_CHECK, SCREEN_WARN, SCREEN_UNSCREENED = "temiz", "kontrol", "uyari", "taranamadi"
SCREEN_TEXT = {SCREEN_CLEAN: "temiz", SCREEN_CHECK: "kontrol gerekli", SCREEN_WARN: "uyarı",
               SCREEN_UNSCREENED: "taranamadı"}
# Flow'un görünür Veo filigranı sağ alt köşede (kesir: x0, y0, x1, y1). Altyazı bu bölgenin üstünde
# (kurgu reçetesi: özel konum %85), sahne etiketi sol üstte kalmalı; test_fiction yerleşimi denetler.
VEO_WATERMARK_BOX = (0.78, 0.92, 1.0, 1.0)
FIXED_RULES = ("Vertical 9:16 frame, {seconds} seconds, one continuous shot with no cuts. No text, no captions, "
               "no logos, no insignia, no watermarks, no real people, no recognizable faces.")

PLAN_PROMPT = """
# Role: storyboard writer for AI-generated video clips (Google Veo)

A Turkish "what if" science video is narrated over {count} clips of {seconds} seconds each, one clip per
scene, vertical 9:16. Write ONE style book shared by every clip and, for every scene, what its clip shows.

## Rules
1. Style book: the visual language, the day lighting and the night lighting, the sky, the color palette, era and
   setting, the recurring objects with one fixed description each (e.g. a mining rover, the base modules) and
   the physical plausibility of the setting (e.g. on the Moon: black sky, no atmosphere, no haze or clouds, low
   gravity). Every clip must look like the same world. Keep it compact, it opens every prompt: one or two
   sentences per field, at most 5 recurring objects with one sentence each.
2. Every scene is ONE continuous {seconds}-second shot (no cuts) with its own camera movement and light, and
   shows what the narration of that scene is about. It is clearly an animated dramatization, not documentary
   footage. "time": "day" or "night" (when the shot happens). "elements": the names of the recurring objects
   seen in that shot (only their descriptions go into its prompt).
3. Nothing on screen may be text, captions or numbers, logos or insignia (no space agency emblem), and no real
   or recognizable people. Describe what IS there instead of what is not: suits are plain, unmarked
   white-and-grey suits with blank shoulders, chest and backpack; vehicles, modules and machines have bare,
   unmarked panels. Never write the words "flag", "flags", "patch" or "patches", not even negated: naming them
   makes the video model draw them.
4. People appear only as small figures far from the camera or seen from behind, in bulky suits with opaque
   mirrored gold visors; never a close-up of a face or of the front of a visor.
5. The style book's "sky" fixes, once for the whole video, every celestial body seen in the sky: its numeric
   apparent size, its place and its phase. Every clip gets this same text; scenes never restate or change them
   (an action may only say whether Earth is in view). From the Moon, Earth is a small disc
   about two degrees wide (about four times the full Moon seen from Earth), never filling the sky, fixed in the
   same place. Pick one phase that fits every scene, its lit side facing the sun: with day and night scenes,
   set the days at a low sun near the horizon and Earth half lit; with only night scenes Earth is nearly full;
   with only midday scenes a thin crescent. Stars only in night scenes.
6. Lighting: "lighting_day" is the light of the day scenes (e.g. a low harsh sun, hard black shadows);
   "lighting_night" the light of the night scenes: name every source (earthshine, floodlights, lit windows,
   helmet lamps), say there is no sunlight and there are no sun shadows, and let the ground beyond the lights
   fall to near-black. A clip gets only the lighting of its own time, so the night lighting never mentions the
   sun except to say it is absent. Leave a field empty when no scene needs it. A scene's "light" adds only what
   is particular to that shot.
7. "visual_tr": one or two Turkish sentences for the user (what the clip shows). "action", "camera", "light":
   concrete English for the video model (what is seen, how the camera moves, the light); no story, no narration.
8. Respond ONLY with one minified JSON object, no markdown:
{{"style_book":{{"visual_language":"...","lighting_day":"...","lighting_night":"...","sky":"...","color":"...","era":"...","recurring":[{{"name":"...","description":"..."}}],"science":"..."}},"scenes":[{{"index":1,"time":"day","visual_tr":"...","action":"...","camera":"...","light":"...","elements":["..."]}}]}}
{style_hint}
## Topic
{subject}

## Narration by scene
{scenes}
""".strip()

# "lighting": one lighting for every clip (plans before PLAN_PROMPT_VERSION 4; kept as written, their prompts and
# style versions do not change). Since 4: "lighting_day" / "lighting_night", a clip gets its scene's ("time").
STYLE_FIELDS = ("visual_language", "lighting", "lighting_day", "lighting_night", "sky", "color", "era", "science")
DAY, NIGHT = "day", "night"
TIMES = (DAY, NIGHT)
# A scene without "time" (an older plan, a missing answer): night when its own light or action says so.
NIGHT_WORDS = re.compile(r"\bnight\b|\bno sunlight\b", re.IGNORECASE)


class FictionError(RuntimeError):
    """Kurgu kanalının bir kuralı sağlanmadı (plan, klip, render); mesaj kullanıcıya gösterilir."""


@dataclass(frozen=True)
class Settings:
    """Reçetenin [fiction] bölümü."""

    clip_seconds: int = DEFAULT_CLIP_SECONDS
    tool: str = TOOL
    scene_label: Any = field(default_factory=lambda: dict(SCENE_LABEL))
    style: str = ""  # stil kitabı için yön (ör. "gerçekçi bilimkurgu, 2040'lar"); boşsa LLM seçer
    # Yüklenen klibin görsel taraması (fiction_screen): model ve yedeği vision_screen biçiminde.
    screening: bool = True
    screening_model: str = "gemini:gemini-3.5-flash-lite"
    screening_fallback: str = "claude:sonnet"


# ---------------------------------------------------------------- sahne planı


def scene_count(estimated_seconds: float, clip_seconds: int = DEFAULT_CLIP_SECONDS) -> int:
    """Tahmini seslendirme süresini (payıyla) karşılayan en az klip sayısı."""
    return max(1, math.ceil((max(0.0, float(estimated_seconds)) + SCENE_MARGIN_SECONDS) / max(1, clip_seconds)))


def sentences(script: str) -> list[str]:
    text = re.sub(r"\s+", " ", str(script or "")).strip()
    return [part.strip() for part in re.split(r"(?<=[.!?…])\s+", text) if part.strip()]


def _split_once(piece: str) -> list[str]:
    """Uzun bir parçayı ortasına en yakın virgülden, yoksa kelime sınırından ikiye böler."""
    middle = len(piece) / 2
    commas = [match.end() for match in re.finditer(r"[,;:]\s", piece)]
    spaces = [match.start() for match in re.finditer(r"\s", piece)]
    cut = min(commas, key=lambda at: abs(at - middle)) if commas else (
        min(spaces, key=lambda at: abs(at - middle)) if spaces else 0)
    if not 0 < cut < len(piece):
        return [piece]
    return [piece[:cut].strip(), piece[cut:].strip()]


def split_narration(script: str, count: int) -> list[str]:
    """
    Metni sırasını koruyarak ``count`` parçaya böler: cümle sınırları, gerekirse uzun cümle virgülden ya da
    kelime sınırından. Parça uzunlukları (harf) dengelenir (en uzun parçayı en kısa tutan bölüm).
    """
    pieces = sentences(script)
    while 0 < len(pieces) < count:
        longest = max(range(len(pieces)), key=lambda i: len(pieces[i]))
        parts = _split_once(pieces[longest])
        if len(parts) == 1:
            break
        pieces[longest:longest + 1] = parts
    count = min(count, len(pieces))
    if count <= 0:
        return []
    lengths = [len(piece) for piece in pieces]
    prefix = [0]
    for length in lengths:
        prefix.append(prefix[-1] + length)
    # best[k][i]: ilk i parçayı k gruba bölmenin en kısa "en uzun grubu"; cut: son grubun başlangıcı.
    inf = float("inf")
    best = [[inf] * (len(pieces) + 1) for _ in range(count + 1)]
    cut = [[0] * (len(pieces) + 1) for _ in range(count + 1)]
    best[0][0] = 0
    for k in range(1, count + 1):
        for i in range(k, len(pieces) + 1):
            for j in range(k - 1, i):
                value = max(best[k - 1][j], prefix[i] - prefix[j])
                if value < best[k][i]:
                    best[k][i], cut[k][i] = value, j
    groups, end = [], len(pieces)
    for k in range(count, 0, -1):
        start = cut[k][end]
        groups.append(" ".join(pieces[start:end]))
        end = start
    return list(reversed(groups))


def estimate_narration(text: str) -> float:
    """Tahmini anlatım süresi (ilk kelimeden son kelimeye): harf × hız + cümleler arası duraklamalar."""
    parts = sentences(text) or [str(text or "")]
    letters = sum(len(re.sub(r"\s", "", part)) for part in parts)
    return round(letters * SPEECH_SECONDS_PER_LETTER + SENTENCE_GAP_SECONDS * max(0, len(parts) - 1), 2)


def narration_groups(script: str, limit: float = MAX_NARRATION_SECONDS) -> list[str]:
    """
    Metni cümle sınırlarından sahnelere böler: her sahnenin tahmini anlatımı ≤ ``limit`` (tek başına daha uzun
    cümle kendi sahnesidir, ``timing_warnings`` uyarır). Sahne sayısı bu sınırla en az olandır; gruplar en uzun
    sahneyi en kısa tutacak biçimde dengelenir.
    """
    pieces = sentences(script)
    if not pieces:
        return []
    seconds = [estimate_narration(piece) for piece in pieces]

    def cost(start: int, end: int) -> float:  # pieces[start:end] tek sahnede
        return sum(seconds[start:end]) + SENTENCE_GAP_SECONDS * (end - start - 1)

    count, start = 0, 0
    while start < len(pieces):  # açgözlü: sınırla en az sahne
        end = start + 1
        while end < len(pieces) and cost(start, end + 1) <= limit:
            end += 1
        count, start = count + 1, end
    inf = float("inf")
    best = [[inf] * (len(pieces) + 1) for _ in range(count + 1)]
    cut = [[0] * (len(pieces) + 1) for _ in range(count + 1)]
    best[0][0] = 0.0
    for k in range(1, count + 1):
        for i in range(k, len(pieces) + 1):
            for j in range(k - 1, i):
                group = cost(j, i)
                if i - j > 1 and group > limit:
                    continue  # birden çok cümle sınırı aşamaz
                value = max(best[k - 1][j], group)
                if value < best[k][i]:
                    best[k][i], cut[k][i] = value, j
    groups, end = [], len(pieces)
    for k in range(count, 0, -1):
        begin = cut[k][end]
        groups.append(" ".join(pieces[begin:end]))
        end = begin
    return list(reversed(groups))


def timing_warnings(plan: dict) -> list[str]:
    """
    Render öncesi uyarılar (inceleme ve klip panelleri): tahmini anlatımı sınırı aşan sahne ve ekranda kalacağı
    süre (anlatım + duraklama) klip × MAX_SLOWDOWN'ı aşıyorsa "metni bölün".
    """
    warnings = []
    cap = float(plan.get("clip_seconds") or DEFAULT_CLIP_SECONDS) * MAX_SLOWDOWN
    for scene in plan.get("scenes") or []:
        estimate = estimate_narration(scene.get("narration", ""))
        if estimate <= MAX_NARRATION_SECONDS:
            continue
        screen = estimate + SENTENCE_GAP_SECONDS
        text = (f"sahne {scene['index']:02d}: anlatım ~{estimate:.1f} sn ({MAX_NARRATION_SECONDS:g} sn sınırından uzun; "
                f"tek cümle)" if len(sentences(scene.get("narration", ""))) <= 1
                else f"sahne {scene['index']:02d}: anlatım ~{estimate:.1f} sn ({MAX_NARRATION_SECONDS:g} sn sınırından uzun)")
        if screen > cap:
            text += (f"; ekranda ~{screen:.1f} sn kalmalı, klip {cap / MAX_SLOWDOWN:g} sn × {MAX_SLOWDOWN:g} = {cap:.1f} sn: "
                     "sonraki sahne erken başlar, metni bölebilirsiniz")
        else:
            text += f"; klip ~{screen / (cap / MAX_SLOWDOWN):.2f}× yavaşlatılır"
        warnings.append(text)
    return warnings


def file_name(index: int) -> str:
    return f"sahne-{index:02d}.mp4"


def timed_lighting(style: dict | None) -> bool:
    """Stil kitabında gündüz/gece ışığı ayrı mı (istem sürümü 4'ten beri); değilse tek "lighting" her klibe."""
    style = style or {}
    return any(str(style.get(f"lighting_{time}") or "").strip() for time in TIMES)


def style_fields(style: dict | None) -> tuple[str, ...]:
    """Bu stil kitabının düzenlenen metin alanları: eski planda tek ışık, yenisinde gündüz ve gece ışığı."""
    style = style or {}
    if str(style.get("lighting") or "").strip() and not timed_lighting(style):
        lighting = ("lighting",)
    else:
        lighting = ("lighting_day", "lighting_night")
    return ("visual_language", *lighting, "sky", "color", "era", "science")


def scene_time(scene: dict) -> str:
    """Sahnenin zamanı ("day" / "night"): plandaki, yoksa çekimin kendi ışığından ya da eyleminden."""
    time = str(scene.get("time") or "").strip().lower()
    if time in TIMES:
        return time
    return NIGHT if NIGHT_WORDS.search(f"{scene.get('light') or ''} {scene.get('action') or ''}") else DAY


def style_text(style: dict, elements: list | None = None, time: str | None = None) -> str:
    """
    Her istemin başına eklenen stil kitabı metni (İngilizce). ``elements`` verilirse tekrarlayan öğelerden
    yalnız o çekimde görünenlerin tarifi girer (istem kısa kalır; Veo istemi ~1024 token); None: hepsi.
    ``time`` ("day" / "night") verilirse gündüz/gece ışığından yalnız o zamanınki girer (gece sahnesine güneş
    cümlesi gitmez); None: ikisi de (paket, stil kitabı sürümü). Eski tek "lighting" her zaman girer.
    """
    style = style or {}
    wanted = None if elements is None else {str(name).strip().casefold() for name in elements}
    timed = timed_lighting(style)
    # Plans before version 4 keep their exact text (the label, the order): their prompts and style versions stay.
    label = "Style book (shared by every clip)" if timed else "Style book (identical in every clip)"
    parts = [f"{label}: {style.get('visual_language', '').strip()}".rstrip(": ")]
    fields = [("lighting", "Lighting")] if not timed else \
        [(f"lighting_{t}", f"{t.capitalize()} lighting") for t in TIMES if time is None or t == time]
    fields += [("sky", "Sky (the same in every clip)"), ("color", "Color palette"), ("era", "Era and setting")]
    for key, label in fields:
        if str(style.get(key) or "").strip():
            parts.append(f"{label}: {str(style[key]).strip()}")
    recurring = [f"{str(item.get('name') or '').strip()}: {str(item.get('description') or '').strip()}"
                 for item in style.get("recurring") or [] if isinstance(item, dict) and item.get("name")
                 and (wanted is None or str(item["name"]).strip().casefold() in wanted)]
    if recurring:
        parts.append("Recurring elements, always the same: " + "; ".join(recurring))
    if str(style.get("science") or "").strip():
        parts.append(f"Physical plausibility: {str(style['science']).strip()}")
    return ". ".join(part.rstrip(". ") for part in parts) + "."


def style_version(style: dict) -> str:
    return hashlib.sha256(style_text(style).encode("utf-8")).hexdigest()[:10]


def compose_prompt(style: dict, scene: dict, count: int, seconds: int) -> str:
    """Veo istemi: stil kitabı + sahnenin çekimi (eylem, kamera, ışık) + sabit kurallar."""
    shot = [f"Scene {scene['index']} of {count}: {str(scene.get('action') or '').strip()}"]
    if str(scene.get("camera") or "").strip():
        shot.append(f"Camera: {str(scene['camera']).strip()}")
    if str(scene.get("light") or "").strip():
        shot.append(f"Lighting: {str(scene['light']).strip()}")
    elements = scene.get("elements") if isinstance(scene.get("elements"), list) else None
    return "\n\n".join([style_text(style, elements, scene_time(scene)),
                        "\n".join(line.rstrip(".") + "." for line in shot), FIXED_RULES.format(seconds=seconds)])


def _json_object(text: str) -> dict:
    from app.services import llm

    text = llm._strip_code_fence(str(text or ""))
    try:
        data = json.loads(text)
    except ValueError:
        data = llm._extract_embedded_json(text, "{")
    if not isinstance(data, dict):
        raise ValueError("yanıt bir JSON nesnesi değil")
    return data


def _clip(text: Any, limit: int) -> str:
    """Metni sınırda keser; kesmek gerekirse son tam cümlede (kelime ortasında değil)."""
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    end = max(cut.rfind(". "), cut.rfind("; "))
    return (cut[:end + 1] if end > limit // 2 else cut[:cut.rfind(" ")]).strip()


def _clean_style(raw: Any) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    style = {key: _clip(raw.get(key), 700) for key in STYLE_FIELDS}
    style["recurring"] = [{"name": _clip(item.get("name"), 80), "description": _clip(item.get("description"), 300)}
                          for item in raw.get("recurring") or [] if isinstance(item, dict) and item.get("name")][:6]
    if not style["visual_language"]:
        raise ValueError("stil kitabında görsel dil yok")
    return style


def scrub_prompt_words(text: str) -> str:
    """LLM'in yazdığı istem alanından "flag/patch" kelimelerini (olumsuzlarıyla) çıkarır; son çare (kural 3)."""
    cleaned = BANNED_PROMPT_WORDS.sub("", str(text or ""))
    if cleaned == str(text or ""):
        return cleaned
    cleaned = re.sub(r"\s+([,.;:])", r"\1", cleaned)
    cleaned = re.sub(r"([,;:])\s*([.;:])", r"\2", cleaned)
    return re.sub(r"\s{2,}", " ", cleaned).strip()


def _scrub_plan(style: dict, shots: list[dict]) -> tuple[dict, list[dict]]:
    before = json.dumps([style, shots], ensure_ascii=False)
    style = {key: (scrub_prompt_words(value) if isinstance(value, str) else value) for key, value in style.items()}
    style["recurring"] = [{**item, "description": scrub_prompt_words(item.get("description", ""))}
                          for item in style.get("recurring") or []]
    shots = [{**shot, **{key: scrub_prompt_words(shot.get(key, "")) for key in ("action", "camera", "light")}}
             for shot in shots]
    if json.dumps([style, shots], ensure_ascii=False) != before:
        logger.warning("sahne planı: istemlerden \"flag/patch\" kelimeleri çıkarıldı (Veo onları çizer)")
    return style, shots


def parse_plan_answer(text: str, count: int) -> tuple[dict, list[dict]]:
    """LLM yanıtı → (stil kitabı, sahneler); sahne sayısı tutmazsa ValueError."""
    data = _json_object(text)
    style = _clean_style(data.get("style_book"))
    scenes = data.get("scenes")
    if not isinstance(scenes, list) or len(scenes) != count:
        raise ValueError(f"{count} sahne bekleniyordu, {len(scenes) if isinstance(scenes, list) else 0} geldi")
    cleaned = []
    for position, scene in enumerate(scenes, start=1):
        if not isinstance(scene, dict) or not str(scene.get("action") or "").strip():
            raise ValueError(f"sahne {position}: çekim tarifi yok")
        shot = {key: str(scene.get(key) or "").strip()[:600] for key in ("visual_tr", "action", "camera", "light")}
        shot["time"] = scene_time({**shot, "time": scene.get("time")})
        if isinstance(scene.get("elements"), list):
            names = {item["name"].casefold(): item["name"] for item in style["recurring"]}
            shot["elements"] = [names[str(name).strip().casefold()] for name in scene["elements"]
                                if str(name).strip().casefold() in names]
        cleaned.append(shot)
    return _scrub_plan(style, cleaned)


def _ask(prompt: str) -> str:
    from app.services import llm

    response = llm._generate_response(prompt)
    if not isinstance(response, str) or response.startswith("Error: "):
        raise RuntimeError(str(response)[:300])
    return response


def build_plan(subject: str, script: str, estimated_seconds: float, settings: Settings | None = None,
               ask: Callable[[str], str] = _ask, style_book: dict | None = None, previous: dict | None = None) -> dict:
    """
    Metnin sahne planı. ``style_book`` verilirse (kullanıcının düzenlediği) olduğu gibi kullanılır. ``previous``
    (eski plan) verilirse elle düzenlenmiş istemler aynı sıradaki sahnede korunur (metin değişince yeniden üretim).
    """
    from app.services import batch_review

    settings = settings or Settings()
    # Sahneler cümle sınırlarından, tahmini anlatıma göre (sahne başına ≤ MAX_NARRATION_SECONDS); 1. sürüm
    # sahne sayısını toplam tahminden çıkarıp metni eşit harfli parçalara bölüyordu (scene_count, split_narration).
    narration = narration_groups(script)
    count = len(narration)
    if not count:
        raise FictionError("metin boş: sahne planı kurulamaz")
    hint = f"\n## Style direction\n{settings.style.strip()}\n" if settings.style.strip() else ""
    if style_book:
        hint += ("\n## Fixed style book (the user's; return it unchanged)\n"
                 + json.dumps(style_book, ensure_ascii=False) + "\n")
    prompt = PLAN_PROMPT.format(count=count, seconds=settings.clip_seconds, subject=subject, style_hint=hint,
                                scenes="\n".join(f"{i}. {text}" for i, text in enumerate(narration, start=1)))
    last_error = ""
    for _ in range(2):
        try:
            style, shots = parse_plan_answer(ask(prompt), count)
            break
        except Exception as exc:  # noqa: BLE001 -- yanıt okunamadı: bir kez daha sorulur
            last_error = f"{type(exc).__name__}: {exc}"
            logger.warning(f"sahne planı okunamadı: {last_error}")
    else:
        raise FictionError(f"sahne planı üretilemedi: {last_error}")
    if style_book:
        style = _clean_style(style_book)
    scenes = []
    for index, (text, shot) in enumerate(zip(narration, shots), start=1):
        scene = {"index": index, "narration": text, "estimated_seconds": estimate_narration(text),
                 "file": file_name(index), **shot, "prompt_edited": False}
        scene["prompt"] = compose_prompt(style, scene, count, settings.clip_seconds)
        scenes.append(scene)
    plan = {
        "version": PLAN_VERSION, "prompt_version": PLAN_PROMPT_VERSION, "subject": subject,
        "script_sha256": batch_review.script_sha256(script), "clip_seconds": settings.clip_seconds,
        "estimated_seconds": round(float(estimated_seconds), 1), "scene_count": count, "tool": settings.tool,
        "timing": TIMING_NARRATION,
        "style_book": style, "style_book_edited": bool(style_book), "style_version": style_version(style),
        "scenes": scenes,
    }
    if previous:
        keep_edits(plan, previous)
    return plan


def keep_edits(plan: dict, previous: dict) -> dict:
    """Eski planın elle düzenlenmiş istemleri yeni planda aynı sıradaki sahneye taşınır."""
    edited = {scene["index"]: scene for scene in previous.get("scenes") or [] if scene.get("prompt_edited")}
    for scene in plan["scenes"]:
        old = edited.get(scene["index"])
        if old:
            scene.update(prompt=old["prompt"], visual_tr=old.get("visual_tr", scene["visual_tr"]), prompt_edited=True)
    return plan


def plan_current(plan: dict | None, script: str) -> bool:
    from app.services import batch_review

    return bool(plan) and plan.get("script_sha256") == batch_review.script_sha256(script)


def apply_edits(plan: dict, prompts: dict | None = None, style_book: dict | None = None) -> dict:
    """
    Kullanıcının düzenlemeleri: ``prompts`` {sahne sırası: istem metni}; ``style_book`` yeni stil kitabı (elle
    düzenlenmemiş istemler yeni stil kitabıyla yeniden kurulur). Değişen plan döner (girdi değişmez).
    """
    plan = json.loads(json.dumps(plan))
    if style_book is not None:
        style = _clean_style(style_book)
        if style != plan["style_book"]:
            plan.update(style_book=style, style_book_edited=True, style_version=style_version(style))
            for scene in plan["scenes"]:
                if not scene.get("prompt_edited"):
                    scene["prompt"] = compose_prompt(style, scene, plan["scene_count"], plan["clip_seconds"])
    for index, text in (prompts or {}).items():
        scene = next((s for s in plan["scenes"] if s["index"] == int(index)), None)
        text = str(text or "").strip()
        if scene is None or not text:
            continue
        if text != scene["prompt"]:
            scene.update(prompt=text, prompt_edited=True)
    return plan


def set_production_order(plan: dict, order: list, reasons: dict | None = None) -> dict:
    """
    Üretim (önem) sırası: günlük üretim kotası sınırlıysa klipler birkaç güne yayılır, anlatım için en kritik
    sahneler önce üretilir. ``order`` sahne sıraları (listede olmayanlar anlatım sırasıyla sona eklenir),
    ``reasons`` {sahne: kısa neden; verilmeyenin eski nedeni korunur}; boş ``order`` sırayı kaldırır. Videodaki
    sahne sırası değişmez: yalnız istem paketi bu sırayla yazılır (en üstte not). Değişen plan döner.
    """
    plan = json.loads(json.dumps(plan))
    indexes = [scene["index"] for scene in plan["scenes"]]
    if not order:
        plan.pop("production_order", None)
        return plan
    chosen: list[int] = []
    for value in order:
        index = int(value)
        if index not in indexes:
            raise FictionError(f"üretim sırası: sahne {index} planda yok (1–{len(indexes)})")
        if index in chosen:
            raise FictionError(f"üretim sırası: sahne {index} iki kez verildi")
        chosen.append(index)
    reasons = {int(key): str(value or "").strip() for key, value in (reasons or {}).items()}
    unknown = sorted(set(reasons) - set(indexes))
    if unknown:
        raise FictionError(f"üretim sırası: sahne {unknown[0]} planda yok (1–{len(indexes)})")
    previous = {entry["index"]: entry.get("why", "") for entry in plan.get("production_order") or []}
    chosen += [index for index in indexes if index not in chosen]
    plan["production_order"] = [{"index": index, "why": reasons.get(index, previous.get(index, ""))}
                                for index in chosen]
    return plan


def _ordered(plan: dict, rows: list[dict]) -> list[dict]:
    """Satırlar planın üretim sırasıyla (yoksa anlatım sırasıyla); her satıra ``why`` (neden) eklenir."""
    order = [entry for entry in plan.get("production_order") or [] if isinstance(entry, dict)]
    rank = {entry.get("index"): position for position, entry in enumerate(order)}
    why = {entry.get("index"): str(entry.get("why") or "") for entry in order}
    rows = [{**row, "why": why.get(row["index"], "")} for row in rows]
    return sorted(rows, key=lambda row: (rank.get(row["index"], len(rank)), row["index"])) if order else rows


ORDER_NOTE = ("Günlük üretim kotası sınırlıysa klipleri bu sırayla üretin: anlatım için en kritik sahneler başta. "
              "Videodaki sahne sırası değişmez; sahneler aşağıda bu sırayla listelenir.")

# Referans görseller (Flow "Ingredients"; resmî Flow yardımı, 15.11.2026): AI Pro'da var; Veo 3.1 Lite ve Fast
# destekler (8 sn, 9:16), Veo 3.1 Quality desteklemez; Fast 05.12.2026'da kalkıyor. Görsel Flow'da (Image, Nano Banana
# Pro; aylık kredi harcamaz) ya da Gemini uygulamasında üretilir. Flow yardımı üst sınır vermiyor; eski Flow blogu ve
# Veo 3.1 API en çok 3 referans diyor: sahne başına en çok MAX_REFERENCES.
MAX_REFERENCES = 3
REFERENCE_PROMPT = ("Reference image for AI video consistency: {name}. {description} One single subject, shown whole "
                    "and centered, front three-quarter view, on a plain light-grey studio background with soft even "
                    "light, photorealistic, square image. Nothing else in the frame; no text, letters, numbers, logos "
                    "or markings anywhere.")
REFERENCE_STEPS = [
    "Her öğe için görseli bir kez üretin: Flow'da model adı → Image (Nano Banana Pro; aylık krediden düşmez) ya da "
    "Gemini uygulamasında. İstemi aşağıdan kopyalayın; düz arka planlı, tek özneli, birbirine benzer görünümlü olsun.",
    "Görseli Flow projesinde öğenin adıyla saklayın (görselin üstünde More → Add to Prompt ile de eklenir).",
    "Klip üretirken model adı → Video → Ingredients seçin; model Veo 3.1 Fast ya da Lite (Quality Ingredients "
    "desteklemez; Fast 5 Aralık 2026'da kalkıyor), 9:16, 8 sn.",
    "Sahnede yazan referansları sürükleyin, @ ile ada göre seçin ya da Add ile ekleyin (sahne başına en çok "
    f"{MAX_REFERENCES}), sonra sahnenin istemini yapıştırın: istem öğeyi aynı adla anar, görselle çelişmez.",
]


def reference_images(plan: dict) -> list[dict]:
    """
    Tekrarlayan öğelerin referans görsel istemleri (Flow Ingredients): {"name", "prompt", "scenes"}; yalnız bir
    sahnede görünen öğeler. Tarifteki "flag/patch" kelimeleri çıkarılır (kural 3).
    """
    style = plan.get("style_book") or {}
    references = []
    for item in style.get("recurring") or []:
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        scenes = [scene["index"] for scene in plan.get("scenes") or []
                  if name.casefold() in {str(element).strip().casefold() for element in scene.get("elements") or []}]
        if not scenes:
            continue
        description = scrub_prompt_words(str(item.get("description") or "").strip()).rstrip(".")
        description = description + "." if description else ""
        if re.search(r"\b(?:astronaut|spacesuit|suit|crew)\b", f"{name} {description}", re.IGNORECASE) \
                and "unmarked" not in description.lower():
            # Kural 3'ün olumlu tarifi: ilk gerçek videoda bu giysinin omzunda bayrak çıktı.
            description += " The suit is plain white and grey with blank, unmarked shoulders, chest and backpack."
        references.append({"name": name, "scenes": scenes,
                           "prompt": REFERENCE_PROMPT.format(name=name, description=description.strip())})
    return references


def scene_references(plan: dict, scene: dict) -> list[str]:
    """Sahnenin Ingredients'a eklenecek referansları (en çok MAX_REFERENCES, stil kitabındaki sırayla)."""
    names = {reference["name"]: reference for reference in reference_images(plan)}
    return [name for name in names if scene["index"] in names[name]["scenes"]][:MAX_REFERENCES]


# ---------------------------------------------------------------- istem paketi


def screening_text(record: dict | None) -> str:
    """Tarama kaydının kısa Türkçe özeti: "kontrol gerekli: bayrak: sol omuzda…", "temiz", "taranamadı: …"."""
    if not record:
        return "taranmadı"
    status = record.get("status")
    if status == SCREEN_UNSCREENED:
        return "taranamadı" + (f": {record.get('error')}" if record.get("error") else "")
    parts = [finding.get("text", "") for finding in record.get("findings") or []] + list(record.get("warnings") or [])
    return SCREEN_TEXT.get(status, str(status)) + (": " + "; ".join(parts) if parts else "")


def _clip_rows(plan: dict, clips: dict | None) -> list[dict]:
    states = (clips or {}).get("scenes") or {}
    rows = []
    for scene in plan["scenes"]:
        state = states.get(str(scene["index"])) or {}
        screening = _screening(state) if state.get("status") == ARRIVED else None
        note = ""
        if screening and screening.get("status") in (SCREEN_CHECK, SCREEN_WARN):
            note = screening_text(screening) + (" (yine de kullanılacak)" if _used_anyway(state) else "")
        rows.append({**scene, "status": state.get("status", WAITING), "reason": state.get("reason", ""),
                     "warnings": state.get("warnings") or [], "screening_note": note})
    return rows


STATUS_TEXT = {WAITING: "bekliyor", ARRIVED: "geldi", UNSUITABLE: "uygun değil"}


def prompts_markdown(plan: dict, clips: dict | None = None) -> str:
    lines = [f"# {plan.get('subject', '')} — Veo istem paketi", "",
             f"{plan['scene_count']} sahne, her biri {plan['clip_seconds']} sn, 9:16. Araç: {plan.get('tool', TOOL)}. "
             f"Stil kitabı sürümü {plan['style_version']}.", "",
             "Her sahnenin istemini Flow'a yapıştırın, klibi verilen dosya adıyla indirip konu klasöründeki "
             f"`{INBOX}/` klasörüne bırakın (ya da WebUI'da sahne kartından yükleyin).", ""]
    rows = _ordered(plan, _clip_rows(plan, clips))
    if plan.get("production_order"):
        lines += ["## Üretim sırası (önem sırasına göre)", "", ORDER_NOTE, ""]
        lines += [f"{position}. Sahne {row['index']:02d} · {STATUS_TEXT.get(row['status'], row['status'])}"
                  + (f" — {row['why']}" if row["why"] else "") for position, row in enumerate(rows, start=1)]
        lines.append("")
    references = reference_images(plan)
    if references:
        lines += ["## Önce bunları üret: referans görseller (Flow Ingredients)", ""]
        lines += [f"{number}. {step}" for number, step in enumerate(REFERENCE_STEPS, start=1)] + [""]
        for reference in references:
            used = ", ".join(f"{index:02d}" for index in reference["scenes"])
            lines += [f"### {reference['name']} (sahne {used})", "", "```text", reference["prompt"], "```", ""]
    lines += ["## Stil kitabı", "", "```text", style_text(plan["style_book"]), "```", ""]
    for row in rows:
        lines += [f"## Sahne {row['index']:02d} · `{row['file']}` · {STATUS_TEXT.get(row['status'], row['status'])}", "",
                  f"> {row['narration']}", "", f"**Görsel:** {row.get('visual_tr', '')}", ""]
        if references:
            names = scene_references(plan, row)
            lines += [f"**Referanslar (Ingredients):** {', '.join(names) if names else 'yok (yalnız istem)'}", ""]
        if row["reason"]:
            lines += [f"**Uygun değil:** {row['reason']}", ""]
        if row["screening_note"]:
            lines += [f"**Tarama:** {row['screening_note']}", ""]
        lines += ["```text", row["prompt"], "```", ""]
    return "\n".join(lines)


PACKAGE_STYLE = """
body{font-family:system-ui,Segoe UI,Arial,sans-serif;margin:0;background:#f4f5f7;color:#1d2330}
header{padding:20px 24px;background:#1d2330;color:#fff}header h1{margin:0 0 6px;font-size:20px}
header p{margin:0;opacity:.8;font-size:14px}main{max-width:980px;margin:0 auto;padding:16px}
section{background:#fff;border-radius:10px;padding:14px 16px;margin:12px 0;box-shadow:0 1px 3px rgba(0,0,0,.08)}
h2{font-size:16px;margin:0 0 8px;display:flex;gap:8px;align-items:center;flex-wrap:wrap}
pre{white-space:pre-wrap;background:#f0f2f5;border-radius:8px;padding:10px;font-size:13px;margin:8px 0}
.badge{font-size:12px;border-radius:10px;padding:2px 8px;background:#e3e6eb}.bekliyor{background:#fff1c2}
.geldi{background:#cdeccf}.uygun_degil{background:#f8d0d0}code{background:#eef0f3;padding:1px 5px;border-radius:4px}
blockquote{margin:6px 0;padding-left:10px;border-left:3px solid #c5cad3;color:#4a5262}
button{cursor:pointer;border:0;border-radius:6px;background:#2f6fde;color:#fff;padding:6px 12px;font-size:13px}
.note{font-size:13px;color:#7a2020}
.order{border-left:4px solid #2f6fde}.order ol{margin:8px 0 0;padding-left:22px}.order li{margin:4px 0}
.order a{color:#1d4fa8;font-weight:600;text-decoration:none}.order p{margin:0;font-size:14px}
.refs{border-left:4px solid #1f8a4c}.refs h3{font-size:14px;margin:12px 0 4px;display:flex;gap:8px;align-items:center}
.refs ol{margin:4px 0 8px;padding-left:22px;font-size:14px}.refs li{margin:3px 0}.refline{font-size:13px;color:#1f5f3a}
""".strip()

PACKAGE_SCRIPT = """
function copyText(id, button){const text=document.getElementById(id).innerText;
const done=()=>{const old=button.innerText;button.innerText='Kopyalandı';setTimeout(()=>button.innerText=old,1500)};
if(navigator.clipboard&&window.isSecureContext){navigator.clipboard.writeText(text).then(done)}else{
const area=document.createElement('textarea');area.value=text;document.body.appendChild(area);area.select();
document.execCommand('copy');area.remove();done()}}
""".strip()


def package_html(plan: dict, clips: dict | None = None) -> str:
    esc = html.escape
    rows = _ordered(plan, _clip_rows(plan, clips))
    arrived = sum(row["status"] == ARRIVED for row in rows)
    order = ""
    if plan.get("production_order"):
        items = "".join(
            f"<li><a href='#sahne-{row['index']:02d}'>Sahne {row['index']:02d}</a> "
            f"<span class='badge {esc(row['status'])}'>{esc(STATUS_TEXT.get(row['status'], row['status']))}</span>"
            + (f" — {esc(row['why'])}" if row["why"] else "") + "</li>" for row in rows)
        order = (f"<section class='order' id='uretim-sirasi'><h2>Üretim sırası (önem sırasına göre)</h2>"
                 f"<p>{esc(ORDER_NOTE)}</p><ol>{items}</ol></section>")
    parts = [
        "<!doctype html><html lang='tr'><head><meta charset='utf-8'><meta name='viewport' "
        "content='width=device-width, initial-scale=1'>", f"<title>İstem paketi · {esc(plan.get('subject', ''))}</title>",
        f"<style>{PACKAGE_STYLE}</style><script>{PACKAGE_SCRIPT}</script></head><body>",
        f"<header><h1>{esc(plan.get('subject', ''))}</h1><p>{plan['scene_count']} sahne · {plan['clip_seconds']} sn · "
        f"9:16 · {esc(plan.get('tool', TOOL))} · stil kitabı {esc(plan['style_version'])} · "
        f"{arrived}/{len(rows)} klip geldi</p></header><main>", order,
        "<section><h2>Nasıl kullanılır</h2><p>Her sahnenin istemini <b>Kopyala</b> ile alıp Flow'a yapıştırın "
        "(9:16, 8 sn). Klibi gösterilen dosya adıyla indirin ve konu klasöründeki <code>"
        f"{INBOX}/</code> klasörüne bırakın ya da WebUI'da sahne kartından yükleyin.</p></section>",
    ]
    references = reference_images(plan)
    if references:
        steps = "".join(f"<li>{esc(step)}</li>" for step in REFERENCE_STEPS)
        items = "".join(
            f"<h3>{esc(reference['name'])} <span class='badge'>sahne "
            f"{esc(', '.join(f'{index:02d}' for index in reference['scenes']))}</span> "
            f"<button onclick=\"copyText('ref{number}',this)\">Kopyala</button></h3>"
            f"<pre id='ref{number}'>{esc(reference['prompt'])}</pre>" for number, reference in enumerate(references, 1))
        parts.append(f"<section class='refs' id='referanslar'><h2>Önce bunları üret: referans görseller "
                     f"(Flow Ingredients)</h2><ol>{steps}</ol>{items}</section>")
    parts.append("<section><h2>Stil kitabı <button onclick=\"copyText('style',this)\">Kopyala</button></h2>"
                 f"<pre id='style'>{esc(style_text(plan['style_book']))}</pre></section>")
    for row in rows:
        anchor = f"p{row['index']:02d}"
        names = scene_references(plan, row) if references else []
        parts.append(
            f"<section id='sahne-{row['index']:02d}'><h2>Sahne {row['index']:02d} <code>{esc(row['file'])}</code> "
            f"<span class='badge {esc(row['status'])}'>{esc(STATUS_TEXT.get(row['status'], row['status']))}</span> "
            f"<button onclick=\"copyText('{anchor}',this)\">Kopyala</button></h2>"
            f"<blockquote>{esc(row['narration'])}</blockquote><p>{esc(row.get('visual_tr', ''))}</p>"
            + (f"<p class='refline'>Referanslar (Ingredients): <b>{esc(', '.join(names) or 'yok (yalnız istem)')}</b>"
               "</p>" if references else "")
            + (f"<p class='note'>Uygun değil: {esc(row['reason'])}</p>" if row["reason"] else "")
            + (f"<p class='note'>Tarama: {esc(row['screening_note'])}</p>" if row["screening_note"] else "")
            + f"<pre id='{anchor}'>{esc(row['prompt'])}</pre></section>")
    parts.append("</main></body></html>")
    return "".join(parts)


def _write_text(path: Path, text: str) -> None:
    temp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex[:6]}.tmp")
    temp.write_text(text, encoding="utf-8")
    _replace(temp, path)


def _replace(source: Path, target: Path) -> None:
    for attempt in range(50):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if attempt == 49:
                source.unlink(missing_ok=True)
                raise
            time.sleep(0.1)


def write_package(folder: Path, plan: dict, clips: dict | None = None) -> list[Path]:
    """İstem paketi: sahne-plani.json, istemler.md ve istem-paketi.html (konu ya da görev klasöründe)."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    clips = clips if clips is not None else load_clips(folder)
    data = {**plan, "clips": {str(row["index"]): {"status": row["status"], "reason": row["reason"]}
                              for row in _clip_rows(plan, clips)},
            "references": [{**reference, "scenes": reference["scenes"]} for reference in reference_images(plan)]}
    paths = [folder / PLAN_FILE, folder / PROMPTS_FILE, folder / PACKAGE_FILE]
    _write_text(paths[0], json.dumps(data, ensure_ascii=False, indent=1))
    _write_text(paths[1], prompts_markdown(plan, clips))
    _write_text(paths[2], package_html(plan, clips))
    return paths


# ---------------------------------------------------------------- klipler


def load_clips(folder: Path) -> dict:
    try:
        data = json.loads((Path(folder) / CLIPS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"scenes": {}, "unmatched": []}
    data.setdefault("scenes", {})
    data.setdefault("unmatched", [])
    return data


def save_clips(folder: Path, clips: dict) -> None:
    clips["updated_at"] = datetime.now().isoformat(timespec="seconds")
    _write_text(Path(folder) / CLIPS_FILE, json.dumps(clips, ensure_ascii=False, indent=1))


def probe(path: str | Path) -> dict:
    """Klibin süresi, boyutu, ses akışı ve videonun çözülebildiği (ilk kare)."""
    from moviepy.video.io.ffmpeg_reader import ffmpeg_parse_infos

    info = {"readable": False, "duration": 0.0, "width": 0, "height": 0, "audio": False}
    try:
        parsed = ffmpeg_parse_infos(str(path))
    except Exception as exc:  # noqa: BLE001 -- bozuk ya da video olmayan dosya
        info["error"] = f"{type(exc).__name__}: {exc}"[:200]
        return info
    size = parsed.get("video_size") or [0, 0]
    info.update(duration=float(parsed.get("duration") or 0.0), width=int(size[0] or 0), height=int(size[1] or 0),
                audio=bool(parsed.get("audio_found")))
    if not parsed.get("video_found"):
        return info
    result = subprocess.run(
        [utils.get_ffmpeg_binary(), "-nostdin", "-hide_banner", "-loglevel", "error",
         *ffmpeg_inputs.ffmpeg_input(path), "-map", "0:v:0", "-frames:v", "1", "-f", "null", "-"],
        capture_output=True, check=False, timeout=120)
    info["readable"] = result.returncode == 0
    if result.returncode:
        info["error"] = result.stderr.decode("utf-8", "replace").strip()[-200:]
    return info


def validate_clip(info: dict, clip_seconds: float, fit_mode: str = "blur") -> tuple[str, str, list[str]]:
    """(durum, neden, uyarılar): video okunmalı, süre ≥ klip süresi, kısa kenar ≥ 720; yön ve ses uyarıdır."""
    if not info.get("readable"):
        return UNSUITABLE, "video akışı okunamadı", []
    width, height, duration = info.get("width") or 0, info.get("height") or 0, float(info.get("duration") or 0)
    if duration + DURATION_TOLERANCE < clip_seconds:
        return UNSUITABLE, (f"kısa: {duration:.1f} sn < {clip_seconds:g} sn (son kare uzatılmaz; "
                            f"{clip_seconds:g} sn'lik klip üretin)"), []
    if min(width, height) < MIN_SHORT_SIDE:
        return UNSUITABLE, f"çözünürlük düşük: {width}×{height} (kısa kenar en az {MIN_SHORT_SIDE})", []
    warnings = []
    if height and abs(width / height - PORTRAIT_RATIO) > RATIO_TOLERANCE:
        warnings.append(f"en-boy {width}×{height} 9:16 değil: {fit_mode} ile sığdırılacak")
    if info.get("audio"):
        warnings.append("ses akışı var: atılacak")
    return ARRIVED, "", warnings


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def scene_number(name: str, count: int) -> int | None:
    """
    Dosya adındaki sahne numarası: "sahne-03", "scene_3", "klip 03" önce; yoksa harfe bitişik olmayan tek bir
    sayı ("03 ay yüzeyi.mp4"). Birden çok aday ya da aralık dışı sayı: None (kullanıcıya sorulur).
    """
    stem = Path(name).stem.lower()
    labelled = re.findall(r"(?:sahne|scene|shot|klip|clip|s)[\s_\-]*0*(\d{1,3})(?!\d)", stem)
    numbers = labelled or re.findall(r"(?<![a-z0-9])0*(\d{1,3})(?![a-z0-9])", stem)
    found = {int(number) for number in numbers}
    if len(found) == 1:
        number = found.pop()
        return number if 1 <= number <= count else None
    return None


def _entry(path: Path, plan: dict, index: int, source: str, fit_mode: str) -> dict:
    scene = next(s for s in plan["scenes"] if s["index"] == index)
    info = probe(path)
    status, reason, warnings = validate_clip(info, plan["clip_seconds"], fit_mode)
    return {"status": status, "reason": reason, "warnings": warnings, "file": path.name, "source": source,
            "sha256": _sha256(path), "size": path.stat().st_size, "duration": round(info["duration"], 3),
            "width": info["width"], "height": info["height"], "audio": info["audio"],
            "uploaded_at": datetime.now().isoformat(timespec="seconds"),
            "prompt_sha256": hashlib.sha256(scene["prompt"].encode("utf-8")).hexdigest()[:12]}


def inbox(folder: Path) -> Path:
    path = Path(folder) / INBOX
    path.mkdir(parents=True, exist_ok=True)
    return path


def accept_upload(folder: Path, plan: dict, index: int, name: str, data: bytes, fit_mode: str = "blur") -> dict:
    """WebUI'dan sahne kartına yüklenen dosya: gelen/sahne-NN<uzantı> olarak yazılır ve doğrulanır."""
    if not any(s["index"] == index for s in plan["scenes"]):
        raise FictionError(f"sahne {index} planda yok")
    suffix = Path(name).suffix.lower() or ".mp4"
    if suffix not in VIDEO_SUFFIXES:
        raise FictionError(f"video dosyası değil: {name}")
    target = inbox(folder) / f"sahne-{index:02d}{suffix}"
    temp = target.with_name(f".{target.name}.{uuid.uuid4().hex[:6]}.part")
    temp.write_bytes(data)
    _replace(temp, target)
    return assign(folder, plan, target.name, index, source="webui", fit_mode=fit_mode)


def assign(folder: Path, plan: dict, name: str, index: int, source: str = "gelen", fit_mode: str = "blur") -> dict:
    """gelen/ klasöründeki bir dosyayı bir sahneye bağlar (kullanıcının eşleştirmesi de bu yoldan)."""
    path = inbox(folder) / Path(name).name
    if not path.is_file():
        raise FictionError(f"dosya yok: {INBOX}/{Path(name).name}")
    clips = load_clips(folder)
    entry = _entry(path, plan, index, source, fit_mode)
    clips["scenes"][str(index)] = entry
    clips["unmatched"] = [n for n in clips.get("unmatched", []) if n != path.name]
    clips.setdefault("checked", {})[path.name] = entry["sha256"]
    save_clips(folder, clips)
    write_package(folder, plan, clips)
    logger.info(f"kurgu klibi: sahne {index:02d} ← {path.name}: {entry['status']}"
                + (f" ({entry['reason']})" if entry["reason"] else ""))
    return entry


def check_inbox(folder: Path, plan: dict, fit_mode: str = "blur") -> dict:
    """
    gelen/ klasörünü tarar: adında tek sahne numarası olan ve henüz kaydı olmayan (ya da değişmiş) dosyalar o
    sahneye bağlanır; numarasız ya da belirsiz olanlar ``unmatched``'a yazılır (kullanıcı eşleştirir).
    """
    folder = Path(folder)
    clips = load_clips(folder)
    # Daha önce kontrol edilen (eşlenen, kullanıcının bağladığı ya da yeni yüklemeyle yerine başkası konan)
    # dosya, içeriği değişmedikçe yeniden eşlenmez: aksi halde eski dosya yeni yüklemenin üstüne geçerdi.
    checked = clips.setdefault("checked", {})
    matched, unmatched = [], [name for name in clips.get("unmatched", []) if (inbox(folder) / name).is_file()]
    for path in sorted(inbox(folder).iterdir()):
        if not path.is_file() or path.name.startswith(".") or path.suffix.lower() not in VIDEO_SUFFIXES:
            continue
        digest = _sha256(path)
        if checked.get(path.name) == digest:
            continue
        checked[path.name] = digest
        number = scene_number(path.name, plan["scene_count"])
        if number is None:
            unmatched.append(path.name)
            continue
        unmatched = [name for name in unmatched if name != path.name]
        matched.append((number, path.name))
    clips["unmatched"] = sorted(set(unmatched))
    save_clips(folder, clips)
    for number, name in matched:
        assign(folder, plan, name, number, fit_mode=fit_mode)
    clips = load_clips(folder)
    write_package(folder, plan, clips)
    return {"matched": [{"scene": number, "file": name} for number, name in matched],
            "unmatched": clips["unmatched"], "clips": clips}


def _screening(entry: dict) -> dict | None:
    """Sahnenin geçerli tarama kaydı: yalnız şu anki dosyanınki (SHA-256 tutmalı)."""
    record = entry.get("screening") if isinstance(entry, dict) else None
    return record if isinstance(record, dict) and record.get("sha256") == entry.get("sha256") else None


def _used_anyway(entry: dict) -> bool:
    approval = entry.get("use_anyway") if isinstance(entry, dict) else None
    return isinstance(approval, dict) and approval.get("sha256") == entry.get("sha256")


def scene_states(folder: Path, plan: dict) -> list[dict]:
    """Sahne başına durum: {"index", "status", "reason", "warnings", "file", "screening", "use_anyway"}."""
    entries = load_clips(folder).get("scenes") or {}
    states = []
    for row in _clip_rows(plan, load_clips(folder)):
        entry = entries.get(str(row["index"])) or {}
        states.append({"index": row["index"], "status": row["status"], "reason": row["reason"],
                       "warnings": row["warnings"], "file": entry.get("file", ""), "screening": _screening(entry),
                       "use_anyway": _used_anyway(entry)})
    return states


def clips_complete(folder: Path, plan: dict | None) -> bool:
    if not plan:
        return False
    states = load_clips(folder).get("scenes") or {}
    return all((states.get(str(s["index"])) or {}).get("status") == ARRIVED and (Path(folder) / INBOX / (
        states[str(s["index"])].get("file") or "")).is_file() for s in plan["scenes"])


def screening_blockers(folder: Path, plan: dict | None) -> list[int]:
    """Taramada bayrak/arma/logo/yazı/yüz bulunan ve kullanıcının "yine de kullan" demediği sahneler."""
    entries = load_clips(folder).get("scenes") or {}
    blocked = []
    for scene in (plan or {}).get("scenes") or []:
        entry = entries.get(str(scene["index"])) or {}
        record = _screening(entry)
        if entry.get("status") == ARRIVED and record and record.get("status") == SCREEN_CHECK \
                and not _used_anyway(entry):
            blocked.append(scene["index"])
    return blocked


def render_ready(folder: Path, plan: dict | None) -> bool:
    """Bütün klipler geldi ve kontrol gerektiren tarama sonucu onaysız kalmadı."""
    return clips_complete(folder, plan) and not screening_blockers(folder, plan)


def use_anyway(folder: Path, plan: dict, index: int, revoke: bool = False) -> dict:
    """
    Kontrol gerektiren (bayrak, arma, logo, yazı, yüz) klibi kullanıcının açık onayıyla render'a açar; onay o
    dosyaya bağlıdır (yeni klip yüklenince düşer). ``revoke`` onayı geri alır.
    """
    folder = Path(folder)
    clips = load_clips(folder)
    entry = (clips.get("scenes") or {}).get(str(index))
    if not entry or entry.get("status") != ARRIVED:
        raise FictionError(f"sahne {index:02d}: gelmiş bir klip yok")
    record = _screening(entry)
    if revoke:
        entry.pop("use_anyway", None)
    else:
        if not record or record.get("status") != SCREEN_CHECK:
            raise FictionError(f"sahne {index:02d}: taramada kontrol gerektiren bir bulgu yok")
        entry["use_anyway"] = {"sha256": entry["sha256"], "at": datetime.now().isoformat(timespec="seconds")}
    save_clips(folder, clips)
    write_package(folder, plan, clips)
    logger.info(f"kurgu klibi: sahne {index:02d} " + ("onayı geri alındı" if revoke else "yine de kullanılacak"))
    return entry


def _render_screening(entry: dict, enabled: bool) -> dict | None:
    """Render planına ve kaynak kaydına giden tarama özeti."""
    if not enabled:
        return None
    record = _screening(entry) or {}
    return {"status": record.get("status") or SCREEN_UNSCREENED, "model": record.get("model", ""),
            "findings": [finding.get("text", "") for finding in record.get("findings") or []],
            "warnings": list(record.get("warnings") or []), "error": record.get("error", "")
            if record else "taranmadı", "use_anyway": _used_anyway(entry)}


def write_render_plan(folder: Path, plan: dict, subject: str, scene_label: Any = None,
                      settings: Settings | None = None) -> Path:
    """
    Render'ın girdisi: sahneler sırayla, dosyalar ve SHA-256'ları, istemler, stil kitabı sürümü ve (tarama
    açıkken) tarama özetleri. Kontrol gerektiren onaysız klip varsa FictionError.
    """
    folder = Path(folder)
    if not clips_complete(folder, plan):
        raise FictionError("bütün sahnelerin klibi gelmedi")
    blocked = screening_blockers(folder, plan)
    if blocked:
        raise FictionError("taramada kontrol gerekli: sahne " + ", ".join(f"{index:02d}" for index in blocked)
                           + " (yeni klip yükleyin ya da \"yine de kullan\")")
    screening = settings.screening if settings is not None else False
    clips = load_clips(folder)["scenes"]
    scenes = []
    for scene in plan["scenes"]:
        entry = clips[str(scene["index"])]
        path = (folder / INBOX / entry["file"]).resolve()
        if _sha256(path) != entry["sha256"]:
            raise FictionError(f"sahne {scene['index']:02d} klibi kontrolden sonra değişmiş: yeniden kontrol edin")
        scenes.append({"index": scene["index"], "file": str(path), "sha256": entry["sha256"],
                       "uploaded_at": entry.get("uploaded_at", ""), "prompt": scene["prompt"],
                       "narration": scene["narration"], "duration": entry.get("duration", 0)})
        summary = _render_screening(entry, screening)
        if summary:
            scenes[-1]["screening"] = summary
    data = {"version": PLAN_VERSION, "subject": subject, "tool": plan.get("tool", TOOL),
            "style_version": plan["style_version"], "clip_seconds": plan["clip_seconds"],
            "scene_label": scene_label or dict(SCENE_LABEL), "timing": TIMING_NARRATION, "scenes": scenes}
    target = folder / RENDER_PLAN_FILE
    _write_text(target, json.dumps(data, ensure_ascii=False, indent=1))
    return target


# ---------------------------------------------------------------- görev (ai_manual kaynağı)


def vision_record(summary: dict | None) -> dict | None:
    """
    Tarama özetinden kaynak kaydının ``vision`` alanı (kalite raporu bunu okur): taranamayan klip
    ``unscreened`` (gerçek kanalındaki gibi kontrol gerekli), diğerleri ``use`` + durum, bulgular, onay.
    """
    if not isinstance(summary, dict):
        return None
    status = summary.get("status") or SCREEN_UNSCREENED
    reasons = ([summary.get("error") or "taranmadı"] if status == SCREEN_UNSCREENED
               else list(summary.get("findings") or []) + list(summary.get("warnings") or []))
    return {"decision": "unscreened" if status == SCREEN_UNSCREENED else "use", "screening": status,
            "reasons": reasons, "model": summary.get("model") or "", "use_anyway": bool(summary.get("use_anyway"))}


def scene_seconds(audio_duration: float, count: int) -> float:
    """Eşit süreli sahneler (anlatım zamanlaması kurulamazsa): ses sahnelere eşit yayılır."""
    return (float(audio_duration) + 0.05) / max(1, count)


def subtitle_cues(path: str | Path) -> list[tuple[float, float, str]]:
    """Altyazı dosyasının (başlangıç, bitiş, metin) satırları; dosya yoksa boş."""
    from app.utils import subtitle_reader

    cues = []
    for _index, times, text in subtitle_reader.file_to_subtitles(str(path)):
        stamps = re.findall(r"(\d+):(\d+):(\d+)[,.](\d+)", times)
        if len(stamps) >= 2:
            start, end = (int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000 for h, m, s, ms in stamps[:2])
            cues.append((start, end, text))
    return cues


def _tokens(text: str) -> list[str]:
    return re.findall(r"\w+", str(text or "").lower())


def narration_spans(narrations: list[str], cues: list[tuple[float, float, str]]) -> list[tuple[float, float]]:
    """
    Her sahnenin anlatımının gerçek aralığı (ilk kelimesinin başı, son kelimesinin sonu): altyazı satırlarının
    zamanı kelimelerine harf sayısıyla paylaştırılır, metin kelimeleri altyazınınkilerle sırayla eşlenir
    (difflib; yazım farkları atlanır). Eşleşme zayıfsa FictionError.
    """
    import difflib

    words = []
    for start, end, text in cues:
        tokens = _tokens(text)
        total = sum(len(token) for token in tokens) or 1
        at = start
        for token in tokens:
            span = (end - start) * len(token) / total
            words.append((token, at, at + span))
            at += span
    script, owner = [], []
    for index, text in enumerate(narrations):
        tokens = _tokens(text)
        script += tokens
        owner += [index] * len(tokens)
    if not words or not script:
        raise FictionError("altyazı ya da anlatım boş: sahneler anlatıma hizalanamaz")
    matcher = difflib.SequenceMatcher(None, script, [word[0] for word in words], autojunk=False)
    mapped = {}
    for a, b, size in matcher.get_matching_blocks():
        for offset in range(size):
            mapped[a + offset] = b + offset
    if len(mapped) < 0.8 * len(script):
        raise FictionError(f"altyazı anlatımla eşleşmedi ({len(mapped)}/{len(script)} kelime)")
    spans = []
    for index in range(len(narrations)):
        found = [position for position, scene in enumerate(owner) if scene == index and position in mapped]
        if not found:
            raise FictionError(f"sahne {index + 1:02d}: anlatımı altyazıda bulunamadı")
        spans.append((words[mapped[found[0]]][1], words[mapped[found[-1]]][2]))
    return spans


def narration_timing(narrations: list[str], cues: list[tuple[float, float, str]], total: float,
                     clip_durations: list[float], max_speed: float = MAX_SLOWDOWN) -> dict:
    """
    Anlatıma hizalı sahne aralıkları: geçiş iki sahnenin anlatımı arasındaki duraklamanın ortasında, ilk sahne
    0'da başlar, son sahne ``total``'da biter; her sahne kendi anlatımı boyunca ekranda. Klipten uzun sahne en çok
    ``max_speed`` kat yavaşlatılır; daha uzunsa fazlası sonraki (sonda: önceki) sahneye geçer ve uyarı yazılır
    (anlatımın görüntü dışına taşan kısmı ``outside``). Kliplerin yavaşlatılmış toplamı yetmezse FictionError.
    """
    spans = narration_spans(narrations, cues)
    count = len(spans)
    bounds = [0.0] + [round((spans[i][1] + spans[i + 1][0]) / 2, 3) for i in range(count - 1)] + [round(total, 3)]
    caps = [max(0.1, float(duration)) * max_speed for duration in clip_durations]
    warnings = []
    for i in range(count - 1):  # ileri: kapasitesini aşan sahnenin fazlası sonrakine
        excess = bounds[i + 1] - bounds[i] - caps[i]
        if excess > 1e-6:
            bounds[i + 1] = round(bounds[i] + caps[i], 3)
            warnings.append(f"sahne {i + 1:02d}: ekranda {caps[i] + excess:.1f} sn kalmalı, klip {clip_durations[i]:.1f} sn "
                            f"× {max_speed:g} = {caps[i]:.1f} sn; sahne {i + 2:02d} {excess:.1f} sn erken başlıyor "
                            "(metni bölebilirsiniz)")
    for i in range(count - 1, 0, -1):  # geri: sondaki sahnelerin fazlası öncekine
        excess = bounds[i + 1] - bounds[i] - caps[i]
        if excess > 1e-6:
            bounds[i] = round(bounds[i] + excess, 3)
            warnings.append(f"sahne {i + 1:02d}: klip {clip_durations[i]:.1f} sn × {max_speed:g} = {caps[i]:.1f} sn'ye "
                            f"sığmıyor; sahne {i:02d} {excess:.1f} sn uzun kalıyor (metni bölebilirsiniz)")
    if bounds[1] - bounds[0] > caps[0] + 1e-6 or any(b - a < 0.2 for a, b in zip(bounds, bounds[1:])):
        raise FictionError(f"seslendirme {total:.1f} sn: kliplerin {max_speed:g}× yavaşlatılmış toplamı "
                           f"{sum(caps):.1f} sn, sahnelere sığmıyor (kısa klip uzatılmaz; metni kısaltın ya da sahne ekleyin)")
    scenes = []
    for i in range(count):
        start, end = bounds[i], bounds[i + 1]
        scenes.append({"index": i + 1, "narration_start": round(spans[i][0], 2), "narration_end": round(spans[i][1], 2),
                       "start": start, "end": end, "duration": round(end - start, 3),
                       "clip_duration": round(float(clip_durations[i]), 3),
                       "speed": round(max(1.0, (end - start) / max(0.1, float(clip_durations[i]))), 4),
                       "outside": round(max(0.0, start - spans[i][0], spans[i][1] - end), 2)})
    return {"mode": TIMING_NARRATION, "total": round(total, 3), "scenes": scenes, "warnings": warnings,
            "max_outside": max(scene["outside"] for scene in scenes)}


def equal_timing(narrations: list[str], cues: list[tuple[float, float, str]], audio_duration: float) -> dict:
    """Eşit süreli sahnelerin (1. sürüm) aralıkları ve anlatımın görüntü dışına taşan kısmı (karşılaştırma için)."""
    seconds = scene_seconds(audio_duration, len(narrations))
    try:
        spans = narration_spans(narrations, cues)
    except FictionError:
        spans = [(None, None)] * len(narrations)
    scenes = []
    for i, (first, last) in enumerate(spans):
        start, end = round(i * seconds, 3), round((i + 1) * seconds, 3)
        outside = None if first is None else round(max(0.0, start - first, last - end), 2)
        scenes.append({"index": i + 1, "narration_start": None if first is None else round(first, 2),
                       "narration_end": None if last is None else round(last, 2), "start": start, "end": end,
                       "duration": round(seconds, 3), "speed": 1.0, "outside": outside})
    known = [scene["outside"] for scene in scenes if scene["outside"] is not None]
    return {"mode": TIMING_EQUAL, "scenes": scenes, "warnings": [], "max_outside": max(known) if known else None}


def prepare_scene_clip(source: str | Path, duration: float, speed: float, target: str | Path) -> Path:
    """Sahnenin klibi: sessiz, ``speed`` > 1 ise o kat yavaş (setpts), ``duration`` saniyede kesilmiş."""
    target = Path(target)
    command = [utils.get_ffmpeg_binary(), "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
               *ffmpeg_inputs.ffmpeg_input(source), "-an"]
    if speed > 1.0005:
        command += ["-vf", f"setpts={speed:.6f}*PTS"]
    command += ["-t", f"{duration:.3f}", "-c:v", "libx264", "-preset", "veryfast", "-crf", "16",
                "-pix_fmt", "yuv420p", str(target)]
    result = subprocess.run(command, capture_output=True, check=False, timeout=600)
    if result.returncode or not target.is_file() or not target.stat().st_size:
        raise FictionError(f"sahne klibi hazırlanamadı: {Path(source).name}: "
                           f"{result.stderr.decode('utf-8', 'replace').strip()[-200:]}")
    return target


def scene_clip_limit(task_id: str, audio_duration: float, count: int) -> float:
    """
    Birleştirmenin sahne başına kesme sınırı: anlatıma hizalı hazırlanmış kliplerde en uzun sahneden uzun (klipler
    olduğu gibi sıralanır), eşit sürelilerde ses / sahne sayısı.
    """
    try:
        data = json.loads((Path(utils.task_dir(task_id)) / "ai_clips" / TIMING_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = None
    if isinstance(data, dict) and data.get("mode") == TIMING_NARRATION and data.get("scenes"):
        return max(float(scene["duration"]) for scene in data["scenes"]) + 1.0
    return scene_seconds(audio_duration, count)


def load_render_plan(path: str | Path) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise FictionError(f"render planı okunamadı: {path} ({exc})") from exc
    if not data.get("scenes"):
        raise FictionError("render planında sahne yok")
    return data


def task_clips(task_id: str, plan_path: str | Path, audio_duration: float) -> list[str]:
    """
    ai_manual kaynağının malzeme aşaması: klipler görev klasörüne kopyalanır (sabit bağlantı değil: kaynak
    kullanıcının indirdiği dosya), SHA-256'ları denetlenir, kaynak kayıtları yazılır. Sahne süresi klipten uzun
    olamaz (kısa klip uzatılmaz): ses sahne sayısı × en kısa klibi aşarsa FictionError.
    """
    from app.models.schema import MaterialInfo
    from app.services import material, task_materials

    plan = load_render_plan(plan_path)
    scenes = plan["scenes"]
    folder = Path(utils.task_dir(task_id)) / "ai_clips"
    folder.mkdir(parents=True, exist_ok=True)
    copies, durations = [], []
    for scene in scenes:
        source = Path(scene["file"])
        if not source.is_file():
            raise FictionError(f"sahne {scene['index']:02d} klibi yok: {source.name}")
        if _sha256(source) != scene["sha256"]:
            raise FictionError(f"sahne {scene['index']:02d} klibi onaydan sonra değişmiş: {source.name}")
        target = folder / f"sahne-{scene['index']:02d}{source.suffix.lower()}"
        shutil.copy2(source, target)
        copies.append(target)
        durations.append(probe(target)["duration"])
    timing = None
    if plan.get("timing") == TIMING_NARRATION:
        cues = subtitle_cues(Path(utils.task_dir(task_id)) / "subtitle.srt")
        try:
            timing = narration_timing([scene.get("narration", "") for scene in scenes], cues,
                                      float(audio_duration) + RENDER_TAIL_SECONDS, durations)
        except FictionError as exc:
            if not cues or "sığmıyor" not in str(exc):
                logger.warning(f"kurgu: sahneler anlatıma hizalanamadı ({exc}); eşit süreli sahneler")
            else:
                raise
    if timing is None:
        seconds = scene_seconds(audio_duration, len(scenes))
        for scene, duration in zip(scenes, durations):
            if duration + DURATION_TOLERANCE < seconds:
                raise FictionError(
                    f"seslendirme {audio_duration:.1f} sn: {len(scenes)} sahneye {seconds:.1f} sn düşüyor, sahne "
                    f"{scene['index']:02d} klibi {duration:.1f} sn (kısa klip uzatılmaz; metni kısaltın ya da sahne ekleyin)")
        timing = {"mode": TIMING_EQUAL, "seconds": round(seconds, 3)}
        paths = [str(copy) for copy in copies]
    else:
        paths = []
        for position, (copy, scene_timing) in enumerate(zip(copies, timing["scenes"])):
            duration = scene_timing["duration"]
            if position == len(copies) - 1:  # son kare payı: birleştirme sesin sonunda keser
                duration += 0.3
            target = folder / f"sahne-{scene_timing['index']:02d}-sahne.mp4"
            prepare_scene_clip(copy, duration, scene_timing["speed"], target)
            paths.append(str(target))
        for warning in timing["warnings"]:
            logger.warning(f"kurgu zamanlaması: {warning}")
    _write_text(folder / TIMING_FILE, json.dumps(timing, ensure_ascii=False, indent=1))
    records = []
    for scene, path in zip(scenes, paths):
        target, duration = Path(path), probe(path)["duration"]
        info = {"provider": PROVIDER, "asset_id": f"sahne-{scene['index']:02d}", "search_term": scene.get("narration", ""),
                "credit": plan.get("tool", TOOL), "tool": plan.get("tool", TOOL), "prompt": scene.get("prompt", ""),
                "style_version": plan.get("style_version", ""), "sha256": scene["sha256"],
                "uploaded_at": scene.get("uploaded_at", ""), "scene_label": plan.get("scene_label") or SCENE_LABEL,
                "ai_generated": True}
        vision = vision_record(scene.get("screening"))
        if vision:
            info["vision"] = vision
        item = MaterialInfo(provider=PROVIDER, url=str(target), duration=max(1, int(duration)), source_info=info)
        records.append(material._material_source_record(item, str(target)))
    material._persist_material_sources(task_id, records)
    task_materials.materials_ready()
    if timing["mode"] == TIMING_NARRATION:
        logger.info(f"kurgu klipleri: {len(paths)} sahne anlatıma hizalı: "
                    + ", ".join(f"{s['index']:02d} {s['duration']:.1f} sn" + (f" ×{s['speed']:.2f}" if s["speed"] > 1 else "")
                                for s in timing["scenes"]))
    else:
        logger.info(f"kurgu klipleri: {len(paths)} sahne, sahne başına {timing['seconds']:.2f} sn")
    return paths
