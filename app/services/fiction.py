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
PLAN_VERSION = 1
PLAN_PROMPT_VERSION = 1
DEFAULT_CLIP_SECONDS = 8
# Sahne sayısı tahmini süreye bu payla hesaplanır: Edge TR tahmininin en büyük sapması 3,1 sn (batch_review).
SCENE_MARGIN_SECONDS = 3.0
MIN_SHORT_SIDE = 720
PORTRAIT_RATIO = 9 / 16
RATIO_TOLERANCE = 0.03
DURATION_TOLERANCE = 0.1
VIDEO_SUFFIXES = (".mp4", ".mov", ".m4v", ".webm", ".mkv")
WAITING, ARRIVED, UNSUITABLE = "bekliyor", "geldi", "uygun_degil"
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
1. Style book: the visual language, lighting, color palette, era and setting, the recurring objects with one
   fixed description each (e.g. a mining rover, the base modules) and the physical plausibility of the setting
   (e.g. on the Moon: black sky, hard shadows, no atmosphere, no haze or clouds, low gravity). Every clip must
   look like the same world.
2. Every scene is ONE continuous {seconds}-second shot (no cuts) with its own camera movement and light, and
   shows what the narration of that scene is about. It is clearly an animated dramatization, not documentary
   footage.
3. Nothing on screen may be text, captions or numbers; no logos or insignia (no NASA meatball or worm, no
   flags); no real or recognizable people (people only as small figures in suits with opaque visors).
4. "visual_tr": one or two Turkish sentences for the user (what the clip shows). "action", "camera", "light":
   concrete English for the video model (what is seen, how the camera moves, the light); no story, no narration.
5. Respond ONLY with one minified JSON object, no markdown:
{{"style_book":{{"visual_language":"...","lighting":"...","color":"...","era":"...","recurring":[{{"name":"...","description":"..."}}],"science":"..."}},"scenes":[{{"index":1,"visual_tr":"...","action":"...","camera":"...","light":"..."}}]}}
{style_hint}
## Topic
{subject}

## Narration by scene
{scenes}
""".strip()

STYLE_FIELDS = ("visual_language", "lighting", "color", "era", "science")


class FictionError(RuntimeError):
    """Kurgu kanalının bir kuralı sağlanmadı (plan, klip, render); mesaj kullanıcıya gösterilir."""


@dataclass(frozen=True)
class Settings:
    """Reçetenin [fiction] bölümü."""

    clip_seconds: int = DEFAULT_CLIP_SECONDS
    tool: str = TOOL
    scene_label: Any = field(default_factory=lambda: dict(SCENE_LABEL))
    style: str = ""  # stil kitabı için yön (ör. "gerçekçi bilimkurgu, 2040'lar"); boşsa LLM seçer


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


def file_name(index: int) -> str:
    return f"sahne-{index:02d}.mp4"


def style_text(style: dict) -> str:
    """Her istemin başına eklenen stil kitabı metni (İngilizce)."""
    style = style or {}
    parts = [f"Style book (identical in every clip): {style.get('visual_language', '').strip()}".rstrip(": ")]
    for key, label in (("lighting", "Lighting"), ("color", "Color palette"), ("era", "Era and setting")):
        if str(style.get(key) or "").strip():
            parts.append(f"{label}: {str(style[key]).strip()}")
    recurring = [f"{str(item.get('name') or '').strip()}: {str(item.get('description') or '').strip()}"
                 for item in style.get("recurring") or [] if isinstance(item, dict) and item.get("name")]
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
    return "\n\n".join([style_text(style), "\n".join(line.rstrip(".") + "." for line in shot),
                        FIXED_RULES.format(seconds=seconds)])


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
    style = {key: _clip(raw.get(key), 1200) for key in STYLE_FIELDS}
    style["recurring"] = [{"name": _clip(item.get("name"), 80), "description": _clip(item.get("description"), 400)}
                          for item in raw.get("recurring") or [] if isinstance(item, dict) and item.get("name")][:8]
    if not style["visual_language"]:
        raise ValueError("stil kitabında görsel dil yok")
    return style


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
        cleaned.append({key: str(scene.get(key) or "").strip()[:600]
                        for key in ("visual_tr", "action", "camera", "light")})
    return style, cleaned


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
    count = scene_count(estimated_seconds, settings.clip_seconds)
    narration = split_narration(script, count)
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
        scene = {"index": index, "narration": text, "file": file_name(index), **shot, "prompt_edited": False}
        scene["prompt"] = compose_prompt(style, scene, count, settings.clip_seconds)
        scenes.append(scene)
    plan = {
        "version": PLAN_VERSION, "prompt_version": PLAN_PROMPT_VERSION, "subject": subject,
        "script_sha256": batch_review.script_sha256(script), "clip_seconds": settings.clip_seconds,
        "estimated_seconds": round(float(estimated_seconds), 1), "scene_count": count, "tool": settings.tool,
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


# ---------------------------------------------------------------- istem paketi


def _clip_rows(plan: dict, clips: dict | None) -> list[dict]:
    states = (clips or {}).get("scenes") or {}
    rows = []
    for scene in plan["scenes"]:
        state = states.get(str(scene["index"])) or {}
        rows.append({**scene, "status": state.get("status", WAITING), "reason": state.get("reason", ""),
                     "warnings": state.get("warnings") or []})
    return rows


STATUS_TEXT = {WAITING: "bekliyor", ARRIVED: "geldi", UNSUITABLE: "uygun değil"}


def prompts_markdown(plan: dict, clips: dict | None = None) -> str:
    lines = [f"# {plan.get('subject', '')} — Veo istem paketi", "",
             f"{plan['scene_count']} sahne, her biri {plan['clip_seconds']} sn, 9:16. Araç: {plan.get('tool', TOOL)}. "
             f"Stil kitabı sürümü {plan['style_version']}.", "",
             "Her sahnenin istemini Flow'a yapıştırın, klibi verilen dosya adıyla indirip konu klasöründeki "
             f"`{INBOX}/` klasörüne bırakın (ya da WebUI'da sahne kartından yükleyin).", "",
             "## Stil kitabı", "", "```text", style_text(plan["style_book"]), "```", ""]
    for row in _clip_rows(plan, clips):
        lines += [f"## Sahne {row['index']:02d} · `{row['file']}` · {STATUS_TEXT.get(row['status'], row['status'])}", "",
                  f"> {row['narration']}", "", f"**Görsel:** {row.get('visual_tr', '')}", ""]
        if row["reason"]:
            lines += [f"**Uygun değil:** {row['reason']}", ""]
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
    rows = _clip_rows(plan, clips)
    arrived = sum(row["status"] == ARRIVED for row in rows)
    parts = [
        "<!doctype html><html lang='tr'><head><meta charset='utf-8'><meta name='viewport' "
        "content='width=device-width, initial-scale=1'>", f"<title>İstem paketi · {esc(plan.get('subject', ''))}</title>",
        f"<style>{PACKAGE_STYLE}</style><script>{PACKAGE_SCRIPT}</script></head><body>",
        f"<header><h1>{esc(plan.get('subject', ''))}</h1><p>{plan['scene_count']} sahne · {plan['clip_seconds']} sn · "
        f"9:16 · {esc(plan.get('tool', TOOL))} · stil kitabı {esc(plan['style_version'])} · "
        f"{arrived}/{len(rows)} klip geldi</p></header><main>",
        "<section><h2>Nasıl kullanılır</h2><p>Her sahnenin istemini <b>Kopyala</b> ile alıp Flow'a yapıştırın "
        "(9:16, 8 sn). Klibi gösterilen dosya adıyla indirin ve konu klasöründeki <code>"
        f"{INBOX}/</code> klasörüne bırakın ya da WebUI'da sahne kartından yükleyin.</p></section>",
        "<section><h2>Stil kitabı <button onclick=\"copyText('style',this)\">Kopyala</button></h2>"
        f"<pre id='style'>{esc(style_text(plan['style_book']))}</pre></section>",
    ]
    for row in rows:
        anchor = f"p{row['index']:02d}"
        parts.append(
            f"<section id='sahne-{row['index']:02d}'><h2>Sahne {row['index']:02d} <code>{esc(row['file'])}</code> "
            f"<span class='badge {esc(row['status'])}'>{esc(STATUS_TEXT.get(row['status'], row['status']))}</span> "
            f"<button onclick=\"copyText('{anchor}',this)\">Kopyala</button></h2>"
            f"<blockquote>{esc(row['narration'])}</blockquote><p>{esc(row.get('visual_tr', ''))}</p>"
            + (f"<p class='note'>Uygun değil: {esc(row['reason'])}</p>" if row["reason"] else "")
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
                              for row in _clip_rows(plan, clips)}}
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


def scene_states(folder: Path, plan: dict) -> list[dict]:
    """Sahne başına durum: {"index", "status", "reason", "warnings", "file"}."""
    return [{"index": row["index"], "status": row["status"], "reason": row["reason"], "warnings": row["warnings"],
             "file": ((load_clips(folder).get("scenes") or {}).get(str(row["index"])) or {}).get("file", "")}
            for row in _clip_rows(plan, load_clips(folder))]


def clips_complete(folder: Path, plan: dict | None) -> bool:
    if not plan:
        return False
    states = load_clips(folder).get("scenes") or {}
    return all((states.get(str(s["index"])) or {}).get("status") == ARRIVED and (Path(folder) / INBOX / (
        states[str(s["index"])].get("file") or "")).is_file() for s in plan["scenes"])


def write_render_plan(folder: Path, plan: dict, subject: str, scene_label: Any = None) -> Path:
    """Render'ın girdisi: sahneler sırayla, dosyalar ve SHA-256'ları, istemler, stil kitabı sürümü."""
    folder = Path(folder)
    if not clips_complete(folder, plan):
        raise FictionError("bütün sahnelerin klibi gelmedi")
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
    data = {"version": PLAN_VERSION, "subject": subject, "tool": plan.get("tool", TOOL),
            "style_version": plan["style_version"], "clip_seconds": plan["clip_seconds"],
            "scene_label": scene_label or dict(SCENE_LABEL), "scenes": scenes}
    target = folder / RENDER_PLAN_FILE
    _write_text(target, json.dumps(data, ensure_ascii=False, indent=1))
    return target


# ---------------------------------------------------------------- görev (ai_manual kaynağı)


def scene_seconds(audio_duration: float, count: int) -> float:
    """Her sahnenin süresi: ses sahnelere eşit yayılır (birleştirmenin güvenlik payıyla)."""
    return (float(audio_duration) + 0.05) / max(1, count)


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
    seconds = scene_seconds(audio_duration, len(scenes))
    folder = Path(utils.task_dir(task_id)) / "ai_clips"
    folder.mkdir(parents=True, exist_ok=True)
    paths, records = [], []
    for scene in scenes:
        source = Path(scene["file"])
        if not source.is_file():
            raise FictionError(f"sahne {scene['index']:02d} klibi yok: {source.name}")
        if _sha256(source) != scene["sha256"]:
            raise FictionError(f"sahne {scene['index']:02d} klibi onaydan sonra değişmiş: {source.name}")
        target = folder / f"sahne-{scene['index']:02d}{source.suffix.lower()}"
        shutil.copy2(source, target)
        duration = probe(target)["duration"]
        if duration + DURATION_TOLERANCE < seconds:
            raise FictionError(
                f"seslendirme {audio_duration:.1f} sn: {len(scenes)} sahneye {seconds:.1f} sn düşüyor, sahne "
                f"{scene['index']:02d} klibi {duration:.1f} sn (kısa klip uzatılmaz; metni kısaltın ya da sahne ekleyin)")
        info = {"provider": PROVIDER, "asset_id": f"sahne-{scene['index']:02d}", "search_term": scene.get("narration", ""),
                "credit": plan.get("tool", TOOL), "tool": plan.get("tool", TOOL), "prompt": scene.get("prompt", ""),
                "style_version": plan.get("style_version", ""), "sha256": scene["sha256"],
                "uploaded_at": scene.get("uploaded_at", ""), "scene_label": plan.get("scene_label") or SCENE_LABEL,
                "ai_generated": True}
        item = MaterialInfo(provider=PROVIDER, url=str(target), duration=max(1, int(duration)), source_info=info)
        records.append(material._material_source_record(item, str(target)))
        paths.append(str(target))
    material._persist_material_sources(task_id, records)
    task_materials.materials_ready()
    logger.info(f"kurgu klipleri: {len(paths)} sahne, sahne başına {seconds:.2f} sn")
    return paths
