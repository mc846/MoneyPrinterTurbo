"""
Toplu üretim paketinin .srt altyazıları（供 YouTube 上传）。

- altyazi.<dil>.srt（视频语言，如 tr）：取任务的 subtitle.srt，按词对齐到朗读文本，补回断句时去掉的
  标点；规范时间（序号从 1 连续、结束晚于开始、不重叠、不超过视频长度、无空字幕）；每行最多 42 个字符、
  每条最多 2 行，超出的按词边界拆成几条，时间按字符比例分配。只生成 .srt 文件，视频里烧录的字幕不变。
- 翻译：字幕文本一次 LLM 调用、按顺序以 JSON 数组翻译（长度必须相同；不同就重试一次，仍不同则分块、
  再逐条翻译）。时间与源语言文件一一对应；译文超出两行时在原时间段内拆分并记为警告。
- 轻量质量检查：空译文、看起来未翻译（土耳其语特有字母）、长度比例异常，作为警告写入 kalite.json。
文件为 UTF-8（无 BOM）、CRLF 换行。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from loguru import logger

from app.services import llm

MAX_LINE = 42
MAX_LINES = 2
NEWLINE = "\r\n"
CHUNK_SIZE = 6
# Satırlar noktalamadan sonra bölünmeyi tercih eder.
_BREAK_AFTER = (",", ".", ";", ":", "?", "!", "…")
_TIME = re.compile(r"(\d{2,}):(\d{2}):(\d{2}),(\d{3})")
_TIMING = re.compile(r"^(\d{2}):([0-5]\d):([0-5]\d),(\d{3}) --> (\d{2}):([0-5]\d):([0-5]\d),(\d{3})$")
# Türkçe ve Azericeye özgü harfler: başka dile çevrilmiş bir satırda görünüyorsa satır çevrilmemiş olabilir.
TURKISH_ONLY_LETTERS = set("ğĞıİşŞ")
LANGUAGE_CODE = re.compile(r"^[a-z]{2,3}(-[A-Za-z0-9]{2,8})*$")
# Çeviri dilleri: kod → (İngilizce adı, kendi adı). WebUI çoklu seçimi buradan gelir.
LANGUAGES = {
    "en": ("English", "English"), "de": ("German", "Deutsch"), "fr": ("French", "Français"),
    "es": ("Spanish", "Español"), "it": ("Italian", "Italiano"), "pt": ("Portuguese", "Português"),
    "nl": ("Dutch", "Nederlands"), "pl": ("Polish", "Polski"), "ru": ("Russian", "Русский"),
    "uk": ("Ukrainian", "Українська"), "ar": ("Arabic", "العربية"), "az": ("Azerbaijani", "Azərbaycanca"),
    "tr": ("Turkish", "Türkçe"), "id": ("Indonesian", "Bahasa Indonesia"), "vi": ("Vietnamese", "Tiếng Việt"),
    "zh": ("Chinese (Simplified)", "中文"), "ja": ("Japanese", "日本語"), "ko": ("Korean", "한국어"),
}
# Uzunluk oranı (çeviri / kaynak) bu aralığın dışındaysa uyarı; yalnız en az 20 karakterlik kaynaklarda.
RATIO_RANGE = (0.5, 2.0)
RATIO_MIN_SOURCE = 20

TRANSLATION_PROMPT = """
# Role: Subtitle translator

Translate the {source_name} subtitle lines of a short science video into {target_name} ({code}).

## Rules
1. Write natural, fluent {target_name} that keeps the meaning exactly: do not add, drop or soften
   information.
2. Use the established {target_name} terms for scientific concepts. Keep proper names (people, objects,
   missions, catalogue designations such as "SN 2012Z" or "PSR B1919+21") as they are.
3. Do not attribute any information to an organization (no "according to NASA", "NASA says") and do not
   imply that an organization endorses the video.
4. Each translated line must fit in two subtitle lines of {max_line} characters (at most {max_chars}
   characters). Prefer short wording.
5. Translate line by line: return exactly {count} strings, in the same order, one per input line, even
   when a sentence continues on the next line.
6. Respond ONLY with a JSON array of strings, no markdown and no commentary.

## Full text (context only)
{context}

## Lines to translate ({count})
{lines}
""".strip()


@dataclass
class Cue:
    start: int  # milisaniye
    end: int
    text: str


# ---------------------------------------------------------------- okuma ve yazma

def _milliseconds(parts) -> int:
    hours, minutes, seconds, millis = (int(part) for part in parts)
    return ((hours * 60 + minutes) * 60 + seconds) * 1000 + millis


def _stamp(milliseconds: int) -> str:
    hours, rest = divmod(max(0, int(milliseconds)), 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    seconds, millis = divmod(rest, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def parse_srt(text: str) -> list[Cue]:
    """Esnek okuyucu: BOM, CRLF/LF, eksik sıra numarası; satırları tek boşlukla birleştirir."""
    # Tek kalan CR atılır: metin kipinde yazılmış CRLF Windows'ta "\r\r\n" olur.
    text = (text or "").lstrip("﻿").replace("\r\n", "\n").replace("\r", "")
    cues = []
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = block.split("\n")
        timing = next((index for index, line in enumerate(lines) if "-->" in line), None)
        if timing is None:
            continue
        times = _TIME.findall(lines[timing])
        if len(times) < 2:
            continue
        body = " ".join(line.strip() for line in lines[timing + 1:] if line.strip())
        cues.append(Cue(_milliseconds(times[0]), _milliseconds(times[1]), body))
    return cues


def format_srt(cues: list[Cue]) -> str:
    blocks = [
        f"{number}{NEWLINE}{_stamp(cue.start)} --> {_stamp(cue.end)}{NEWLINE}{NEWLINE.join(layout(cue.text))}"
        for number, cue in enumerate(cues, start=1)
    ]
    return (NEWLINE * 2).join(blocks) + NEWLINE if blocks else ""


# ---------------------------------------------------------------- satır kuralları

def _split_long_words(words: list[str]) -> list[str]:
    """42 karakteri aşan tek bir kelime tireyle bölünür (kelime sınırı yoksa tek çıkış yolu)."""
    result = []
    for word in words:
        while len(word) > MAX_LINE:
            result.append(word[:MAX_LINE - 1] + "-")
            word = word[MAX_LINE - 1:]
        result.append(word)
    return result


def _wrap(words: list[str]) -> list[str]:
    lines, current = [], ""
    for word in words:
        candidate = f"{current} {word}" if current else word
        if len(candidate) <= MAX_LINE:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def layout(text: str) -> list[str]:
    """Bir altyazının satırları: 42 karakteri aşıyorsa en dengeli iki satır (noktalamadan sonra bölmek yeğlenir)."""
    words = _split_long_words(text.split())
    joined = " ".join(words)
    if len(joined) <= MAX_LINE:
        return [joined]
    best = None
    for index in range(1, len(words)):
        first, second = " ".join(words[:index]), " ".join(words[index:])
        if len(first) <= MAX_LINE and len(second) <= MAX_LINE:
            score = abs(len(first) - len(second)) - (8 if words[index - 1].endswith(_BREAK_AFTER) else 0)
            if best is None or score < best[0]:
                best = (score, [first, second])
    return best[1] if best else _wrap(words)


def _fits(words: list[str]) -> bool:
    return len(layout(" ".join(words))) <= MAX_LINES


def _greedy_groups(words: list[str]) -> list[list[str]]:
    groups, current = [], []
    for word in words:
        if current and not _fits(current + [word]):
            groups.append(current)
            current = []
        current.append(word)
    return groups + [current]


def _balanced_groups(words: list[str]) -> list[list[str]]:
    """
    Açgözlü bölmenin gerektirdiği kadar parça, ama uzunlukları dengeli; noktalamayla biten parça ödüllenir.
    Dinamik programlama: maliyet = Σ (parça uzunluğu − ortalama)² − noktalama ödülü.
    """
    count = len(_greedy_groups(words))
    target = len(" ".join(words)) / count
    best: dict[tuple[int, int], tuple[float, int]] = {(0, 0): (0.0, -1)}
    for group in range(1, count + 1):
        for end in range(1, len(words) + 1):
            for start in range(group - 1, end):
                previous = best.get((start, group - 1))
                if previous is None or not _fits(words[start:end]):
                    continue
                length = len(" ".join(words[start:end]))
                bonus = 150 if end < len(words) and words[end - 1].endswith(_BREAK_AFTER) else 0
                cost = previous[0] + (length - target) ** 2 - bonus
                if (end, group) not in best or cost < best[(end, group)][0]:
                    best[(end, group)] = (cost, start)
    if (len(words), count) not in best:
        return _greedy_groups(words)
    groups, end = [], len(words)
    for group in range(count, 0, -1):
        start = best[(end, group)][1]
        groups.append(words[start:end])
        end = start
    return groups[::-1]


def split_cue(cue: Cue) -> list[Cue]:
    """İki satıra sığmayan altyazıyı kelime sınırından böler; süre karakter sayısıyla orantılı paylaşılır."""
    if _fits(cue.text.split()):
        return [cue]
    texts = [" ".join(group) for group in _balanced_groups(_split_long_words(cue.text.split())) if group]
    total, span = sum(len(text) for text in texts), cue.end - cue.start
    pieces, start = [], cue.start
    for index, text in enumerate(texts):
        end = cue.end if index == len(texts) - 1 else start + round(span * len(text) / total)
        end = max(end, start + 1)
        pieces.append(Cue(start, end, text))
        start = end
    return pieces


# ---------------------------------------------------------------- Türkçe dosya

def _key(word: str) -> str:
    return re.sub(r"[\W_]+", "", word.casefold())


def restore_punctuation(cues: list[Cue], narration: str) -> tuple[list[Cue], bool]:
    """
    Altyazı metnini seslendirilen metne kelime kelime hizalar ve kelimeleri metindeki halleriyle (noktalama
    dahil) yazar. Hizalanamazsa (kelimeler farklıysa) altyazılar olduğu gibi döner ve ikinci değer False olur.
    """
    tokens = narration.split()
    position, restored = 0, []
    for cue in cues:
        taken = []
        for word in cue.text.split():
            while position < len(tokens) and not _key(tokens[position]):  # yalnız noktalama ("—")
                taken.append(tokens[position])
                position += 1
            key = _key(word)
            if not key:
                continue
            if position >= len(tokens) or _key(tokens[position]) != key:
                return cues, False
            taken.append(tokens[position])
            position += 1
        restored.append(Cue(cue.start, cue.end, " ".join(taken) if taken else cue.text))
    rest = tokens[position:]
    if any(_key(token) for token in rest):
        return cues, False
    if rest and restored:
        restored[-1].text = f"{restored[-1].text} {' '.join(rest)}"
    return restored, True


def normalize_cues(cues: list[Cue], duration_ms: int | None = None) -> list[Cue]:
    """Boş altyazılar atılır; çakışan bitiş sonrakinin başlangıcına çekilir; son bitiş video süresini aşmaz."""
    cues = [Cue(max(0, cue.start), cue.end, " ".join(cue.text.split())) for cue in cues if cue.text.strip()]
    cues.sort(key=lambda cue: cue.start)
    for index, cue in enumerate(cues):
        if index + 1 < len(cues):
            cue.end = min(cue.end, cues[index + 1].start)
        if duration_ms is not None:
            cue.end = min(cue.end, duration_ms)
    kept = []
    for cue in cues:
        if cue.end > cue.start:
            kept.append(cue)
        elif kept:  # süresi kalmayan altyazının metni öncekine eklenir
            kept[-1].text = f"{kept[-1].text} {cue.text}"
    return kept


def build_source_srt(subtitle_path: Path, narration: str, duration_ms: int | None) -> tuple[list[Cue], dict]:
    cues = parse_srt(subtitle_path.read_text(encoding="utf-8-sig"))
    restored, aligned = restore_punctuation(cues, narration)
    normalized = normalize_cues(restored, duration_ms)
    final = [piece for cue in normalized for piece in split_cue(cue)]
    return final, {"kaynak_altyazi": len(cues), "altyazi": len(final), "bolunen": len(final) - len(normalized),
                   "noktalama_geri_geldi": aligned}


# ---------------------------------------------------------------- doğrulayıcı

def validate_srt(text: str, duration_ms: int | None = None) -> list[str]:
    """YouTube uyumluluğu ve okunabilirlik kuralları; sorun yoksa boş liste."""
    problems = []
    if text.startswith("﻿"):
        problems.append("BOM var")
    if "\r\n" in text and re.search(r"(?<!\r)\n", text):
        problems.append("satır sonları karışık (CRLF ve LF)")
    blocks = [block for block in re.split(r"\r?\n\r?\n", text.strip("\r\n")) if block.strip()]
    if not blocks:
        return problems + ["altyazı yok"]
    previous_end = None
    for expected, block in enumerate(blocks, start=1):
        lines = re.split(r"\r?\n", block)
        if lines[0].strip() != str(expected):
            problems.append(f"{expected}. altyazı: sıra numarası {lines[0].strip()!r}")
        timing = _TIMING.match(lines[1].strip()) if len(lines) > 1 else None
        if not timing:
            problems.append(f"{expected}. altyazı: zaman biçimi")
            continue
        start, end = _milliseconds(timing.groups()[:4]), _milliseconds(timing.groups()[4:])
        if end <= start:
            problems.append(f"{expected}. altyazı: bitiş başlangıçtan önce")
        if previous_end is not None and start < previous_end:
            problems.append(f"{expected}. altyazı: öncekiyle çakışıyor")
        previous_end = end
        body = [line for line in lines[2:]]
        if not any(line.strip() for line in body):
            problems.append(f"{expected}. altyazı: boş")
        if len(body) > MAX_LINES:
            problems.append(f"{expected}. altyazı: {len(body)} satır")
        for line in body:
            if len(line) > MAX_LINE:
                problems.append(f"{expected}. altyazı: {len(line)} karakterlik satır")
    if duration_ms is not None and previous_end is not None and previous_end > duration_ms:
        problems.append("son altyazı video süresini aşıyor")
    return problems


# ---------------------------------------------------------------- çeviri

def _parse_array(response: str, count: int) -> list[str] | None:
    """LLM yanıtındaki JSON dizisi; dizi değilse ya da uzunluğu farklıysa None."""
    text = llm._strip_code_fence(str(response or ""))
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        try:
            data = llm._extract_embedded_json(text, "[")
        except ValueError:
            return None
    if not isinstance(data, list) or len(data) != count or not all(isinstance(item, (str, int, float))
                                                                   for item in data):
        return None
    return [str(item).strip() for item in data]


def translate_texts(texts: list[str], code: str, context: str, ask, source: str = "tr") -> tuple[list[str], dict]:
    """
    Metinleri sırayla çevirir. Önce tek çağrı (uzunluk farklıysa bir kez daha); olmazsa CHUNK_SIZE'lık
    parçalar, bir parça yine tutmazsa o parçanın satırları tek tek çevrilir.
    """
    calls = 0

    def attempt(chunk: list[str]) -> list[str] | None:
        nonlocal calls
        calls += 1
        prompt = TRANSLATION_PROMPT.format(
            source_name=LANGUAGES.get(source, (source,))[0], target_name=LANGUAGES.get(code, (code,))[0], code=code,
            max_line=MAX_LINE, max_chars=MAX_LINE * MAX_LINES, count=len(chunk), context=context,
            lines=json.dumps(chunk, ensure_ascii=False, indent=1),
        )
        try:
            return _parse_array(ask(prompt), len(chunk))
        except Exception as exc:
            logger.warning(f"altyazı çevirisi alınamadı ({code}): {type(exc).__name__}: {exc}")
            return None

    for _ in range(2):
        result = attempt(texts)
        if result is not None:
            return result, {"yontem": "tek çağrı" if calls == 1 else "yeniden deneme", "cagri": calls}
        logger.warning(f"altyazı çevirisi ({code}) dizi uzunluğu tutmadı, yeniden deneniyor")
    translated = []
    for index in range(0, len(texts), CHUNK_SIZE):
        chunk = texts[index:index + CHUNK_SIZE]
        result = attempt(chunk)
        if result is None:
            result = [(attempt([text]) or [""])[0] for text in chunk]
        translated.extend(result)
    return translated, {"yontem": "parça parça", "cagri": calls}


def translation_warnings(sources: list[str], translations: list[str], code: str) -> list[str]:
    warnings = []
    for number, (source, target) in enumerate(zip(sources, translations), start=1):
        if not target.strip():
            warnings.append(f"{number}. altyazı: çeviri boş")
            continue
        if code.split("-")[0] not in ("tr", "az") and (TURKISH_ONLY_LETTERS & set(target)
                                                         or (len(source.split()) >= 3 and target == source)):
            warnings.append(f"{number}. altyazı: çevrilmemiş görünüyor: {target!r}")
        if len(source) >= RATIO_MIN_SOURCE:
            ratio = len(target) / len(source)
            if not RATIO_RANGE[0] <= ratio <= RATIO_RANGE[1]:
                warnings.append(f"{number}. altyazı: uzunluk oranı {ratio:.2f}")
    return warnings


def build_translation(cues: list[Cue], code: str, context: str, ask, source: str = "tr") -> tuple[list[Cue], dict]:
    """Kaynak altyazılarla aynı zamanlamada çeviri; iki satıra sığmayan çeviri kendi zaman diliminde bölünür."""
    sources = [cue.text for cue in cues]
    translations, info = translate_texts(sources, code, context, ask, source=source)
    warnings = translation_warnings(sources, translations, code)
    result, split = [], 0
    for cue, text in zip(cues, translations):
        text = " ".join(text.split())
        if not text:
            continue  # boş altyazı YouTube'da hata verir; uyarısı yukarıda
        pieces = split_cue(Cue(cue.start, cue.end, text))
        split += len(pieces) - 1
        result.extend(pieces)
    if split:
        warnings.append(f"{split} altyazı iki satıra sığmadı, kendi süresi içinde bölündü")
    return result, {**info, "altyazi": len(result), "uyarilar": warnings}


# ---------------------------------------------------------------- paket

def srt_name(code: str) -> str:
    return f"altyazi.{code}.srt"


def write_subtitles(folder: Path, subtitle_path: Path, narration: str, duration_ms: int | None,
                    source: str, translations, ask) -> dict:
    """altyazi.<kaynak>.srt ve her çeviri dili için altyazi.<dil>.srt; kalite.json için özet döner."""
    if not subtitle_path.is_file():
        return {"dosyalar": [], "uyarilar": ["görevde subtitle.srt yok (altyazı kapalı mı?)"]}
    cues, source_info = build_source_srt(subtitle_path, narration, duration_ms)
    files, warnings, details = [], [], {source: source_info}
    if not source_info["noktalama_geri_geldi"]:
        warnings.append(f"{srt_name(source)}: altyazı metni seslendirilen metne hizalanamadı, noktalama eksik")
    text = format_srt(cues)
    problems = validate_srt(text, duration_ms)
    source_info["sorunlar"] = problems
    warnings += [f"{srt_name(source)}: {problem}" for problem in problems]
    (folder / srt_name(source)).write_bytes(text.encode("utf-8"))
    files.append(srt_name(source))
    context = " ".join(cue.text for cue in cues)
    for code in translations or ():
        translated, info = build_translation(cues, code, context, ask, source=source)
        text = format_srt(translated)
        info["sorunlar"] = validate_srt(text, duration_ms)
        details[code] = info
        warnings += [f"{srt_name(code)}: {warning}" for warning in info["uyarilar"] + info["sorunlar"]]
        if translated:
            (folder / srt_name(code)).write_bytes(text.encode("utf-8"))
            files.append(srt_name(code))
    return {"dosyalar": files, "uyarilar": warnings, **details}
