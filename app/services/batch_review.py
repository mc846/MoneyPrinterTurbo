"""
İki aşamalı toplu üretim: hazırlanan metinlerin onay kuralları, konu klasöründeki dosyalar ve onay komutları.

Akış: reçetede review_before_render açıkken batch.py her konu için metin, arama terimleri ve çapaları üretir,
bağımsız denetçiyle denetler, konu klasörüne hazirlik.json yazar ve konuyu "onay bekliyor"a geçirir. Kullanıcı
WebUI'da ya da bu modülün komutlarıyla (batch.py onayla / yeniden-uret / yeniden-denetle / atla /
temizleri-onayla) metni düzeltir, yeniden üretir, yeniden denetler, atlar ya da onaylar; kararlar review.json'a
yazılır. "batch.py render <klasör>" yalnız onaylıları, onaylanan metin ve terimlerle üretir.

Konu klasöründeki dosyalar:
- hazirlik.json: hazırlığın sonucu. Yalnız arka plan süreci (batch.py) yazar.
- review.json: kararlar. Yalnız arayüz ve onay komutları yazar; yazım atomiktir (geçici dosya + değiştirme,
  Windows'ta okuyucu varken yeniden denenir), okuma da kısa bir süre yeniden denenir.
- .islem.json: süren bir LLM işleminin kilidi (pid ve süreç başlangıcı; süreç ölünce bayat sayılıp silinir).
- .duzenleme.txt: WebUI'nın düzenlenmiş metni onay komutuna verdiği geçici dosya.
- inceleme.log: WebUI'nın başlattığı onay komutlarının çıktısı.

Onay kuralları (CLI ve WebUI aynı fonksiyonları kullanır):
- Onayla: metin son denetimden sonra değiştiyse önce yeniden denetlenir; terimler kullanıcı tarafından
  düzenlenmediyse ve metin terimlerden sonra değiştiyse metinden yeniden üretilir. Denetimde Y/X varsa ya da
  denetim yapılamadıysa onay yalnız açık bir "yine de onayla" (force) ile verilir.
- Yeniden üret: isteğe bağlı ek talimatla metin ve terimler yeniden üretilip denetlenir; önceki metin geçmişte.
- Yeniden denetle: (düzenlenmiş) metin onaylanmadan denetlenir; eskimiş, elle düzenlenmemiş terimler de yenilenir.
- Atla / atlamayı geri al.
- Temizleri onayla: son denetimi metnin güncel haliyle eşleşen, terimleri güncel ve Y/X içermeyen (denetimi de
  yapılmış) bütün bekleyenler onaylanır; LLM gerekmez.

LLM gerektiren işlemler yalnız batch.py süreçlerinde çalışır (PipelineServices): reçetenin LLM sağlayıcısı
süreç içi config.app'e yazılır, WebUI ise config.app'i config.toml'a kaydedebilir. WebUI bu işlemleri aynı
komutlarla alt süreçte başlatır (batch_runner.start_review_action).
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import signal
import sys
import time
import uuid
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

from loguru import logger

from app.services import batch_production as bp

PREPARATION_FILE = "hazirlik.json"
REVIEW_FILE = "review.json"
BUSY_FILE = ".islem.json"
EDIT_FILE = ".duzenleme.txt"
ACTION_LOG = "inceleme.log"
FILE_VERSION = 1
READ_ATTEMPTS = 20
READ_DELAY_SECONDS = 0.05
# Türkçe Edge seslendirmesinin süresi (tr-TR-EmelNeural-Female, hız 1.0), 09.11.2026'da 18 videoda ölçüldü:
# boşluksuz harf sayısıyla doğrusal, süre ≈ 0,067 sn × harf + 7,7 sn (en büyük sapma 3,1 sn, RMS 1,8 sn;
# kelime sayısıyla en büyük sapma 5,2 sn). Video süresi seslendirmeyle aynı (±0,03 sn). Hız ayarı süreyi böler.
EDGE_TR_SECONDS_PER_CHAR = 0.067
EDGE_TR_BASE_SECONDS = 7.7
COMMANDS = ("durum", "render", "onayla", "yeniden-uret", "yeniden-denetle", "atla", "temizleri-onayla")
# Komut adı → işlem anahtarı (review.json last_action, işlem kilidi ve WebUI çevirileri: batch.review.action.<anahtar>)
ACTION_KEYS = {"onayla": "approve", "yeniden-uret": "regenerate", "yeniden-denetle": "reaudit", "atla": "skip",
               "temizleri-onayla": "approve_clean"}


def action_key(name: str) -> str:
    return ACTION_KEYS.get(name, name)


class ReviewError(ValueError):
    """İşlem bu konuda yapılamaz; reason WebUI çevirisinin anahtarı (batch.review.error.<reason>)."""

    def __init__(self, reason: str, message: str = ""):
        super().__init__(message or reason)
        self.reason = reason


class ReviewBusy(RuntimeError):
    """Bu konuda başka bir LLM işlemi sürüyor."""

    def __init__(self, marker: dict):
        super().__init__(f"bu konuda başka bir işlem sürüyor: {marker.get('action')} (pid {marker.get('pid')})")
        self.marker = marker


# ---------------------------------------------------------------- metin

def normalize_script(text) -> str:
    """
    Düzenlenmiş metin, LLM çıktısıyla aynı noktalama düzeltmelerinden geçer (yazı tipinde glifi olmayan tireler
    ve boşluklar, harf arası kesme işareti); satır sonları LF, satır sonu boşlukları kırpılır.
    """
    from app.services import llm

    text = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    text = llm._LETTER_APOSTROPHE_RE.sub("'", text.translate(llm._UNSUPPORTED_PUNCTUATION))
    return "\n".join(line.rstrip() for line in text.split("\n")).strip()


def script_sha256(text) -> str:
    return hashlib.sha256(normalize_script(text).encode("utf-8")).hexdigest()


def parse_list(value) -> list[str]:
    """Terim ve çapa listesi: liste ya da virgül/satırla ayrılmış metin; boşlar ve tekrarlar atılır."""
    parts = value if isinstance(value, (list, tuple)) else re.split(r"[,，\n]", str(value or ""))
    items, seen = [], set()
    for part in parts:
        text = re.sub(r"\s+", " ", str(part).replace(",", " ")).strip()
        if text and text.casefold() not in seen:
            seen.add(text.casefold())
            items.append(text)
    return items


def estimate(script: str, voice_rate: float = 1.0) -> dict:
    """Kelime ve boşluksuz harf sayısı, ölçülen Türkçe Edge hızıyla tahmini seslendirme süresi (sn)."""
    chars = len(re.sub(r"\s+", "", script or ""))
    rate = voice_rate if isinstance(voice_rate, (int, float)) and voice_rate > 0 else 1.0
    seconds = (EDGE_TR_SECONDS_PER_CHAR * chars + EDGE_TR_BASE_SECONDS) / rate if chars else 0.0
    return {"kelime": len((script or "").split()), "harf": chars, "tahmini_sn": round(seconds, 1)}


# ---------------------------------------------------------------- dosyalar

def read_json(path: Path) -> dict | None:
    """JSON nesnesi; dosya yoksa None. Windows'ta yazıcı dosyayı değiştirirken okuma kısa süre yeniden denenir."""
    for attempt in range(READ_ATTEMPTS):
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (PermissionError, ValueError):
            if attempt == READ_ATTEMPTS - 1:
                raise
            time.sleep(READ_DELAY_SECONDS)
            continue
        return data if isinstance(data, dict) else None
    return None


def write_json(path: Path, data: dict) -> None:
    """Atomik yazım: aynı klasörde benzersiz geçici dosya, sonra değiştirme (Windows'ta okuyucu varken yeniden)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    try:
        bp.replace_file(temp, path)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise


def load_preparation(folder: Path) -> dict | None:
    return read_json(Path(folder) / PREPARATION_FILE)


def write_preparation(folder: Path, data: dict) -> None:
    """Yalnız arka plan süreci (batch.py hazırlık aşaması) çağırır."""
    write_json(Path(folder) / PREPARATION_FILE, {"version": FILE_VERSION, **data})


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def initial_review(preparation: dict) -> dict:
    """Hazırlıktan, henüz karar verilmemiş review.json içeriği (dosyaya ilk işlemde yazılır)."""
    script = normalize_script(preparation.get("script", ""))
    sha = script_sha256(script)
    return {
        "version": FILE_VERSION,
        "script": script,
        "terms": parse_list(preparation.get("terms") or []),
        "anchors": parse_list(preparation.get("anchors") or []),
        "script_edited": False,
        "terms_edited": False,  # terimler ya da çapalar elle düzenlendi: metin değişince yeniden üretilmez
        "terms_sha256": sha,  # terimlerin üretildiği metnin özeti
        "audit": preparation.get("audit") or {},
        "audit_sha256": sha,  # son denetimin ait olduğu metnin özeti
        "approved": False,
        "approved_at": "",
        "approved_sha256": "",  # onaylanan metnin özeti; metin sonra değişirse onay geçersiz
        "approved_despite_flags": False,
        "skipped": False,
        "skipped_at": "",
        "history": [],  # yeniden üretimler: talimat ve önceki metin, terimler, denetim
        "last_action": {},
        "updated_at": "",
    }


def load_review(folder: Path) -> dict | None:
    """review.json; yoksa hazırlıktan başlangıç kararı; konu hazırlanmadıysa None."""
    folder = Path(folder)
    review = read_json(folder / REVIEW_FILE)
    if review is None:
        preparation = load_preparation(folder)
        return initial_review(preparation) if preparation else None
    return {**initial_review({"script": review.get("script", "")}), **review}


def save_review(folder: Path, review: dict) -> None:
    """Yalnız arayüz ve onay komutları çağırır."""
    review["updated_at"] = _now()
    write_json(Path(folder) / REVIEW_FILE, review)


# ---------------------------------------------------------------- kurallar

def audit_current(review: dict) -> bool:
    return review.get("audit_sha256") == script_sha256(review.get("script", ""))


def terms_current(review: dict) -> bool:
    return bool(review.get("terms_edited")) or review.get("terms_sha256") == script_sha256(review.get("script", ""))


def audit_blocks(audit: dict | None) -> bool:
    """Onayı "yine de onayla"ya bağlayan denetim: Y ya da X var, ya da denetim yapılamadı. Kapalıysa engellemez."""
    audit = audit or {}
    if audit.get("durum") == "kapalı":
        return False
    if audit.get("durum") != "tamam":
        return True
    counts = audit.get("sayim") or {}
    return any(counts.get(rating) for rating in ("Y", "X"))


def flagged_claims(audit: dict | None) -> list[dict]:
    return [claim for claim in (audit or {}).get("iddialar", []) if claim.get("rating") in ("Y", "X")]


def is_approved(review: dict | None) -> bool:
    return bool(review and review.get("approved") and not review.get("skipped")
                and review.get("approved_sha256") == script_sha256(review.get("script", "")))


def is_clean(review: dict | None) -> bool:
    """Temizleri onayla'nın ölçütü: güncel denetim, güncel (ya da elle düzenlenmiş) terimler, Y/X yok."""
    return bool(review and audit_current(review) and terms_current(review) and not audit_blocks(review.get("audit")))


def approved_copy(review: dict | None) -> dict | None:
    """Render'ın kullanacağı onaylı içerik; geçerli onay yoksa None."""
    if not is_approved(review):
        return None
    return {
        "script": normalize_script(review["script"]),
        "terms": parse_list(review.get("terms") or []),
        "anchors": parse_list(review.get("anchors") or []),
        "audit": review.get("audit") or {},
        "approved_at": review.get("approved_at", ""),
        "approved_despite_flags": bool(review.get("approved_despite_flags")),
        "script_edited": bool(review.get("script_edited")),
        "terms_edited": bool(review.get("terms_edited")),
        "regenerations": len(review.get("history") or []),
    }


def effective_status(item: bp.ItemState, review: dict | None, running: bool = False) -> str:
    """
    Görüntülenen durum: state.json'daki durum ve review.json'daki kararlar birlikte. Süreç çalışmıyorsa
    yarıda kalan hazırlık "bekliyor", yarıda kalan render "onaylandı" görünür (sonraki çalıştırma sürdürür).
    """
    status = item.status
    if status in (bp.RUNNING, bp.PREPARING):
        return status if running else bp.PENDING
    if status == bp.RENDERING:
        return bp.RENDERING if running else bp.APPROVED
    if status == bp.DONE:
        return "review" if item.quality.get("kontrol_gerekli") else bp.DONE
    if status == bp.FAILED:
        return bp.FAILED
    if status in (bp.AWAITING_REVIEW, bp.APPROVED, bp.SKIPPED):
        if review is not None and review.get("skipped"):
            return bp.SKIPPED
        if is_approved(review):
            return bp.APPROVED
        return bp.AWAITING_REVIEW
    return bp.PENDING


def _apply_edits(review: dict, script=None, terms=None, anchors=None) -> None:
    if script is not None:
        script = normalize_script(script)
        if not script:
            raise ReviewError("empty_script", "metin boş olamaz")
        if script != review["script"]:
            review["script"], review["script_edited"] = script, True
    for key, value in (("terms", terms), ("anchors", anchors)):
        if value is not None:
            value = parse_list(value)
            if value != review.get(key):
                review[key], review["terms_edited"] = value, True
    if not review.get("terms"):
        raise ReviewError("empty_terms", "en az bir arama terimi gerekli")


def needs_llm(review: dict, script=None, terms=None, anchors=None) -> bool:
    """Onay bu düzenlemelerle LLM gerektirir mi (yeniden denetim ya da terim üretimi)? WebUI buna göre seçer."""
    preview = copy.deepcopy(review)
    _apply_edits(preview, script, terms, anchors)
    return not audit_current(preview) or not terms_current(preview)


def _subject(folder: Path) -> str:
    return str((load_preparation(folder) or {}).get("subject") or Path(folder).name)


def _editable(folder: Path) -> dict:
    review = load_review(folder)
    if review is None:
        raise ReviewError("not_prepared", "konu henüz hazırlanmadı")
    if review.get("skipped"):
        raise ReviewError("skipped", "konu atlandı; önce atlamayı geri alın")
    if is_approved(review):
        raise ReviewError("approved", "konu zaten onaylı")
    return review


def _record(review: dict, action: str, ok: bool = True, reason: str = "", message: str = "") -> None:
    review["last_action"] = {"action": action, "ok": ok, "reason": reason, "message": message, "at": _now()}


def _refresh(review: dict, services, subject: str) -> None:
    """Eskimiş denetimi ve (elle düzenlenmemiş) eskimiş terimleri yeniler; LLM gerekiyorsa services şart."""
    sha = script_sha256(review["script"])
    stale_audit, stale_terms = review.get("audit_sha256") != sha, not terms_current(review)
    if (stale_audit or stale_terms) and services is None:
        raise ReviewError("needs_llm", "metin değişti: yeniden denetim ve terim üretimi için onay komutu gerekli")
    if stale_audit:
        review["audit"], review["audit_sha256"] = services.audit(subject, review["script"]), sha
    if stale_terms:
        review["terms"], review["anchors"] = services.terms(subject, review["script"])
        review["terms_sha256"] = sha


@dataclass
class Outcome:
    status: str  # approved | needs_force | audited | regenerated | skipped | unskipped
    review: dict
    message: str = ""


def approve(folder: Path, services=None, *, script=None, terms=None, anchors=None, force: bool = False) -> Outcome:
    folder = Path(folder)
    review = _editable(folder)
    _apply_edits(review, script, terms, anchors)
    _refresh(review, services, _subject(folder))
    blocked = audit_blocks(review.get("audit"))
    if blocked and not force:
        message = describe_audit(review.get("audit"))
        _record(review, "approve", ok=False, reason="needs_force", message=message)
        save_review(folder, review)
        return Outcome("needs_force", review, message)
    review.update(approved=True, approved_at=_now(), approved_sha256=script_sha256(review["script"]),
                  approved_despite_flags=blocked)
    _record(review, "approve")
    save_review(folder, review)
    return Outcome("approved", review)


def reaudit(folder: Path, services, *, script=None, terms=None, anchors=None) -> Outcome:
    folder = Path(folder)
    review = _editable(folder)
    _apply_edits(review, script, terms, anchors)
    subject = _subject(folder)
    sha = script_sha256(review["script"])
    review["audit"], review["audit_sha256"] = services.audit(subject, review["script"]), sha
    if not terms_current(review):
        review["terms"], review["anchors"] = services.terms(subject, review["script"])
        review["terms_sha256"] = sha
    _record(review, "reaudit", message=describe_audit(review["audit"]))
    save_review(folder, review)
    return Outcome("audited", review)


def regenerate(folder: Path, services, *, instruction: str = "") -> Outcome:
    folder = Path(folder)
    review = _editable(folder)
    subject = _subject(folder)
    previous = {key: copy.deepcopy(review.get(key)) for key in
                ("script", "terms", "anchors", "audit", "audit_sha256", "script_edited", "terms_edited")}
    generated = services.generate(subject, instruction.strip())
    script = normalize_script(generated["script"])
    if not script:
        raise ReviewError("empty_script", "yeniden üretim boş metin döndürdü")
    sha = script_sha256(script)
    audit = services.audit(subject, script)
    review["history"] = [*review.get("history", []), {"at": _now(), "instruction": instruction.strip(), **previous}]
    review.update(script=script, terms=parse_list(generated.get("terms") or []),
                  anchors=parse_list(generated.get("anchors") or []), script_edited=False, terms_edited=False,
                  terms_sha256=sha, audit=audit, audit_sha256=sha, approved=False, approved_sha256="")
    _record(review, "regenerate", message=describe_audit(audit))
    save_review(folder, review)
    return Outcome("regenerated", review)


def skip(folder: Path) -> Outcome:
    folder = Path(folder)
    review = load_review(folder)
    if review is None:
        raise ReviewError("not_prepared", "konu henüz hazırlanmadı")
    if is_approved(review):
        raise ReviewError("approved", "konu zaten onaylı")
    review.update(skipped=True, skipped_at=_now())
    _record(review, "skip")
    save_review(folder, review)
    return Outcome("skipped", review)


def unskip(folder: Path) -> Outcome:
    folder = Path(folder)
    review = load_review(folder)
    if review is None:
        raise ReviewError("not_prepared", "konu henüz hazırlanmadı")
    review.update(skipped=False, skipped_at="")
    _record(review, "unskip")
    save_review(folder, review)
    return Outcome("unskipped", review)


def approve_clean(batch_dir: Path, state: bp.BatchState | None = None, exclude=()) -> list[int]:
    """
    Onay bekleyen ve temiz (bkz. is_clean) bütün konuları onaylar; LLM çağırmaz. Onaylananların sıraları.
    exclude: atlanacak sıralar (WebUI'da kaydedilmemiş düzenlemesi olan konular eski metinle onaylanmasın).
    """
    batch_dir = Path(batch_dir)
    state = state or bp.BatchState.load(batch_dir)
    approved = []
    for item in state.items:
        if item.index in set(exclude):
            continue
        folder = batch_dir / item.folder
        review = load_review(folder) if item.prepared else None
        if effective_status(item, review) != bp.AWAITING_REVIEW or not is_clean(review) or busy(folder):
            continue
        approve(folder)
        approved.append(item.index)
    return approved


def describe_audit(audit: dict | None) -> str:
    audit = audit or {}
    if audit.get("durum") == "kapalı":
        return "denetim kapalı"
    if audit.get("durum") != "tamam":
        return f"denetim yapılamadı: {audit.get('hata', '')}".strip()
    counts = audit.get("sayim") or {}
    return " ".join(f"{rating} {counts.get(rating, 0)}" for rating in ("D", "B", "Y", "X"))


# ---------------------------------------------------------------- görünüm

@dataclass
class ItemReview:
    item: bp.ItemState
    folder: Path
    status: str
    review: dict | None
    preparation: dict | None
    busy: dict | None


@dataclass
class Overview:
    batch_dir: Path
    state: bp.BatchState
    preset: bp.Preset | None
    items: list[ItemReview]
    running: bool

    def count(self, status: str) -> int:
        return sum(entry.status == status for entry in self.items)

    @property
    def flagged(self) -> int:
        """Onay bekleyenlerden son denetiminde Y/X olanlar (ya da denetimi yapılamamış olanlar)."""
        return sum(entry.status == bp.AWAITING_REVIEW and entry.review is not None
                   and audit_blocks(entry.review.get("audit")) for entry in self.items)

    @property
    def clean(self) -> int:
        return sum(entry.status == bp.AWAITING_REVIEW and is_clean(entry.review) and not entry.busy
                   for entry in self.items)

    @property
    def approved_waiting(self) -> int:
        """Onaylı ama henüz videosu üretilmemiş konular."""
        return self.count(bp.APPROVED)


def overview(batch_dir: Path, running: bool = False) -> Overview:
    batch_dir = Path(batch_dir)
    state = bp.BatchState.load(batch_dir)
    try:
        preset = bp.load_preset(state.preset)
    except (bp.PresetError, OSError):
        preset = None
    entries = []
    for item in state.items:
        folder = batch_dir / item.folder
        review = load_review(folder) if item.prepared else None
        entries.append(ItemReview(item, folder, effective_status(item, review, running), review,
                                  load_preparation(folder) if item.prepared else None, busy(folder)))
    return Overview(batch_dir, state, preset, entries, running)


# ---------------------------------------------------------------- işlem kilidi

def busy(folder: Path) -> dict | None:
    """Süren LLM işleminin kaydı; süreç ölmüşse (ya da PID başka sürece geçmişse) kayıt silinir ve None döner."""
    from app.services import batch_runner

    path = Path(folder) / BUSY_FILE
    try:
        marker = read_json(path)
    except (PermissionError, ValueError):
        return {"action": "?", "pid": 0}
    if marker is None:
        return None
    if batch_runner._lock_alive(marker):
        return marker
    path.unlink(missing_ok=True)
    return None


def mark_busy(folder: Path, action: str, pid: int) -> bool:
    """İşlemi başlatan (WebUI) alt sürecin PID'iyle kilidi yazar; kilit yoksa True."""
    from app.services import batch_runner

    record = {"pid": pid, "created": batch_runner.process_start_time(pid) or 0, "action": action, "started": _now()}
    try:
        descriptor = os.open(Path(folder) / BUSY_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(record, handle, ensure_ascii=False)
    return True


def acquire_busy(folder: Path, action: str) -> None:
    """Onay komutunun kilidi: kendisi (ya da onu başlatan venv başlatıcısı) için yazılmışsa kabul; değilse ReviewBusy."""
    from app.services import batch_runner

    owners = batch_runner._own_pids(None)
    for _ in range(3):
        if mark_busy(folder, action, os.getpid()):
            return
        marker = busy(folder)
        if marker and marker.get("pid") in owners:
            return
        if marker:
            raise ReviewBusy(marker)
    raise ReviewBusy(busy(folder) or {})


def release_busy(folder: Path) -> None:
    from app.services import batch_runner

    path = Path(folder) / BUSY_FILE
    try:
        marker = read_json(path)
    except (PermissionError, ValueError):
        return
    if marker and marker.get("pid") in batch_runner._own_pids(None):
        path.unlink(missing_ok=True)


# ---------------------------------------------------------------- LLM işlemleri

def _ensure_not_webui() -> None:
    """Reçetenin LLM ayarları config.app'e yazılır; WebUI süreci bunu config.toml'a kaydedebilir."""
    if "streamlit" in sys.modules:
        try:
            from streamlit import runtime

            inside = runtime.exists()
        except Exception:  # noqa: BLE001
            inside = False
        if inside:
            raise RuntimeError("LLM işlemleri WebUI sürecinde çalıştırılmaz; batch.py onay komutlarını kullanın")


class PipelineServices:
    """
    Metin, terim ve denetim, reçetenin ayarlarıyla CLI'ın görev yolundan (LLM sağlayıcısı dahil). Yalnız
    batch.py süreçlerinde; ayarlar ilk LLM çağrısında bu sürecin config.app'ine yazılır, config.toml değişmez.
    """

    def __init__(self, preset: bp.Preset, item: bp.ItemState):
        self.preset, self.item = preset, item
        self._ready = False

    def _prepare(self) -> None:
        if not self._ready:
            _ensure_not_webui()
            bp.apply_app_overrides(self.preset)
            self._ready = True

    def generate(self, subject: str, instruction: str = "", task_id: str | None = None) -> dict:
        self._prepare()
        extra = "\n".join(part for part in (self.item.instruction, instruction) if part.strip())
        item = replace(self.item, subject=subject, instruction=extra)
        return bp.run_text_task(bp.build_task_argv(self.preset, item), task_id or str(uuid.uuid4()))

    def terms(self, subject: str, script: str) -> tuple[list[str], list[str]]:
        self._prepare()
        from app.services import task as tm

        item = replace(self.item, subject=subject)
        params = bp._task_params(bp.build_task_argv(self.preset, item) + [f"--video-script={script}"], "terms")
        params.video_anchors = ""  # metinden yeni çapalar
        terms = tm.generate_terms(str(uuid.uuid4()), params, script)
        if not terms:
            raise bp.TaskFailed("arama terimleri üretilemedi", "terms")
        return parse_list(terms), parse_list(tm._video_anchor_list(params.video_anchors))

    def audit(self, subject: str, script: str) -> dict:
        if not self.preset.audit:
            return {"durum": "kapalı", "iddialar": [], "sayim": {}, "isaretli": []}
        self._prepare()
        from app.services import batch_package

        return batch_package.audit_script(subject, script)


def prepare_item(preset: bp.Preset, item: bp.ItemState, task_id: str) -> dict:
    """Hazırlık aşamasının varsayılan işi (arka plan süreci): metin, terimler, çapalar, denetim ve tahmin."""
    services = PipelineServices(preset, item)
    generated = services.generate(item.subject, task_id=task_id)
    script = normalize_script(generated["script"])
    logger.info(f"toplu üretim [{item.index}] denetleniyor")  # batch_runner bu satırı aşama olarak okur
    audit = services.audit(item.subject, script)
    return {
        "script": script, "script_sha256": script_sha256(script), "terms": parse_list(generated["terms"]),
        "anchors": parse_list(generated.get("anchors") or []), "audit": audit,
        "prompt": preset.script_prompt(item.subject, item.instruction),
        **estimate(script, preset.video.get("voice_rate", 1.0)),
    }


# ---------------------------------------------------------------- komutlar

def resolve_batch_dir(value: str) -> Path:
    """Klasör yolu ya da storage/batches altındaki klasörün adı."""
    path = Path(value)
    if not (path / bp.STATE_FILE).is_file() and (bp.batches_dir() / value / bp.STATE_FILE).is_file():
        path = bp.batches_dir() / value
    if not (path / bp.STATE_FILE).is_file():
        raise bp.BatchInputError(f"toplu iş bulunamadı (state.json yok): {value}")
    return path


STATUS_LABELS = {
    bp.PENDING: "bekliyor", bp.PREPARING: "hazırlanıyor", bp.AWAITING_REVIEW: "onay bekliyor",
    bp.APPROVED: "onaylandı", bp.RENDERING: "render ediliyor", bp.RUNNING: "çalışıyor", bp.DONE: "bitti",
    "review": "kontrol gerekli", bp.FAILED: "başarısız", bp.SKIPPED: "atlandı",
}


def status_lines(view: Overview) -> list[str]:
    lines = [f"{view.batch_dir.name}: {view.count(bp.AWAITING_REVIEW)} onay bekliyor ({view.flagged} Y/X), "
             f"{view.approved_waiting} onaylı, {view.count(bp.SKIPPED)} atlandı, "
             f"{view.count(bp.DONE) + view.count('review')} bitti"]
    target = view.preset.target_words if view.preset else None
    for entry in view.items:
        line = f"{entry.item.index:>2}. {entry.item.subject} — {STATUS_LABELS.get(entry.status, entry.status)}"
        if entry.review is not None:
            info = estimate(entry.review["script"], (view.preset.video if view.preset else {}).get("voice_rate", 1.0))
            stale = "" if audit_current(entry.review) else " (eski denetim)"
            line += (f"; {info['kelime']} kelime" + (f" (hedef {target[0]}-{target[1]})" if target else "")
                     + f", ~{info['tahmini_sn']} sn; denetim {describe_audit(entry.review.get('audit'))}{stale}")
            for claim in flagged_claims(entry.review.get("audit")):
                line += f"\n      {claim['rating']}: {claim['claim']} — {claim.get('reason', '')}"
        if entry.item.status == bp.FAILED and entry.item.error:
            line += f"; hata: {entry.item.error}"
        if entry.busy:
            line += f"; işlem sürüyor: {entry.busy.get('action')}"
        lines.append(line)
    return lines


def _read_text_file(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        return Path(value).read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise ReviewError("edit_file", f"metin dosyası okunamadı: {value} ({exc})") from exc


def record_failure(folder: Path, action: str, message: str) -> None:
    """LLM işlemi başarısız olduysa review.json'a son işlem olarak yazılır (WebUI gösterir)."""
    review = load_review(folder)
    if review is None:
        return
    _record(review, action_key(action), ok=False, reason="failed", message=message[:500])
    save_review(folder, review)


def _item(state: bp.BatchState, index: int) -> bp.ItemState:
    if not 1 <= index <= len(state.items):
        raise ReviewError("index", f"geçersiz sıra: {index} (1-{len(state.items)})")
    return state.items[index - 1]


def cli_main(argv: list[str], prog: str = "batch.py") -> int:
    """
    Çıkış kodları: 0 tamam; 1 işlem başarısız (LLM hatası); 2 girdi hatası ya da işlem bu konuda yapılamaz;
    3 başka bir toplu iş (render) ya da bu konuda başka bir işlem sürüyor; 4 onay için --yine-de gerekiyor;
    render için run_batch'teki kodlar (130 Ctrl+C / Ctrl+Break).
    """
    parser = argparse.ArgumentParser(prog=prog, description="İki aşamalı toplu üretimin onay ve render komutları. "
                                     "<klasör> storage/batches altındaki klasörün adı ya da yolu.")
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("durum", help="konuların durumu, kelime ve süre tahmini, denetim")
    command.add_argument("folder", metavar="klasör")
    command = commands.add_parser("render", help="onaylı ve henüz videosu olmayan konuları üret")
    command.add_argument("folder", metavar="klasör")
    command.add_argument("--gunluk-stderr", action="store_true", help=argparse.SUPPRESS)
    for name, help_text in (("onayla", "konuları onayla (değişmiş metin önce denetlenir)"),
                            ("yeniden-denetle", "metni onaylamadan yeniden denetle")):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("folder", metavar="klasör")
        command.add_argument("indexes", metavar="sıra", type=int, nargs="+" if name == "onayla" else 1)
        command.add_argument("--metin-dosyasi", help="düzenlenmiş metin (UTF-8 dosya); yalnız tek konuyla")
        command.add_argument("--terimler", help="arama terimleri, virgülle (elle düzenlenmiş sayılır)")
        command.add_argument("--capalar", help="çapalar, virgülle (elle düzenlenmiş sayılır)")
        if name == "onayla":
            command.add_argument("--yine-de", action="store_true",
                                 help="denetimde Y/X olsa (ya da denetim yapılamasa) da onayla")
    command = commands.add_parser("yeniden-uret", help="metni ve terimleri yeniden üret ve denetle")
    command.add_argument("folder", metavar="klasör")
    command.add_argument("indexes", metavar="sıra", type=int, nargs=1)
    command.add_argument("--talimat", default="", help="bu üretim için ek talimat")
    command = commands.add_parser("atla", help="konuları atla (render edilmez)")
    command.add_argument("folder", metavar="klasör")
    command.add_argument("indexes", metavar="sıra", type=int, nargs="+")
    command.add_argument("--geri-al", action="store_true", help="atlamayı geri al")
    command = commands.add_parser("temizleri-onayla", help="güncel denetimi temiz (Y/X yok) bütün bekleyenleri onayla")
    command.add_argument("folder", metavar="klasör")
    args = parser.parse_args(argv)

    import app.config  # noqa: F401 -- yapılandırma ve günlük ayarları
    from app.services.batch_runner import BatchBusy
    from app.utils.logging_utils import configure_terminal_logger, get_logging_options

    configure_terminal_logger(sys.stderr, level=get_logging_options()[0], colorize=False)
    try:
        batch_dir = resolve_batch_dir(args.folder)
        if args.command == "render":
            has_break = hasattr(signal, "SIGBREAK")
            previous_break = signal.signal(signal.SIGBREAK, signal.default_int_handler) if has_break else None
            try:
                return bp.render_batch(batch_dir, hold_lock=True, log_file=not args.gunluk_stderr)
            finally:
                if has_break:
                    signal.signal(signal.SIGBREAK, previous_break or signal.SIG_DFL)
        state = bp.BatchState.load(batch_dir)
        if not state.review_before_render:
            raise bp.BatchInputError(f"{batch_dir.name} iki aşamalı (onaylı) bir toplu iş değil")
        if args.command == "durum":
            print("\n".join(status_lines(overview(batch_dir))))
            return 0
        if args.command == "temizleri-onayla":
            approved = approve_clean(batch_dir, state)
            print(f"onaylandı: {', '.join(map(str, approved)) or 'yok'}")
            return 0
        return _item_command(args, batch_dir, state)
    except (bp.BatchInputError, bp.PresetError) as exc:
        logger.error(str(exc))
        return 2
    except BatchBusy as exc:
        logger.error(str(exc))
        return 3


def _item_command(args, batch_dir: Path, state: bp.BatchState) -> int:
    edits = {}
    if args.command in ("onayla", "yeniden-denetle"):
        if args.metin_dosyasi and len(args.indexes) != 1:
            logger.error("--metin-dosyasi yalnız tek bir konuyla verilebilir")
            return 2
        edits = {"script": _read_text_file(args.metin_dosyasi), "terms": args.terimler, "anchors": args.capalar}
    preset = None
    code = 0
    for index in args.indexes:
        try:
            item = _item(state, index)
        except ReviewError as exc:
            logger.error(str(exc))
            return 2
        folder = batch_dir / item.folder
        try:
            acquire_busy(folder, args.command)
        except ReviewBusy as exc:
            logger.error(f"{index}. {item.subject}: {exc}")
            code = max(code, 3)
            continue
        try:
            if args.command == "atla":
                outcome = unskip(folder) if args.geri_al else skip(folder)
            else:
                if preset is None:
                    preset = bp.load_preset(state.preset)
                services = PipelineServices(preset, item)
                if args.command == "onayla":
                    outcome = approve(folder, services, force=args.yine_de, **edits)
                elif args.command == "yeniden-denetle":
                    outcome = reaudit(folder, services, **edits)
                else:
                    outcome = regenerate(folder, services, instruction=args.talimat)
        except ReviewError as exc:
            logger.error(f"{index}. {item.subject}: {exc}")
            code = max(code, 2)
            continue
        except KeyboardInterrupt:
            raise
        except Exception as exc:  # noqa: BLE001 -- LLM ya da görev hatası: review.json'a yazılır
            message = f"{type(exc).__name__}: {exc}"
            logger.exception(f"{index}. {item.subject}: {args.command} başarısız: {message}")
            record_failure(folder, args.command, message)
            code = max(code, 1)
            continue
        finally:
            release_busy(folder)
        label = {"approved": "onaylandı", "needs_force": "onaylanmadı, --yine-de gerekiyor", "audited": "denetlendi",
                 "regenerated": "yeniden üretildi", "skipped": "atlandı", "unskipped": "atlama geri alındı"}
        detail = f" ({describe_audit(outcome.review.get('audit'))})" if args.command != "atla" else ""
        print(f"{index}. {item.subject}: {label[outcome.status]}{detail}")
        if outcome.status == "needs_force":
            code = max(code, 4)
    return code
