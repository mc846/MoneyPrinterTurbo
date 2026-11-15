"""
Kurgu klipleri için görsel tarama (ai_manual; reçetenin ``[fiction] clip_screening``'i).

Yüklenen her klibin üç karesi (başı, ortası, sonu) görsel tarama arka ucuyla (``vision_screen``: Gemini,
yedek claude:sonnet) tek istekte sorulur. Kurgu kipinin istemi (``PROMPT_VERSION``) görüntü türünü sormaz
(klip zaten yapay zekâ); bayrak, ulusal ya da kurumsal arma, yama, logo, okunabilir yazı ve tanınabilir yüz
arar ve istemde sahnenin Veo istemi (planlanan tarif) verilir: büyük sapma (istenen makine yerine bambaşka bir
nesne, eksik ana öğe, başka bir yer, gece yerine gündüz) uyarı olarak döner.

Sonuç (``klipler.json`` → sahne → ``screening``):
- ``kontrol`` (kontrol gerekli): bayrak, arma, logo, okunabilir yazı ya da tanınabilir yüz. Klip "uygun değil"
  sayılmaz, ama render kullanıcının açık onayını ister ("yine de kullan", ``fiction.use_anyway``) ya da yeni
  klip yüklenir. Onay o dosyaya (SHA-256) bağlıdır.
- ``uyari``: yalnız yama ya da istemden büyük sapma; render'ı durdurmaz.
- ``temiz``.
- ``taranamadi``: iki model de yanıt vermedi; render'ı durdurmaz, kalite raporunda klip taranmamış sayılır
  (video kontrol gerekli, gerçek kanalının kuralı).

Karar klibin SHA-256'sı, istem ve istem sürümüyle diskte saklanır (``storage/cache_vision/fiction-*.json``);
aynı klip yeniden sorulmaz (revizyon penceresinde taranan klip revizyon kaydına bu önbellekten girer). Gemini
yanıtının token sayıları kayda girer, maliyet ücretli katman fiyatıyla hesaplanır (``GEMINI_PRICES``).
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

from app.services import fiction, vision_screen
from app.utils import ffmpeg_inputs, utils

PROMPT_VERSION = 1
DEFAULT_MODEL = "gemini:gemini-3.5-flash-lite"
DEFAULT_FALLBACK = "claude:sonnet"
LONG_EDGE = 1024  # küçük bir omuz bayrağı 768 px'te birkaç düzine piksele iner
FRAME_MARGIN = 0.5
CLEAN, CHECK, WARN, UNSCREENED = fiction.SCREEN_CLEAN, fiction.SCREEN_CHECK, fiction.SCREEN_WARN, \
    fiction.SCREEN_UNSCREENED
BLOCKING = ("flag", "emblem", "logo", "readable_text", "recognizable_face")
FINDINGS = BLOCKING + ("patch",)
FINDING_TEXT = {"flag": "bayrak", "emblem": "arma", "patch": "yama", "logo": "logo",
                "readable_text": "okunabilir yazı", "recognizable_face": "tanınabilir yüz"}
# Gemini ücretli katman fiyatı, $ / 1M token (giriş; çıkış = yanıt + düşünme); ai.google.dev/pricing, 13.11.2026.
GEMINI_PRICES = {
    "gemini-3.5-flash-lite": (0.30, 2.50), "gemini-3.1-flash-lite": (0.25, 1.50),
    "gemini-2.5-flash-lite": (0.10, 0.40), "gemini-3.6-flash": (0.75, 3.75), "gemini-3.8-flash": (0.75, 3.75),
}

SYSTEM = ("You check AI-generated video clips for a science-fiction dramatization channel before they are "
          "published. Reply with one JSON object only.")
PROMPT = """The images are three frames (start, middle, end) of one AI-generated clip: scene {index} of {count} of a vertical "what if" science-fiction video. The clip must not show flags, national or organizational emblems, patches, logos, readable text or recognizable faces. It was generated from this prompt (what the scene should show):
---
{prompt}
---

Return only this JSON object, no other text:
{{"flag": true|false, "emblem": true|false, "patch": true|false, "logo": true|false, "readable_text": true|false, "recognizable_face": true|false, "details": "<what and where, a few words; empty if none of the above>", "matches_prompt": true|false, "deviation": "<the main difference from the prompt, a few words; empty if it matches>"}}

Definitions:
- flag: any national, regional or organizational flag, also a small one on a sleeve, shoulder, chest, helmet, backpack, vehicle or pole.
- emblem: a coat of arms, seal, insignia or mission emblem of a country, agency or company (for example a space agency's emblem).
- patch: a patch or badge on a suit or uniform, even a plain colored one, that is not a flag, an emblem or a logo.
- logo: the logo, wordmark or brand name of an organization or a product.
- readable_text: letters, words or numbers that can be read (signs, labels, screens, markings); unreadable smudges or symbol-like shapes do not count.
- recognizable_face: a human face visible clearly enough to identify the person (a face behind a clear visor counts; an opaque or mirrored visor does not).
- matches_prompt: false only for a large deviation: a different main subject or object than the prompt describes (for example a wheeled rover instead of the described tracked excavator), a main element missing, another place (indoors instead of outdoors, Earth instead of the Moon) or daylight instead of night. Differences in color, size, camera movement or small details do not count.
Look at all three frames: something visible in any frame counts."""
RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {**{name: {"type": "boolean"} for name in FINDINGS},
                   "details": {"type": "string"}, "matches_prompt": {"type": "boolean"},
                   "deviation": {"type": "string"}},
    "required": [*FINDINGS, "details", "matches_prompt", "deviation"],
}


# ---------------------------------------------------------------- yanıt ve karar


def build_prompt(index: int, count: int, prompt: str) -> str:
    text = PROMPT.format(index=index, count=count, prompt=str(prompt or "").strip() or "(no prompt recorded)")
    return text if count else text.replace(f"scene {index} of {count} of", f"scene {index} of", 1)


def parse(text: str) -> dict | None:
    """Yanıttaki JSON nesnesi (kod çiti ve çevre metin tolere edilir), doğrulanmış; okunamazsa None."""
    data = vision_screen.parse_object(text)
    if data is None:
        return None
    result = {}
    for key in (*FINDINGS, "matches_prompt"):
        value = data.get(key)
        if isinstance(value, str) and value.strip().lower() in ("true", "false"):
            value = value.strip().lower() == "true"
        if not isinstance(value, bool):
            return None
        result[key] = value
    for key in ("details", "deviation"):
        result[key] = str(data.get(key) or "").strip()[:160]
    return result


def decide(result: dict) -> tuple[str, list[dict], list[str]]:
    """(durum, bulgular [{code, text}], uyarılar)."""
    details = result.get("details") or ""
    findings = [{"code": code, "text": FINDING_TEXT[code] + (f": {details}" if details else "")}
                for code in BLOCKING if result.get(code)]
    warnings = []
    if result.get("patch"):
        warnings.append(FINDING_TEXT["patch"] + (f": {details}" if details else ""))
    if not result.get("matches_prompt", True):
        warnings.append("istemden sapma" + (f": {result.get('deviation')}" if result.get("deviation") else ""))
    return (CHECK if findings else WARN if warnings else CLEAN), findings, warnings


def cost_usd(model: str, usage: dict | None) -> float | None:
    """Gemini'de ücretli katman fiyatıyla maliyet; Claude (abonelik) ya da bilinmeyen modelde None."""
    backend, _, name = str(model or "").partition(":")
    price = GEMINI_PRICES.get(name) if backend == "gemini" else None
    if not price or not usage:
        return None
    output = int(usage.get("output") or 0) + int(usage.get("thinking") or 0)
    return round((int(usage.get("input") or 0) * price[0] + output * price[1]) / 1_000_000, 6)


describe = fiction.screening_text


# ---------------------------------------------------------------- kareler ve model


def frame_times(duration: float) -> list[float]:
    duration = max(0.1, float(duration or 0))
    return [round(min(FRAME_MARGIN, duration / 4), 3), round(duration / 2, 3),
            round(max(duration - FRAME_MARGIN, duration * 3 / 4), 3)]


def extract_frames(path: Path, duration: float, folder: str) -> list[str]:
    """Klibin başından, ortasından ve sonundan birer kare (JPEG)."""
    frames = []
    for position, at in enumerate(frame_times(duration)):
        target = os.path.join(folder, f"frame-{position}.jpg")
        subprocess.run([utils.get_ffmpeg_binary(), "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                        "-ss", f"{at:.3f}", *ffmpeg_inputs.ffmpeg_input(path), "-frames:v", "1", "-q:v", "2",
                        target], capture_output=True, check=False, timeout=120)
        if os.path.isfile(target) and os.path.getsize(target):
            frames.append(target)
    return frames


def _ask_backend(images: list[str], prompt: str, backend: str, model: str) -> tuple[str, dict]:
    """Bir arka uca tek çağrı: (yanıt metni, token sayıları); testler bunu değiştirir (conftest)."""
    usage: dict = {}
    text = vision_screen.BACKENDS[backend](images, prompt, model, system=SYSTEM, schema=RESPONSE_SCHEMA,
                                           usage=usage)
    return text, usage


def _models(settings) -> list[tuple[str, str]]:
    primary = vision_screen.parse_model(getattr(settings, "screening_model", "") or DEFAULT_MODEL)
    models = [primary]
    fallback = str(getattr(settings, "screening_fallback", "") or "").strip()
    if fallback and vision_screen.parse_model(fallback) != primary:
        models.append(vision_screen.parse_model(fallback))
    return models


# ---------------------------------------------------------------- önbellek


def cache_key(sha256: str, prompt: str) -> str:
    raw = "|".join([str(PROMPT_VERSION), sha256, hashlib.sha256(str(prompt or "").strip().encode("utf-8")).hexdigest()])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:40]


def _cache_path(key: str) -> Path:
    return Path(utils.storage_dir("cache_vision", create=True)) / f"fiction-{key}.json"


def cached(sha256: str, prompt: str) -> dict | None:
    """Bu klip ve istem için saklanmış karar (yoksa ya da başka istem sürümüyse None)."""
    try:
        data = json.loads(_cache_path(cache_key(sha256, prompt)).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("prompt_version") == PROMPT_VERSION else None


def _store(record: dict, prompt: str) -> None:
    path = _cache_path(cache_key(record["sha256"], prompt))
    try:
        temp = path.with_name(f".{path.name}.{os.getpid()}.part")
        temp.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
        os.replace(temp, path)
    except OSError:
        pass


# ---------------------------------------------------------------- tarama


def screen_clip(path: str | Path, prompt: str, index: int, count: int, settings=None, force: bool = False) -> dict:
    """
    Bir klibin taraması (kayıt sözlüğü). Önbellekte varsa model çağrılmaz (``force`` yeniden sorar). Birincil
    model kullanılamazsa (kota, anahtar yok, oturum sınırı) yedek model; ikisi de olmazsa ``taranamadi``.
    """
    path = Path(path)
    sha256 = fiction._sha256(path)
    if not force:
        known = cached(sha256, prompt)
        if known is not None:
            return {**known, "cached": True}
    duration = fiction.probe(path).get("duration") or 0.0
    started = time.perf_counter()
    record: dict[str, Any] = {"prompt_version": PROMPT_VERSION, "sha256": sha256, "frames": frame_times(duration),
                              "at": datetime.now().isoformat(timespec="seconds"), "cached": False}
    with tempfile.TemporaryDirectory(prefix="fiction-screen-") as folder:
        frames = extract_frames(path, duration, folder)
        images = [vision_screen._encode(frame, LONG_EDGE) for frame in frames]
    if len(images) < len(record["frames"]):
        return {**record, "status": UNSCREENED, "error": "kareler çıkarılamadı", "seconds": 0.0}
    text_prompt = build_prompt(index, count, prompt)
    errors = []
    for backend, model in _models(settings):
        tag = f"{backend}:{model}"
        try:
            result, usage = None, {}
            for _attempt in range(2):
                text, usage = _ask_backend(images, text_prompt, backend, model)
                result = parse(text)
                if result is not None:
                    break
        except Exception as exc:  # noqa: BLE001 -- tarama hiçbir zaman yüklemeyi ya da render'ı düşürmez
            errors.append(f"{tag}: {vision_screen.redact(exc, vision_screen._gemini_key())}"[:200])
            logger.warning(f"kurgu klip taraması: {tag} kullanılamadı ({errors[-1]})")
            continue
        if result is None:
            errors.append(f"{tag}: yanıt okunamadı")
            continue
        status, findings, warnings = decide(result)
        record.update(status=status, findings=findings, warnings=warnings, result=result, model=tag,
                      usage=usage or None, cost_usd=cost_usd(tag, usage),
                      seconds=round(time.perf_counter() - started, 2))
        if errors:
            record["fallback_from"] = errors[0]
        _store(record, prompt)
        return record
    return {**record, "status": UNSCREENED, "error": "; ".join(errors)[:300],
            "seconds": round(time.perf_counter() - started, 2)}


def screen_scene(folder: Path, plan: dict, index: int, settings=None, force: bool = False) -> dict | None:
    """Gelmiş bir sahne klibini tarar ve kaydını ``klipler.json``'a yazar; klip yoksa ya da uygun değilse None."""
    folder = Path(folder)
    entry = (fiction.load_clips(folder).get("scenes") or {}).get(str(index))
    if not entry or entry.get("status") != fiction.ARRIVED:
        return None
    current = entry.get("screening")
    if (not force and isinstance(current, dict) and current.get("sha256") == entry.get("sha256")
            and current.get("status") != UNSCREENED):
        return current
    scene = next(s for s in plan["scenes"] if s["index"] == index)
    record = screen_clip(folder / fiction.INBOX / entry["file"], scene["prompt"], index, plan["scene_count"],
                         settings, force=force)
    clips = fiction.load_clips(folder)
    entry = (clips.get("scenes") or {}).get(str(index))
    if entry and entry.get("sha256") == record["sha256"]:  # bu arada başka klip yüklenmediyse
        entry["screening"] = record
        fiction.save_clips(folder, clips)
        fiction.write_package(folder, plan, clips)
    logger.info(f"kurgu klip taraması: sahne {index:02d}: {describe(record)}"
                + (f" ({record.get('model')}, {record.get('seconds', 0):g} sn"
                   + (", önbellekten" if record.get("cached") else "") + ")" if record.get("model") else ""))
    return record


def screen_pending(folder: Path, plan: dict, settings=None, force: bool = False,
                   indexes: list[int] | None = None) -> list[tuple[int, dict]]:
    """Taraması olmayan (ya da taranamamış) gelmiş klipler; ``force`` ile ``indexes``'tekiler yeniden sorulur."""
    if settings is not None and not getattr(settings, "screening", True):
        return []
    states = fiction.load_clips(folder).get("scenes") or {}
    done = []
    for scene in plan["scenes"]:
        if indexes and scene["index"] not in indexes:
            continue
        if (states.get(str(scene["index"])) or {}).get("status") != fiction.ARRIVED:
            continue
        record = screen_scene(folder, plan, scene["index"], settings, force=force)
        if record is not None:
            done.append((scene["index"], record))
    return done


def total_cost(records: list[dict]) -> float | None:
    costs = [record.get("cost_usd") for record in records if isinstance(record.get("cost_usd"), (int, float))]
    return round(sum(costs), 6) if costs else None
