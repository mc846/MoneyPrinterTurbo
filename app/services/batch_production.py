"""
Toplu video üretimi（个人工作流，不面向 upstream）。

从 .csv/.txt 读取一组主题，按预设（TOML）依次生成视频；每个视频输出一个可手动上传的
发布包（video.mp4、paylasim.md、kapak.jpg、credits.txt、script.txt、kalite.json、
kareler.jpg），最后生成一份离线可看的 rapor.html。进度写入 state.json：中断后用同一
条命令重新运行会跳过已完成的主题。失败的主题在本轮最后再试一次；再次运行同一条命令时，
仍失败的主题会重新尝试。

用法：uv run python batch.py <girdi.csv|.txt> --preset presets/uzay.toml [--output <klasör>]
"""

from __future__ import annotations

import argparse
import contextlib
import csv
import hashlib
import io
import json
import os
import re
import signal
import string
import sys
import time
import tomllib
import unicodedata
import uuid
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Callable

from loguru import logger

from app.utils import utils

STATE_FILE = "state.json"
STATE_VERSION = 1
REPORT_FILE = "rapor.html"
LOG_FILE = "batch.log"

# CSV 列名（不区分大小写）；没有表头时按 konu, talimat, ses 的位置读取。
INPUT_COLUMNS = {
    "konu": "subject", "subject": "subject",
    "talimat": "instruction", "instruction": "instruction",
    "ses": "voice", "voice": "voice",
}

# 预设 [video] 段落里不允许出现的 CLI 选项：它们来自每一行输入，或者与批量生成无关。
PER_VIDEO_OPTIONS = {
    "video_subject": "konu girdi dosyasından gelir",
    "video_script": "metin her konu için üretilir",
    "video_terms": "arama terimleri her konu için üretilir",
    "video_anchors": "çapalar her konu için üretilir",
    "video_script_prompt": "metin talimatı için [script] prompt anahtarını kullanın",
    "task_id": "görev kimliği her video için üretilir",
    "batch_file": "toplu üretim girdisi komut satırından verilir",
    "stop_at": "toplu üretim her zaman videoyu tamamlar",
    "video_count": "her konu için tek video üretilir",
    "video_materials": "yerel malzeme toplu üretimde desteklenmez",
    "custom_audio_file": "her video kendi seslendirmesini kullanır",
}
# [app] 段落：只写入本进程的 config.app，不写回 config.toml。
APP_OPTIONS = {
    "material_concurrency": int,
    "video_clip_concurrency": int,
    "nasa_face_filter": bool,
    # NASA türleri ve sırası (tür önceliği), ör. ["video", "image"].
    "nasa_media_types": list,
    # Sahne etiketleri ("NASA SVS · … verisinden görselleştirme"); varsayılan açık.
    "scene_labels_enabled": bool,
    # NASA'da sanatçı çizimi, canlandırma, simülasyon ve görselleştirmeleri ele (gerçek görüntü kanalı).
    "nasa_exclude_illustrations": bool,
    # Görevin çapalarından en az biri etikette/başlıkta (NASA'da açıklamada) geçmeyen aday uygun değil.
    "material_require_anchor": bool,
    # Görsel tarama (vision_screen): açık/kapalı, model ("claude:haiku", "ollama:gemma4:31b-cloud"), sınırlar.
    "material_vision_screening": bool,
    "material_vision_model": str,
    "material_vision_max_per_term": int,
    "material_vision_max_per_task": int,
    "material_vision_gemini_rpm": int,
    "video_source_fallback_min_relevance": float,
    "video_source_fallback_min_candidates": int,
    "video_codec": str,
}
SCRIPT_OPTIONS = {"prompt", "target_words"}
QUALITY_OPTIONS = {"target_duration", "audit", "review_before_render"}
PACKAGE_OPTIONS = {"subtitle_translations"}
PRESET_SECTIONS = {"video", "app", "script", "quality", "package"}
PROMPT_PLACEHOLDERS = {"kelime_min", "kelime_max", "konu"}

_TURKISH_ASCII = str.maketrans({
    "ç": "c", "Ç": "c", "ğ": "g", "Ğ": "g", "ı": "i", "İ": "i",
    "ö": "o", "Ö": "o", "ş": "s", "Ş": "s", "ü": "u", "Ü": "u",
})


class BatchInputError(ValueError):
    """Girdi dosyası okunamadı ya da biçimi yanlış."""


class PresetError(ValueError):
    """Reçete dosyasında bilinmeyen anahtar ya da geçersiz değer var."""


class TaskFailed(RuntimeError):
    """Video görevi tamamlanmadı."""

    def __init__(self, message: str, stage: str = ""):
        super().__init__(message)
        self.stage = stage


# ---------------------------------------------------------------- girdi

@dataclass
class BatchInput:
    subject: str
    instruction: str = ""
    voice: str = ""


def slugify(text: str, max_length: int = 60) -> str:
    """Türkçe harfleri ASCII'ye çevirip klasör adına uygun kısa bir ad üretir."""
    text = text.translate(_TURKISH_ASCII)
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii").lower()
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    text = text[:max_length].rstrip("-")
    return text or "video"


def _detect_delimiter(text: str) -> str:
    header = next((line for line in text.splitlines() if line.strip()), "")
    try:
        return csv.Sniffer().sniff(header, delimiters=",;").delimiter
    except csv.Error:
        return ";" if header.count(";") > header.count(",") else ","


def read_inputs(path: str | Path) -> list[BatchInput]:
    """
    .csv（konu, talimat, ses；UTF-8 或 UTF-8-BOM，分隔符 , 或 ; 自动识别）或 .txt（每行一个
    主题，跳过空行和以 # 开头的行）。两种格式都会跳过空主题。
    """
    path = Path(path)
    try:
        text = path.read_bytes().decode("utf-8-sig")
    except OSError as exc:
        raise BatchInputError(f"girdi dosyası okunamadı: {path} ({exc})") from exc
    except UnicodeDecodeError as exc:
        raise BatchInputError(f"girdi dosyası UTF-8 değil: {path}") from exc
    suffix = path.suffix.lower()
    if suffix == ".txt":
        items = [
            BatchInput(subject=line.strip())
            for line in text.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
    elif suffix == ".csv":
        rows = [row for row in csv.reader(io.StringIO(text), delimiter=_detect_delimiter(text)) if any(c.strip() for c in row)]
        columns = ["subject", "instruction", "voice"]
        if rows and rows[0] and rows[0][0].strip().lower() in INPUT_COLUMNS:
            header = [cell.strip().lower() for cell in rows.pop(0)]
            unknown = [cell for cell in header if cell and cell not in INPUT_COLUMNS]
            if unknown:
                raise BatchInputError(
                    f"bilinmeyen CSV sütunu: {', '.join(unknown)} (geçerli: konu, talimat, ses)"
                )
            columns = [INPUT_COLUMNS.get(cell, "") for cell in header]
        items = []
        for row in rows:
            if row[0].strip().startswith("#"):
                continue
            values = {name: cell.strip() for name, cell in zip(columns, row) if name}
            if values.get("subject"):
                items.append(BatchInput(**values))
    else:
        raise BatchInputError(f"girdi .csv ya da .txt olmalı: {path}")
    if not items:
        raise BatchInputError(f"girdide konu yok: {path}")
    return items


# ---------------------------------------------------------------- reçete

@dataclass
class Preset:
    path: str
    sha256: str
    video: dict = field(default_factory=dict)
    app: dict = field(default_factory=dict)
    prompt: str = ""
    target_words: tuple[int, int] | None = None
    target_duration: tuple[float, float] | None = None
    audit: bool = True
    subtitle_translations: tuple[str, ...] = ()  # altyazi.<dil>.srt çevirileri
    # İki aşamalı akış: önce metin + terim + denetim, onaydan sonra yalnız onaylananların videosu.
    review_before_render: bool = False

    def script_prompt(self, subject: str, instruction: str = "") -> str:
        """Reçetedeki talimat şablonu (yer tutucular dolu) ve satırın talimatı."""
        low, high = self.target_words or (0, 0)
        prompt = self.prompt.format(kelime_min=low, kelime_max=high, konu=subject).strip()
        return "\n".join(part for part in (prompt, instruction.strip()) if part)


def _cli_module():
    from app import paths

    if paths.is_source_checkout():
        root = str(Path(__file__).resolve().parents[2])
        if root not in sys.path:
            sys.path.insert(0, root)
        import cli
    else:
        from app import cli
    return cli


def _parse_cli(argv: list[str]):
    """CLI ile aynı ayrıştırma ve doğrulama; argparse hatası PresetError olur."""
    cli = _cli_module()
    errors = io.StringIO()
    try:
        with contextlib.redirect_stderr(errors):
            return cli.parse_args(argv)
    except SystemExit as exc:
        message = errors.getvalue().strip().splitlines()
        raise PresetError(message[-1] if message else f"geçersiz ayar (çıkış {exc.code})") from None


def _range(value, name: str, number_type=int) -> tuple:
    if (
        not isinstance(value, list) or len(value) != 2
        or not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in value)
        or value[0] > value[1] or value[0] < 0
    ):
        raise PresetError(f"{name} iki sayıdan oluşan bir aralık olmalı, ör. [90, 100]")
    return number_type(value[0]), number_type(value[1])


def video_option_argv(options: dict) -> list[str]:
    """[video] anahtarlarını CLI bayraklarına çevirir (anahtar = bayrağın alt çizgili adı)."""
    defaults = vars(_parse_cli(["--video-subject", "x"]))
    argv = []
    for key, value in options.items():
        flag = "--" + key.replace("_", "-")
        if isinstance(value, bool):
            if defaults.get(key) is False:  # store_true: yalnız açık hali vardır
                if value:
                    argv.append(flag)
            else:
                argv.append(flag if value else f"--no-{key.replace('_', '-')}")
        elif isinstance(value, list):
            argv.extend([flag, ", ".join(str(item) for item in value)])
        else:
            argv.extend([flag, str(value)])
    return argv


def load_preset(path: str | Path) -> Preset:
    path = Path(path)
    try:
        raw = path.read_bytes()
        data = tomllib.loads(raw.decode("utf-8-sig"))
    except OSError as exc:
        raise PresetError(f"reçete okunamadı: {path} ({exc})") from exc
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise PresetError(f"reçete TOML olarak okunamadı: {path}: {exc}") from exc
    unknown = sorted(set(data) - PRESET_SECTIONS)
    if unknown:
        raise PresetError(f"bilinmeyen bölüm: [{', '.join(unknown)}]; geçerli bölümler: "
                          f"{', '.join(f'[{s}]' for s in sorted(PRESET_SECTIONS))}")
    for section in PRESET_SECTIONS & set(data):
        if not isinstance(data[section], dict):
            raise PresetError(f"[{section}] bir tablo olmalı")

    video = dict(data.get("video", {}))
    known = set(vars(_parse_cli(["--video-subject", "x"])))
    for key in video:
        if key in PER_VIDEO_OPTIONS:
            raise PresetError(f"[video] {key} reçetede kullanılamaz: {PER_VIDEO_OPTIONS[key]}")
        if key not in known:
            suggestion = _suggest(key, known - set(PER_VIDEO_OPTIONS))
            raise PresetError(f"[video] bilinmeyen anahtar: {key}{suggestion} "
                              f"(anahtarlar `uv run python cli.py --help` bayraklarının alt çizgili adı)")
    # Değerler CLI'ın kendi doğrulamasından geçer (seçenekler, tipler, birbirine bağlı bayraklar).
    _parse_cli(["--video-subject", "x", *video_option_argv(video)])

    app = dict(data.get("app", {}))
    for key, value in app.items():
        if key not in APP_OPTIONS:
            raise PresetError(f"[app] bilinmeyen anahtar: {key}{_suggest(key, APP_OPTIONS)}; "
                              f"geçerli: {', '.join(sorted(APP_OPTIONS))}")
        expected = APP_OPTIONS[key]
        valid = isinstance(value, expected) and not (expected is not bool and isinstance(value, bool))
        if expected is float and isinstance(value, int) and not isinstance(value, bool):
            valid = True
        if not valid:
            raise PresetError(f"[app] {key} {expected.__name__} olmalı")

    script = dict(data.get("script", {}))
    quality = dict(data.get("quality", {}))
    package = dict(data.get("package", {}))
    for section, keys, values in (("script", SCRIPT_OPTIONS, script), ("quality", QUALITY_OPTIONS, quality),
                                  ("package", PACKAGE_OPTIONS, package)):
        for key in values:
            if key not in keys:
                raise PresetError(f"[{section}] bilinmeyen anahtar: {key}{_suggest(key, keys)}; "
                                  f"geçerli: {', '.join(sorted(keys))}")
    prompt = script.get("prompt", "")
    if not isinstance(prompt, str):
        raise PresetError("[script] prompt metin olmalı")
    fields = {name for _, name, _, _ in string.Formatter().parse(prompt) if name}
    if fields - PROMPT_PLACEHOLDERS:
        raise PresetError(f"[script] prompt bilinmeyen yer tutucu içeriyor: "
                          f"{', '.join('{' + f + '}' for f in sorted(fields - PROMPT_PLACEHOLDERS))}; "
                          f"geçerli: {{kelime_min}}, {{kelime_max}}, {{konu}}")
    target_words = _range(script["target_words"], "[script] target_words") if "target_words" in script else None
    if {"kelime_min", "kelime_max"} & fields and target_words is None:
        raise PresetError("[script] prompt {kelime_min}/{kelime_max} kullanıyor ama target_words yok")
    target_duration = (
        _range(quality["target_duration"], "[quality] target_duration", float) if "target_duration" in quality else None
    )
    audit = quality.get("audit", True)
    if not isinstance(audit, bool):
        raise PresetError("[quality] audit true ya da false olmalı")
    review_before_render = quality.get("review_before_render", False)
    if not isinstance(review_before_render, bool):
        raise PresetError("[quality] review_before_render true ya da false olmalı")
    raw_translations = package.get("subtitle_translations", [])
    if not isinstance(raw_translations, list):  # virgüllü metin yalnız CLI'da (--altyazi-cevirileri)
        raise PresetError('[package] subtitle_translations dil kodları listesi olmalı, ör. ["en"]')
    translations = parse_languages(raw_translations, "[package] subtitle_translations")
    return Preset(
        path=str(path.resolve()), sha256=hashlib.sha256(raw).hexdigest(), video=video, app=app,
        prompt=prompt, target_words=target_words, target_duration=target_duration, audit=audit,
        subtitle_translations=translations, review_before_render=review_before_render,
    )


def parse_languages(value, name: str) -> tuple[str, ...]:
    """Çeviri altyazı dilleri: BCP-47 kodları listesi (CLI'da virgülle ayrılmış metin); tekrarlar atılır."""
    from app.services.batch_subtitles import LANGUAGE_CODE

    if isinstance(value, str):
        value = [part for part in value.split(",") if part.strip()]
    if not isinstance(value, list):
        raise PresetError(f'{name} dil kodları listesi olmalı, ör. ["en"]')
    codes: list[str] = []
    for code in value:
        if not isinstance(code, str) or not LANGUAGE_CODE.match(code.strip()):
            raise PresetError(f"{name}: geçersiz dil kodu {code!r} (BCP-47, ör. en, pt-BR)")
        if code.strip() not in codes:
            codes.append(code.strip())
    return tuple(codes)


def _suggest(key: str, choices) -> str:
    import difflib

    match = difflib.get_close_matches(key, sorted(choices), n=1)
    return f" (bunu mu demek istediniz: {match[0]}?)" if match else ""


# ---------------------------------------------------------------- durum

# Konu durumları (state.json; yalnız arka plan süreci yazar).
# Tek aşamalı akış: pending → running → done | failed.
# İki aşamalı akış: pending → preparing → awaiting_review → approved → rendering → done | failed; skipped.
#   Onay ve atlama kararları review.json'da durur (batch_review); render süreci başlarken onaylıları
#   approved, atlananları skipped yapar. "Kontrol gerekli", done + quality["kontrol_gerekli"]'dir.
PENDING, RUNNING, DONE, FAILED = "pending", "running", "done", "failed"
PREPARING, AWAITING_REVIEW, APPROVED, RENDERING, SKIPPED = (
    "preparing", "awaiting_review", "approved", "rendering", "skipped")


@dataclass
class ItemState:
    index: int
    subject: str
    instruction: str = ""
    voice: str = ""
    folder: str = ""
    status: str = PENDING
    attempts: int = 0
    failures: int = 0
    task_id: str = ""
    error: str = ""
    failed_stage: str = ""
    seconds: float = 0.0
    quality: dict = field(default_factory=dict)
    started: str = ""  # son denemenin başlangıcı (çalışırken geçen süre için)
    # İki aşamalı akış: hazırlık (metin, terimler, denetim) ayrı sayılır; render süresi seconds'ta.
    prepared: bool = False
    prepare_seconds: float = 0.0
    prepare_attempts: int = 0
    prepare_task_id: str = ""


@dataclass
class BatchState:
    input: str
    input_sha256: str
    preset: str
    preset_sha256: str
    created: str
    items: list[ItemState]
    version: int = STATE_VERSION
    run_seconds: float = 0.0
    finished: str = ""
    # Bu toplu işin çeviri dilleri (WebUI ya da --altyazi-cevirileri); None ise reçetedeki.
    subtitle_translations: list[str] | None = None
    # İki aşamalı akış mı: toplu iş açılırken reçeteden ya da --inceleme / --inceleme-yok'tan bir kez
    # belirlenir; devam ettirilen çalıştırmalar aynı akışı sürdürür. None: bu alan yokken açılmış toplu iş.
    review_before_render: bool | None = None

    def save(self, directory: Path) -> None:
        payload = json.dumps(asdict(self), ensure_ascii=False, indent=1)
        temp = directory / f".{STATE_FILE}.tmp"
        temp.write_text(payload, encoding="utf-8")
        replace_file(temp, directory / STATE_FILE)

    @classmethod
    def load(cls, directory: Path) -> "BatchState":
        data = json.loads((directory / STATE_FILE).read_text(encoding="utf-8"))
        items = [ItemState(**item) for item in data.pop("items")]
        return cls(items=items, **data)


REPLACE_ATTEMPTS = 50
REPLACE_DELAY_SECONDS = 0.1


def replace_file(source: Path, target: Path) -> None:
    """
    os.replace, Windows'ta kısa aralıklarla yeniden denenerek. Hedef başka bir süreçte okunmak için
    açıkken (WebUI state.json'ı birkaç saniyede bir okur) Windows değiştirmeyi "Erişim engellendi"
    ile reddeder; okuma milisaniyeler sürdüğü için birkaç deneme yeter.
    """
    for attempt in range(REPLACE_ATTEMPTS):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if attempt == REPLACE_ATTEMPTS - 1:
                raise
            time.sleep(REPLACE_DELAY_SECONDS)


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def batches_dir() -> Path:
    return Path(utils.storage_dir("batches", create=True))


def find_previous_batch(input_path: Path, preset: Preset) -> Path | None:
    """Aynı girdi (içerik) ve aynı reçeteyle başlatılmış en son toplu üretim klasörü."""
    input_sha = _file_sha256(input_path)
    candidates = []
    for state_file in batches_dir().glob(f"*/{STATE_FILE}"):
        try:
            data = json.loads(state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if data.get("input_sha256") == input_sha and data.get("preset_sha256") == preset.sha256:
            candidates.append((data.get("created", ""), state_file.parent))
    return max(candidates)[1] if candidates else None


def new_batch_dir(input_path: Path) -> Path:
    """storage/batches/<tarih-saat>_<girdi adı>; aynı saniyede ikinci bir klasör -2, -3… alır."""
    name = f"{datetime.now():%Y%m%d-%H%M%S}_{slugify(input_path.stem)}"
    candidate, number = batches_dir() / name, 1
    while candidate.exists():
        number += 1
        candidate = batches_dir() / f"{name}-{number}"
    return candidate


def new_state(input_path: Path, preset: Preset, items: list[BatchInput]) -> BatchState:
    states = [
        ItemState(index=i, subject=item.subject, instruction=item.instruction, voice=item.voice,
                  folder=f"{i:02d}-{slugify(item.subject)}")
        for i, item in enumerate(items, start=1)
    ]
    return BatchState(
        input=str(input_path.resolve()), input_sha256=_file_sha256(input_path),
        preset=preset.path, preset_sha256=preset.sha256,
        created=datetime.now().isoformat(timespec="seconds"), items=states,
    )


# ---------------------------------------------------------------- görev

def build_task_argv(preset: Preset, item: ItemState) -> list[str]:
    argv = ["--video-subject", item.subject, *video_option_argv(preset.video)]
    prompt = preset.script_prompt(item.subject, item.instruction)
    if prompt:
        argv.extend(["--video-script-prompt", prompt])
    if item.voice:
        argv.extend(["--voice-name", item.voice])
    return argv


def build_render_argv(preset: Preset, item: ItemState, approved: dict) -> list[str]:
    """
    İki aşamalı akışta render: onaylanan metin, terimler ve çapalar görev parametresi olarak verilir,
    böylece task.generate_script / generate_terms LLM'e sormadan bunları kullanır. Metin talimatı
    (yalnız metin üretiminde kullanılır) eklenmez. "--x=değer" biçimi "-" ile başlayan değerleri de korur.
    """
    argv = ["--video-subject", item.subject, *video_option_argv(preset.video),
            f"--video-script={approved['script']}", f"--video-terms={', '.join(approved['terms'])}"]
    if approved.get("anchors"):
        argv.append(f"--video-anchors={', '.join(approved['anchors'])}")
    if item.voice:
        argv.extend(["--voice-name", item.voice])
    return argv


def _task_params(argv: list[str], stop_at: str):
    """CLI'ın tek görev yolu: ayrıştır, geçersiz kılmaları (NASA, render, LLM) uygula, parametreleri hazırla."""
    cli = _cli_module()
    args = _parse_cli(argv)
    cli.apply_nasa_overrides(args)
    cli.apply_render_overrides(args)
    cli.apply_llm_overrides(args)
    params = cli.build_video_params(args)
    cli._validate_video_source_fallback(params)
    cli.prepare_cli_files(params, stop_at=stop_at)
    return params


def run_video_task(argv: list[str], task_id: str) -> dict:
    """CLI'ın tek görev yolunu izler: ayrıştır, geçersiz kılmaları uygula, parametreleri hazırla, task.start."""
    params = _task_params(argv, "video")
    from app.models import const
    from app.services import task as tm

    result = tm.start(task_id=task_id, params=params, stop_at="video", allow_server_file_input=True)
    if not result or result.get("state") == const.TASK_STATE_FAILED or not result.get("videos"):
        stage = (result or {}).get("failed_stage", "")
        error = (result or {}).get("error") or "görev video üretmedi"
        raise TaskFailed(str(error), stage)
    return result


def run_text_task(argv: list[str], task_id: str) -> dict:
    """
    Hazırlık: aynı görev yolu "terms" aşamasında durur (metin + arama terimleri + çapalar; ses ve video yok).
    Metin LLM'in normalleştirilmiş çıktısıdır (noktalama düzeltmeleri dahil). Çapalar görevin
    script.json'una yazılan parametrelerden okunur.
    """
    params = _task_params(argv, "terms")
    from app.models import const
    from app.services import task as tm

    result = tm.start(task_id=task_id, params=params, stop_at="terms", allow_server_file_input=True)
    if not result or result.get("state") == const.TASK_STATE_FAILED or not result.get("script") \
            or not result.get("terms"):
        stage = (result or {}).get("failed_stage", "")
        error = (result or {}).get("error") or "görev metin ya da arama terimi üretmedi"
        raise TaskFailed(str(error), stage)
    anchors = []
    try:
        data = json.loads((Path(utils.task_dir(task_id)) / "script.json").read_text(encoding="utf-8"))
        anchors = (data.get("params") or {}).get("video_anchors") or []
    except (OSError, ValueError):
        pass
    if isinstance(anchors, str):
        anchors = [part.strip() for part in re.split(r"[,，]", anchors)]
    return {"script": str(result["script"]).strip(), "terms": [str(t).strip() for t in result["terms"] if str(t).strip()],
            "anchors": [str(a).strip() for a in anchors if str(a).strip()]}


def apply_app_overrides(preset: Preset) -> None:
    """[app] ayarları yalnız bu süreçte config.app'e yazılır; config.toml değişmez."""
    from app.config import config

    config.app.update(preset.app)


# ---------------------------------------------------------------- çalıştırıcı

def _process(item: ItemState, state: BatchState, preset: Preset, batch_dir: Path,
             task_runner: Callable, packager: Callable, argv: list[str] | None = None,
             running_status: str = RUNNING, package_extra: dict | None = None) -> None:
    """
    Bir konunun videosu ve paketi. Tek aşamalı akış argv'yi reçeteden kurar; iki aşamalı akış onaylanan
    metinle kurulmuş argv'yi, "rendering" durumunu ve pakete onaylanan metnin denetimini (package_extra) verir.
    """
    item.status, item.error, item.failed_stage = running_status, "", ""
    item.attempts += 1
    item.task_id = str(uuid.uuid4())
    item.started = datetime.now().isoformat(timespec="seconds")
    state.save(batch_dir)
    started = time.monotonic()
    logger.info(f"toplu üretim [{item.index}/{len(state.items)}] başladı: {item.subject}")
    captured: list[str] = []
    handler = logger.add(lambda message: captured.append(message.record["message"]), level="DEBUG")
    try:
        result = task_runner(argv if argv is not None else build_task_argv(preset, item), item.task_id)
    except KeyboardInterrupt:
        item.seconds = round(item.seconds + time.monotonic() - started, 1)  # kesilen denemenin süresi de sayılır
        raise
    except Exception as exc:
        item.status, item.failures = "failed", item.failures + 1
        item.error = str(exc).strip()[:500] or type(exc).__name__
        item.failed_stage = getattr(exc, "stage", "") or ""
        item.seconds = round(item.seconds + time.monotonic() - started, 1)
        state.save(batch_dir)
        logger.error(f"toplu üretim [{item.index}] başarısız: {item.subject}: {item.error}")
        return
    finally:
        logger.remove(handler)
    logger.info(f"toplu üretim [{item.index}] paket hazırlanıyor")  # batch_runner bu satırı aşama olarak okur
    try:
        item.quality = packager(item=item, result=result, preset=preset,
                                folder=batch_dir / item.folder, log_lines=captured, **(package_extra or {}))
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        # Paket hazırlanamasa da video üretildi; rapor bunu kontrol gerekli olarak gösterir.
        logger.exception(f"toplu üretim [{item.index}] paketi hazırlanamadı: {exc}")
        item.quality = {"kontrol_gerekli": True, "nedenler": [f"paket hazırlanamadı: {exc}"]}
    item.status = "done"
    item.seconds = round(item.seconds + time.monotonic() - started, 1)
    state.save(batch_dir)
    logger.success(f"toplu üretim [{item.index}] bitti ({item.seconds:.0f} sn): {item.subject}")


def run_batch(
    input_path: str | Path,
    preset_path: str | Path,
    output: str | Path | None = None,
    new: bool = False,
    task_runner: Callable | None = None,
    packager: Callable | None = None,
    reporter: Callable | None = None,
    hold_lock: bool = False,
    log_file: bool = True,
    translations: list[str] | tuple[str, ...] | None = None,
    review: bool | None = None,
    preparer: Callable | None = None,
) -> int:
    """
    Toplu üretimi çalıştırır; tamamı bittiyse 0, başarısız konu kaldıysa 1, Ctrl+C ile
    durdurulduysa 130 döner. task_runner/packager/reporter testlerde sahte fonksiyonla değişir.
    hold_lock: süreç boyunca batch_runner kilidini tutar (batch.py). log_file=False iken günlük
    klasördeki batch.log'a ayrıca yazılmaz (WebUI stderr'i zaten oraya yönlendirir).
    translations: bu toplu işin çeviri altyazı dilleri (reçetedekinin yerine); state.json'a yazılır,
    devam ettirilen çalıştırma da aynılarını kullanır.
    review: iki aşamalı akış (None: reçetedeki review_before_render). Açıksa bu çağrı yalnız hazırlığı
    yapar (metin, terimler, denetim; preparer testlerde sahte fonksiyonla değişir) ve konular onay
    beklerken temiz çıkar; videolar render_batch ile üretilir.
    """
    from app.services import batch_package

    task_runner = task_runner or run_video_task
    packager = packager or batch_package.build_package
    reporter = reporter or batch_package.write_report
    input_path = Path(input_path)
    preset = load_preset(preset_path)
    items = read_inputs(input_path)

    if output:
        batch_dir = Path(output)
    else:
        previous = None if new else find_previous_batch(input_path, preset)
        batch_dir = previous or new_batch_dir(input_path)
    batch_dir.mkdir(parents=True, exist_ok=True)
    arguments = (batch_dir, input_path, preset, items, task_runner, packager, reporter, log_file, translations,
                 review, preparer)
    if not hold_lock:
        return _run_in_folder(*arguments)
    from app.services import batch_runner

    batch_runner.acquire_lock(batch_dir)  # başka bir toplu üretim çalışıyorsa BatchBusy
    try:
        return _run_in_folder(*arguments)
    finally:
        batch_runner.release_lock()


def _run_in_folder(batch_dir: Path, input_path: Path, preset: Preset, items: list[BatchInput],
                   task_runner: Callable, packager: Callable, reporter: Callable, log_file: bool,
                   translations, review: bool | None = None, preparer: Callable | None = None) -> int:
    if (batch_dir / STATE_FILE).exists():
        state = BatchState.load(batch_dir)
        if state.input_sha256 != _file_sha256(input_path) or state.preset_sha256 != preset.sha256:
            raise BatchInputError(
                f"{batch_dir} başka bir girdi ya da reçeteyle başlatılmış; yeni klasör için --output vermeyin "
                "ya da --yeni kullanın"
            )
        for item in state.items:
            if item.status == "running":  # önceki çalıştırma bu konuda kesildi
                item.status = "pending"
        logger.info(f"toplu üretim kaldığı yerden devam ediyor: {batch_dir}")
    else:
        state = new_state(input_path, preset, items)
    if state.review_before_render is None:
        state.review_before_render = preset.review_before_render if review is None else bool(review)
    elif review is not None and bool(review) != state.review_before_render:
        logger.warning("toplu işin akışı açılırken belirlendi; --inceleme / --inceleme-yok bu klasörde yok sayıldı")
    if state.review_before_render:
        return _run_review_folder(state, batch_dir, preset, "prepare", task_runner, packager, reporter, log_file,
                                  translations, preparer)
    if translations is not None:
        state.subtitle_translations = list(translations)
    if state.subtitle_translations is not None:
        preset = replace(preset, subtitle_translations=tuple(state.subtitle_translations))
    state.finished = ""
    state.save(batch_dir)

    log_handler = logger.add(str(batch_dir / LOG_FILE), level="INFO", encoding="utf-8") if log_file else None
    apply_app_overrides(preset)
    started = time.monotonic()
    done = sum(item.status == "done" for item in state.items)
    logger.info(f"toplu üretim: {len(state.items)} konu, {done} tanesi daha önce bitti, klasör {batch_dir}")
    utils.log_ffmpeg_version()  # süreç başına bir kez; batch.log'da hangi ffmpeg'in kullanıldığı görünsün
    interrupted = False
    try:
        # İlk tur bitmemiş her konuyu (önceki çalıştırmada başarısız olanlar dahil) bir kez dener;
        # ikinci tur bu çalıştırmada başarısız olanları bir kez daha.
        for retry in (False, True):
            for item in state.items:
                if item.status == "done" or (retry and item.status != "failed"):
                    continue
                if retry:
                    logger.info(f"toplu üretim: başarısız konu yeniden deneniyor: {item.subject}")
                _process(item, state, preset, batch_dir, task_runner, packager)
                reporter(state, batch_dir)
    except KeyboardInterrupt:
        interrupted = True
        for item in state.items:
            if item.status == "running":
                item.status = "pending"
        logger.warning("toplu üretim durduruldu; aynı komutla kaldığı yerden devam edebilirsiniz")
    finally:
        state.run_seconds = round(state.run_seconds + time.monotonic() - started, 1)
        if not interrupted and all(item.status in ("done", "failed") for item in state.items):
            state.finished = datetime.now().isoformat(timespec="seconds")
        state.save(batch_dir)
        try:
            reporter(state, batch_dir)
        finally:
            if log_handler is not None:
                logger.remove(log_handler)
    failed = [item for item in state.items if item.status == "failed"]
    logger.info(
        f"toplu üretim bitti: {sum(i.status == 'done' for i in state.items)} başarılı, {len(failed)} başarısız, "
        f"rapor {batch_dir / REPORT_FILE}"
    )
    if interrupted:
        return 130
    return 1 if failed else 0


# ---------------------------------------------------------------- iki aşamalı akış

def _prepare_item(item: ItemState, state: BatchState, preset: Preset, batch_dir: Path, preparer: Callable) -> None:
    """Metin + terimler + çapalar + denetim; sonuç konu klasöründeki hazirlik.json'a, konu onay beklemeye."""
    from app.services import batch_review

    item.status, item.error, item.failed_stage = PREPARING, "", ""
    item.prepare_attempts += 1
    item.prepare_task_id = str(uuid.uuid4())
    item.started = datetime.now().isoformat(timespec="seconds")
    state.save(batch_dir)
    started = time.monotonic()
    logger.info(f"toplu üretim [{item.index}/{len(state.items)}] başladı (hazırlık): {item.subject}")
    try:
        preparation = preparer(preset, item, item.prepare_task_id)
    except KeyboardInterrupt:
        item.prepare_seconds = round(item.prepare_seconds + time.monotonic() - started, 1)
        raise
    except Exception as exc:
        item.status, item.failures = FAILED, item.failures + 1
        item.error = str(exc).strip()[:500] or type(exc).__name__
        item.failed_stage = getattr(exc, "stage", "") or "hazırlık"
        item.prepare_seconds = round(item.prepare_seconds + time.monotonic() - started, 1)
        state.save(batch_dir)
        logger.error(f"toplu üretim [{item.index}] hazırlanamadı: {item.subject}: {item.error}")
        return
    seconds = round(time.monotonic() - started, 1)
    item.prepare_seconds = round(item.prepare_seconds + seconds, 1)
    batch_review.write_preparation(batch_dir / item.folder, {
        **preparation, "subject": item.subject, "instruction": item.instruction, "task_id": item.prepare_task_id,
        "seconds": seconds, "prepared_at": datetime.now().isoformat(timespec="seconds"),
    })
    item.prepared, item.status = True, AWAITING_REVIEW
    state.save(batch_dir)
    counts = (preparation.get("audit") or {}).get("sayim") or {}
    logger.success(
        f"toplu üretim [{item.index}] onay bekliyor ({seconds:.0f} sn): {item.subject}; "
        f"{preparation.get('kelime')} kelime, ~{preparation.get('tahmini_sn')} sn, "
        f"denetim {' '.join(f'{k} {v}' for k, v in counts.items()) or (preparation.get('audit') or {}).get('durum', '-')}"
    )


def _review_finished(state: BatchState) -> bool:
    return all(item.status in (DONE, FAILED, SKIPPED) and (item.status != FAILED or item.prepared)
               for item in state.items)


def _render_snapshot(state: BatchState, batch_dir: Path) -> dict[int, dict]:
    """
    Render süreci başlarken onay kararlarını review.json'dan bir kez okur: geçerli onaylı ve henüz videosu
    olmayan konular bu turda üretilir; sonradan onaylananlar sonraki "render" çağrısını bekler.
    """
    from app.services import batch_review

    snapshot = {}
    for item in state.items:
        if item.status == DONE or not item.prepared:
            continue
        review = batch_review.load_review(batch_dir / item.folder)
        if review and review.get("skipped"):
            item.status = SKIPPED
            continue
        approved = batch_review.approved_copy(review)
        if approved is None:
            item.status = AWAITING_REVIEW
            continue
        snapshot[item.index] = approved
        item.status = APPROVED
    return snapshot


def _run_review_folder(state: BatchState, batch_dir: Path, preset: Preset, phase: str, task_runner: Callable,
                       packager: Callable, reporter: Callable, log_file: bool, translations,
                       preparer: Callable | None) -> int:
    """İki aşamalı akışın bir aşaması: "prepare" (metinler) ya da "render" (onaylı konuların videoları)."""
    from app.services import batch_review

    preparer = preparer or batch_review.prepare_item
    for item in state.items:  # önceki çalıştırma bu konularda kesildi
        if item.status in (RUNNING, PREPARING):
            item.status = PENDING
        elif item.status == RENDERING:
            item.status = APPROVED
    if translations is not None:
        state.subtitle_translations = list(translations)
    if state.subtitle_translations is not None:
        preset = replace(preset, subtitle_translations=tuple(state.subtitle_translations))
    state.finished = ""
    state.save(batch_dir)

    log_handler = logger.add(str(batch_dir / LOG_FILE), level="INFO", encoding="utf-8") if log_file else None
    apply_app_overrides(preset)
    started = time.monotonic()
    snapshot: dict[int, dict] = {}
    if phase == "render":
        snapshot = _render_snapshot(state, batch_dir)
        state.save(batch_dir)
        logger.info(f"toplu üretim: {len(state.items)} konu, render: {len(snapshot)} onaylı konu, klasör {batch_dir}")
    else:
        prepared = sum(item.prepared for item in state.items)
        logger.info(f"toplu üretim: {len(state.items)} konu, hazırlık: {prepared} tanesi daha önce hazırlandı, "
                    f"klasör {batch_dir}")
    utils.log_ffmpeg_version()
    interrupted = False
    try:
        for retry in (False, True):
            for item in state.items:
                if phase == "render":
                    if item.index not in snapshot or item.status == DONE or (retry and item.status != FAILED):
                        continue
                    if retry:
                        logger.info(f"toplu üretim: başarısız konu yeniden deneniyor: {item.subject}")
                    approved = snapshot[item.index]
                    _process(item, state, preset, batch_dir, task_runner, packager,
                             argv=build_render_argv(preset, item, approved), running_status=RENDERING,
                             package_extra={"review": approved})
                else:
                    if item.prepared or item.status not in (PENDING, FAILED) or (retry and item.status != FAILED):
                        continue
                    if retry:
                        logger.info(f"toplu üretim: hazırlanamayan konu yeniden deneniyor: {item.subject}")
                    _prepare_item(item, state, preset, batch_dir, preparer)
                reporter(state, batch_dir)
    except KeyboardInterrupt:
        interrupted = True
        for item in state.items:
            if item.status == PREPARING:
                item.status = PENDING
            elif item.status == RENDERING:
                item.status = APPROVED
        logger.warning("toplu üretim durduruldu; aynı komutla kaldığı yerden devam edebilirsiniz")
    finally:
        state.run_seconds = round(state.run_seconds + time.monotonic() - started, 1)
        if not interrupted and _review_finished(state):
            state.finished = datetime.now().isoformat(timespec="seconds")
        state.save(batch_dir)
        try:
            reporter(state, batch_dir)
        finally:
            if log_handler is not None:
                logger.remove(log_handler)
    if phase == "render":
        failed = [item for item in state.items if item.index in snapshot and item.status == FAILED]
        logger.info(
            f"toplu üretim render bitti: {sum(state.items[i - 1].status == DONE for i in snapshot)} başarılı, "
            f"{len(failed)} başarısız, rapor {batch_dir / REPORT_FILE}"
        )
    else:
        failed = [item for item in state.items if not item.prepared and item.status == FAILED]
        waiting = sum(item.status == AWAITING_REVIEW for item in state.items)
        logger.info(f"toplu üretim hazırlığı bitti: {waiting} konu onay bekliyor, {len(failed)} hazırlanamadı. "
                    f"Onaydan sonra videolar için: batch.py render {batch_dir}")
    if interrupted:
        return 130
    return 1 if failed else 0


def render_batch(
    batch_dir: str | Path,
    task_runner: Callable | None = None,
    packager: Callable | None = None,
    reporter: Callable | None = None,
    hold_lock: bool = False,
    log_file: bool = True,
) -> int:
    """
    İki aşamalı toplu işin render aşaması: review.json'da onaylanmış ve videosu olmayan konular, onaylanan
    metin ve terimlerle (LLM'e yeniden sorulmadan) üretilir. Girdi değil klasör alır; reçete state.json'daki
    yoldan okunur ve hazırlıktan beri değişmemiş olmalıdır. Çıkış kodları run_batch'teki gibi.
    """
    from app.services import batch_package

    batch_dir = Path(batch_dir)
    try:
        state = BatchState.load(batch_dir)
    except (OSError, ValueError, TypeError) as exc:
        raise BatchInputError(f"toplu iş okunamadı: {batch_dir} ({exc})") from exc
    if not state.review_before_render:
        raise BatchInputError(f"{batch_dir} iki aşamalı (onaylı) bir toplu iş değil; render komutu yalnız onunla")
    preset = load_preset(state.preset)
    if preset.sha256 != state.preset_sha256:
        raise PresetError(f"reçete hazırlıktan sonra değişmiş: {state.preset}")
    arguments = (state, batch_dir, preset, "render", task_runner or run_video_task,
                 packager or batch_package.build_package, reporter or batch_package.write_report, log_file, None, None)
    if not hold_lock:
        return _run_review_folder(*arguments)
    from app.services import batch_runner

    batch_runner.acquire_lock(batch_dir)
    try:
        return _run_review_folder(*arguments)
    finally:
        batch_runner.release_lock()


def main(argv: list[str] | None = None, prog: str = "batch.py") -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    from app.services import batch_review

    if argv and argv[0] in batch_review.COMMANDS:  # iki aşamalı akışın komutları: durum, render, onayla…
        return batch_review.cli_main(argv, prog)
    if argv and argv[0] == "paketi-yenile":  # bir revizyon için yükleme paketi (task_revisions)
        from app.services import task_revisions

        return task_revisions.package_cli(argv[1:], prog)
    parser = argparse.ArgumentParser(
        prog=prog,
        description="Bir .csv ya da .txt dosyasındaki konular için sırayla video üretir; her video için "
                    "yüklemeye hazır bir paket ve toplu bir kalite raporu (rapor.html) yazar. Reçetede "
                    "review_before_render açıksa önce yalnız metinleri hazırlar; onay ve render için: "
                    f"{prog} durum|onayla|yeniden-uret|yeniden-denetle|atla|temizleri-onayla|render <klasör> …",
    )
    parser.add_argument("input", help=".csv (konu, talimat, ses) ya da .txt (satır başına konu)")
    parser.add_argument("--preset", required=True, help="reçete dosyası (TOML), ör. presets/uzay.toml")
    parser.add_argument("--output", help="çıktı klasörü (varsayılan storage/batches/<tarih-saat>_<girdi>)")
    parser.add_argument("--yeni", action="store_true",
                        help="aynı girdi ve reçeteyle önceki bir toplu üretim olsa da yeni klasörde baştan başla")
    # WebUI (batch_runner) stdout/stderr'i batch.log'a yönlendirir; günlük ikinci kez yazılmaz.
    parser.add_argument("--gunluk-stderr", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--altyazi-cevirileri", metavar="DİLLER",
                        help="çeviri altyazı dilleri, virgülle (ör. en,de; boş = çeviri yok); reçetedeki "
                             "subtitle_translations yerine yalnız bu toplu iş için geçerli")
    flow = parser.add_mutually_exclusive_group()
    flow.add_argument("--inceleme", dest="review", action="store_const", const=True, default=None,
                      help="iki aşamalı akış: önce metinler hazırlanır ve onay beklenir (reçetedeki "
                           "review_before_render yerine, yalnız bu toplu iş için)")
    flow.add_argument("--inceleme-yok", dest="review", action="store_const", const=False,
                      help="tek aşamalı akış: metinler onay beklemeden videoya dönüşür")
    args = parser.parse_args(argv)
    import app.config  # noqa: F401 -- yapılandırma ve günlük ayarları
    from app.utils.logging_utils import configure_terminal_logger, get_logging_options

    configure_terminal_logger(sys.stderr, level=get_logging_options()[0], colorize=False)
    # Windows'ta Ctrl+Break (CTRL_BREAK_EVENT) da Ctrl+C gibi KeyboardInterrupt olur ve temiz durdurur.
    has_break = hasattr(signal, "SIGBREAK")
    previous_break = signal.signal(signal.SIGBREAK, signal.default_int_handler) if has_break else None
    from app.services.batch_runner import BatchBusy

    try:
        translations = (None if args.altyazi_cevirileri is None
                        else parse_languages(args.altyazi_cevirileri, "--altyazi-cevirileri"))
        extra = {} if args.review is None else {"review": args.review}
        return run_batch(args.input, args.preset, output=args.output, new=args.yeni, hold_lock=True,
                         log_file=not args.gunluk_stderr, translations=translations, **extra)
    except (BatchInputError, PresetError) as exc:
        logger.error(str(exc))
        return 2
    except BatchBusy as exc:
        logger.error(str(exc))
        return 3
    finally:
        if has_break:
            signal.signal(signal.SIGBREAK, previous_break or signal.SIG_DFL)


if __name__ == "__main__":  # pip kurulumunda batch_runner `python -m app.services.batch_production` kullanır
    from app.utils.logging_utils import force_utf8_console

    force_utf8_console()
    raise SystemExit(main())
