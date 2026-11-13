"""
Visual screening of stock candidates with a vision LLM (``material_vision_screening``).

A candidate that passed relevance, anchors and the source's own filters is shown to a
vision model before it is used: a still for images (NASA's ``~small`` preview, the
Pixabay thumbnail), three frames of the chosen clean window for NASA videos (its
start, middle and end, one request). The model answers one JSON object
(``PROMPT_VERSION``): image_type, subject, subject_matches_anchor, text_overlay,
person, logo. The rules of a real-footage channel (``decide``) turn it down when it is
an illustration, CGI or a diagram, when its subject is not one of the task's anchors,
when it shows a recognizable person or a logo. A data visualization is accepted and
marked ``label`` (it needs an on-screen label such as "NASA SVS · LRO data
visualization" once that label exists).

Backends (``material_vision_model`` = "<backend>:<model>"): ``claude`` runs the claude
CLI headless with stream-json image input and no tools (the subscription login, like
the claude_code LLM provider); ``ollama`` posts to /api/chat (e.g. gemma4:31b-cloud);
``gemini`` calls the Gemini API with google-genai and ``gemini_api_key`` (the key of the
Gemini LLM provider), the same prompt plus the answer's JSON schema. Gemini's quota is per
project, so its requests are spaced across tasks and threads by
``material_vision_gemini_rpm``; a 429 waits the server's retry delay and tries again
(``GEMINI_RETRIES``), a daily or zero quota stops the screening for the task. The key
never goes to a log, an error text, the cache or a record.

Per task (``Session``): at most ``material_vision_max_per_term`` candidates screened per term
and source, cached decisions included, so a warm cache decides the same as a cold one (then
the term moves on to the next source of the chain), and ``material_vision_max_per_task``
model calls in all. When the backend fails (daily quota, tier, session limit, login,
timeout) and ``material_vision_fallback_model`` names another model, the task switches to it
once (the same candidate is asked again); within the task a candidate keeps the decision it
got, whichever model gave it. When the task limit is reached, or no model is left, screening
stops for the task with a warning, the remaining candidates are used unscreened and the
quality report says the screening did not complete (a batch video showing an unscreened
scene needs a check). Decisions are cached on disk by provider, asset, frames, prompt
version, model and anchors.
"""

from __future__ import annotations

import base64
import contextvars
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

import requests
from loguru import logger
from PIL import Image

from app.config import config
from app.services import task_materials
from app.utils import utils

PROMPT_VERSION = 1
DEFAULT_MODEL = "claude:sonnet"
DEFAULT_MAX_PER_TERM = 3
DEFAULT_MAX_PER_TASK = 40
LONG_EDGE = 768
CALL_TIMEOUT = 180
IMAGE_TYPES = ("observation", "photo", "data_visualization", "illustration", "cgi", "diagram")
PERSON_VALUES = ("none", "unrecognizable", "recognizable")
REJECT_TYPES = frozenset({"illustration", "cgi", "diagram"})
LABEL_TYPES = frozenset({"data_visualization"})
# The answer's JSON schema, the same fields as the prompt (Gemini's structured output).
RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "image_type": {"type": "string", "enum": list(IMAGE_TYPES)},
        "subject": {"type": "string"},
        "subject_matches_anchor": {"type": "boolean"},
        "text_overlay": {"type": "boolean"},
        "person": {"type": "string", "enum": list(PERSON_VALUES)},
        "logo": {"type": "boolean"},
    },
    "required": ["image_type", "subject", "subject_matches_anchor", "text_overlay", "person", "logo"],
}
DEFAULT_GEMINI_MODEL = "gemini-3.5-flash-lite"
DEFAULT_GEMINI_RPM = 10
GEMINI_RETRIES = 3
GEMINI_MAX_WAIT = 90.0
GEMINI_RETRY_CODES = frozenset({429, 500, 502, 503, 504})

SYSTEM = ("You are an image screening assistant for a science video channel that uses real footage only. "
          "Reply with one JSON object only.")
PROMPT = """{count_text} of one candidate clip for a short vertical science video.
Video topic: {subject}
Search term the clip was found for: "{term}"
Anchors (what the video is about; any one of them is enough): {anchors}
The source's title and keywords (may be incomplete or wrong; trust the image): {context}

Return only this JSON object, no other text:
{{"image_type": "observation"|"photo"|"data_visualization"|"illustration"|"cgi"|"diagram", "subject": "<the main celestial body or object shown, a few words>", "subject_matches_anchor": true|false, "text_overlay": true|false, "person": "none"|"unrecognizable"|"recognizable", "logo": true|false}}

Definitions:
- image_type: "observation" real imagery of space, a celestial body or Earth taken by a telescope, a spacecraft or a camera (processed telescope images included); "photo" an ordinary real photograph or filmed footage of people, hardware, buildings or places; "data_visualization" a rendering computed from real measured data (a globe, map or animation made from spacecraft data); "illustration" an artist's concept, painting or drawing; "cgi" a computer-generated 3D render or animation of things that were not filmed (planned spacecraft, bases, rovers on other worlds, animated scenes); "diagram" a chart, plot, infographic or labeled schematic.
- subject: the main thing shown, e.g. "the Moon", "Mars", "Mercury", "a rocket tank in a factory".
- subject_matches_anchor: true if the main subject is any one of the anchors, a part of it or a view of it (a lunar crater, the lunar surface or the Moon seen from Earth all match "moon"); false if the main subject is another world or an unrelated object.
- text_overlay: any readable text (titles, captions, labels, signs).
- person: "recognizable" if a human face is visible clearly enough to identify the person; "unrecognizable" if people are present but cannot be identified (from behind, tiny, in a suit); otherwise "none".
- logo: an organization logo, insignia, mission patch or brand name is visible.
With several frames, describe the clip as a whole: a type or a problem visible in any frame counts."""


def context_text(item) -> str:
    """The candidate's title and keywords for the prompt (NASA's tags end with the title)."""
    info = item.source_info if isinstance(item.source_info, dict) else {}
    tags = [str(tag) for tag in info.get("tags") or [] if isinstance(tag, str) and tag.strip()]
    if info.get("provider") == "nasa" and tags:
        tags = [tags[-1], *tags[:-1]]
    return "; ".join(tags)[:240] or "(none)"


def build_prompt(frames: int, subject: str, term: str, anchors: Sequence[str], context: str) -> str:
    return PROMPT.format(
        count_text="The images are three frames (start, middle, end)" if frames > 1 else "The image is a frame",
        subject=subject or term, term=term, anchors=", ".join(anchors) if anchors else "(none)",
        context=context or "(none)")


class VisionUnavailable(RuntimeError):
    """The backend cannot answer for this task (quota, session limit, login, timeout, missing CLI)."""


# ---------------------------------------------------------------- settings


def _flag(key: str, default: bool) -> bool:
    value = config.app.get(key, default)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _count(key: str, default: int) -> int:
    value = config.app.get(key, default)
    try:
        if isinstance(value, bool):
            raise ValueError
        number = int(value)
    except (TypeError, ValueError):
        logger.warning(f"invalid {key}={value!r}, using {default}")
        return default
    return max(1, number)


def enabled() -> bool:
    """material_vision_screening, off by default."""
    return _flag("material_vision_screening", False)


def parse_model(value: Any) -> tuple[str, str]:
    """
    (backend, model) of a material_vision_model value: "claude:sonnet",
    "ollama:gemma4:31b-cloud" (the model keeps its own colon), "gemini:gemini-3.5-flash-lite"
    ("models/" dropped; "gemini:" alone is DEFAULT_GEMINI_MODEL). An unknown backend falls
    back to DEFAULT_MODEL with a warning.
    """
    text = str(value or "").strip() or DEFAULT_MODEL
    backend, _, model = text.partition(":")
    backend, model = backend.strip().lower(), model.strip()
    if backend not in BACKENDS:
        logger.warning(f"unknown material_vision_model backend {text!r}, using {DEFAULT_MODEL}")
        backend, _, model = DEFAULT_MODEL.partition(":")
    if backend == "gemini":
        model = model.removeprefix("models/").strip() or DEFAULT_GEMINI_MODEL
    return backend, model


def model_setting() -> tuple[str, str]:
    """(backend, model) from material_vision_model."""
    return parse_model(config.app.get("material_vision_model"))


def fallback_setting(primary: tuple[str, str]) -> tuple[str, str] | None:
    """(backend, model) from material_vision_fallback_model; None when empty or the same as the primary."""
    value = str(config.app.get("material_vision_fallback_model") or "").strip()
    if not value:
        return None
    fallback = parse_model(value)
    return None if fallback == primary else fallback


# ---------------------------------------------------------------- the model's answer


def parse(text: str) -> dict | None:
    """The JSON object of an answer (code fences and surrounding text tolerated), validated."""
    if not text:
        return None
    text = re.sub(r"^```(?:json)?|```$", "", str(text).strip(), flags=re.M).strip()
    try:
        data = json.loads(text)
    except ValueError:
        match = re.search(r"\{.*\}", text, re.S)
        try:
            data = json.loads(match.group(0)) if match else None
        except ValueError:
            data = None
    if not isinstance(data, dict):
        return None
    image_type = str(data.get("image_type") or "").strip().lower()
    person = str(data.get("person") or "").strip().lower()
    if image_type not in IMAGE_TYPES or person not in PERSON_VALUES:
        return None
    flags = {}
    for key in ("subject_matches_anchor", "text_overlay", "logo"):
        value = data.get(key)
        if isinstance(value, str) and value.strip().lower() in ("true", "false"):
            value = value.strip().lower() == "true"
        if not isinstance(value, bool):
            return None
        flags[key] = value
    return {"image_type": image_type, "subject": str(data.get("subject") or "").strip()[:80],
            "person": person, **flags}


def decide(result: dict, anchors: Sequence[str]) -> tuple[bool, list[str], str | None]:
    """(use, reasons, label) under the real-footage rules."""
    reasons = []
    if result["image_type"] in REJECT_TYPES:
        reasons.append(f"type:{result['image_type']}")
    if anchors and not result["subject_matches_anchor"]:
        reasons.append(f"subject:{result['subject'] or 'other'}")
    if result["person"] == "recognizable":
        reasons.append("person")
    if result["logo"]:
        reasons.append("logo")
    label = result["image_type"] if result["image_type"] in LABEL_TYPES else None
    return not reasons, reasons, label


# ---------------------------------------------------------------- backends


def _encode(path: str) -> str:
    with Image.open(path) as image:
        image = image.convert("RGB")
        image.thumbnail((LONG_EDGE, LONG_EDGE), Image.LANCZOS)
        buffer = io.BytesIO()
        image.save(buffer, "JPEG", quality=85)
    return base64.b64encode(buffer.getvalue()).decode()


def _claude_cli() -> str:
    configured = str(config.app.get("claude_code_cli_path") or "").strip() or "claude"
    path = shutil.which(configured) or (configured if os.path.isfile(configured) else "")
    if not path:
        raise VisionUnavailable(f"claude CLI not found ({configured!r})")
    return path


def ask_claude(images: list[str], prompt: str, model: str) -> str:
    """One headless claude CLI call: stream-json input with the images, no tools, safe mode."""
    from app.services import llm

    content = [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": data}}
               for data in images]
    content.append({"type": "text", "text": prompt})
    message = {"type": "user", "message": {"role": "user", "content": content}}
    command = [_claude_cli(), "-p", "--input-format", "stream-json", "--output-format", "stream-json",
               "--verbose", "--system-prompt", SYSTEM, "--tools", "", "--safe-mode"]
    if model:
        command += ["--model", model]
    env, _removed = llm.build_claude_code_env()
    # Started from inside a Claude Code session (e.g. a batch run from a terminal agent), the
    # nested CLI would see itself as a sub-session; the screening is an independent call.
    env.pop("CLAUDECODE", None)
    with tempfile.TemporaryDirectory() as work_dir:
        try:
            completed = subprocess.run(command, input=json.dumps(message) + "\n", capture_output=True, text=True,
                                       encoding="utf-8", errors="replace", timeout=CALL_TIMEOUT, cwd=work_dir,
                                       env=env)
        except subprocess.TimeoutExpired as exc:
            raise VisionUnavailable(f"claude CLI timed out after {CALL_TIMEOUT}s") from exc
    events = []
    for line in (completed.stdout or "").splitlines():
        if line.strip().startswith("{"):
            try:
                events.append(json.loads(line))
            except ValueError:
                pass
    result = next((event for event in events if event.get("type") == "result"), None)
    if result is None:
        raise VisionUnavailable(f"claude CLI gave no result (exit {completed.returncode}): "
                                f"{(completed.stderr or '').strip()[-200:]}")
    if result.get("is_error"):
        # Usage limits, session limits and login problems come back as an error result.
        raise VisionUnavailable(f"claude CLI: {str(result.get('result') or '').strip()[:200]}")
    return str(result.get("result") or "")


def ask_ollama(images: list[str], prompt: str, model: str) -> str:
    """One Ollama /api/chat call with the images (temperature 0, no thinking)."""
    base = str(config.app.get("ollama_base_url") or "http://localhost:11434").rstrip("/")
    base = base[:-3] if base.endswith("/v1") else base
    body = {"model": model, "stream": False, "think": False, "options": {"temperature": 0},
            "messages": [{"role": "system", "content": SYSTEM},
                         {"role": "user", "content": prompt, "images": images}]}
    try:
        response = requests.post(f"{base}/api/chat", json=body, timeout=CALL_TIMEOUT)
        payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise VisionUnavailable(f"ollama: {type(exc).__name__}: {exc}") from exc
    if response.status_code != 200 or payload.get("error"):
        raise VisionUnavailable(f"ollama {response.status_code}: {str(payload.get('error'))[:200]}")
    return str((payload.get("message") or {}).get("content") or "")


# Gemini: the quota is per project, so the spacing is shared by every task and thread.
_gemini_lock = threading.Lock()
_gemini_next = 0.0  # time.monotonic() of the earliest next request
_sleep = time.sleep  # tests replace it


def _gemini_key() -> str:
    """The Gemini API key: gemini_api_key (shared with the Gemini LLM and TTS), else GEMINI_API_KEY."""
    return str(config.app.get("gemini_api_key") or os.environ.get("GEMINI_API_KEY") or "").strip()


def redact(text: Any, key: str = "") -> str:
    """The text with the key, and anything shaped like a Google API key, masked."""
    text = str(text)
    if key:
        text = text.replace(key, "***")
    return re.sub(r"AIza[0-9A-Za-z_\-]{20,}", "***", text)


def _gemini_pace() -> None:
    """Wait until 60 / material_vision_gemini_rpm seconds have passed since the previous request."""
    global _gemini_next
    interval = 60.0 / _count("material_vision_gemini_rpm", DEFAULT_GEMINI_RPM)
    with _gemini_lock:
        now = time.monotonic()
        start = max(now, _gemini_next)
        _gemini_next = start + interval
    if start > now:
        _sleep(start - now)


def quota_details(error: Any) -> tuple[float | None, list[dict]]:
    """(the retry delay in seconds, the quota violations) of a Gemini API error."""
    body = getattr(error, "details", None)
    body = body if isinstance(body, dict) else {}
    delay, violations = None, []
    for detail in (body.get("error") or body).get("details") or []:
        if not isinstance(detail, dict):
            continue
        if str(detail.get("@type") or "").endswith("RetryInfo"):
            match = re.fullmatch(r"\s*([\d.]+)s\s*", str(detail.get("retryDelay") or ""))
            delay = float(match.group(1)) if match else delay
        violations += [item for item in detail.get("violations") or [] if isinstance(item, dict)]
    return delay, violations


def _thinking(model: str) -> dict:
    """Low thinking for Gemini 3 and newer (the screening is a short classification)."""
    from google.genai import types

    match = re.match(r"gemini-(\d+)", model)
    if match and int(match.group(1)) >= 3:
        return {"thinking_config": types.ThinkingConfig(thinking_level=types.ThinkingLevel.LOW)}
    return {}


def ask_gemini(images: list[str], prompt: str, model: str) -> str:
    """
    One Gemini generateContent call with the images (google-genai, no SDK retries): requests
    spaced by material_vision_gemini_rpm; 429 and 5xx wait (the server's retryDelay, else
    10, 20, 40 s) and try again up to GEMINI_RETRIES times; a daily quota or a quota of 0
    (the model is not in the project's tier) stops at once.
    """
    from google import genai
    from google.genai import errors, types

    key = _gemini_key()
    if not key:
        raise VisionUnavailable("gemini: no API key (gemini_api_key)")
    parts = [types.Part.from_bytes(data=base64.b64decode(data), mime_type="image/jpeg") for data in images]
    parts.append(types.Part.from_text(text=prompt))
    generation = types.GenerateContentConfig(system_instruction=SYSTEM, response_mime_type="application/json",
                                             response_json_schema=RESPONSE_SCHEMA, **_thinking(model))
    for attempt in range(GEMINI_RETRIES + 1):
        _gemini_pace()
        try:
            with genai.Client(api_key=key, http_options=types.HttpOptions(timeout=CALL_TIMEOUT * 1000)) as client:
                response = client.models.generate_content(
                    model=model, contents=[types.Content(role="user", parts=parts)], config=generation)
            return str(response.text or "")
        except errors.APIError as exc:
            code, status, message = exc.code, exc.status, exc.message
            delay, violations = quota_details(exc) if code == 429 else (None, [])
        except Exception as exc:  # network, timeout: the message may name the URL, never the header
            raise VisionUnavailable(redact(f"gemini: {type(exc).__name__}: {exc}", key)[:300]) from None
        final = [item for item in violations
                 if "PerDay" in str(item.get("quotaId") or "") or str(item.get("quotaValue")) == "0"]
        if final:
            quota = final[0]
            raise VisionUnavailable(f"gemini quota {quota.get('quotaId')} = {quota.get('quotaValue')} reached")
        if code not in GEMINI_RETRY_CODES:
            raise VisionUnavailable(redact(f"gemini {code} {status}: {message}", key)[:300])
        if attempt >= GEMINI_RETRIES:
            raise VisionUnavailable(f"gemini {code} {status} after {GEMINI_RETRIES} retries")
        wait = min(GEMINI_MAX_WAIT, delay if delay is not None else 10.0 * 2 ** attempt)
        quotas = ", ".join(f"{item.get('quotaId')}={item.get('quotaValue')}" for item in violations)
        logger.warning(f"vision screening: gemini {code} {status}{' (' + quotas + ')' if quotas else ''}, "
                       f"retrying in {wait:.0f}s ({attempt + 1}/{GEMINI_RETRIES})")
        _sleep(wait)
    raise VisionUnavailable("gemini: no answer")  # not reached


BACKENDS: dict[str, Callable[[list[str], str, str], str]] = {
    "claude": ask_claude, "ollama": ask_ollama, "gemini": ask_gemini}


# ---------------------------------------------------------------- inputs and cache


def _download(url: str, folder: str) -> str:
    target = os.path.join(folder, f"preview-{utils.md5(url)}.jpg")
    response = requests.get(url, timeout=30, proxies=config.proxy or None)
    response.raise_for_status()
    with open(target, "wb") as file:
        file.write(response.content)
    return target


def _signature(item) -> str | None:
    """What the model is shown, known without fetching it (the window of a NASA video, else the
    preview); None when there is nothing to show."""
    from app.services import nasa_video

    info = item.source_info if isinstance(item.source_info, dict) else {}
    if nasa_video.is_video(item):
        segment = info.get("segment") or {}
        if segment.get("start") is None:
            return None
        return f"window:{segment.get('start')}:{segment.get('duration')}"
    preview = info.get("preview")
    return "preview" if isinstance(preview, str) and preview.startswith(("http://", "https://")) else None


def _frames(item, folder: str) -> list[str]:
    """The image files to show: three frames of a NASA video's window, else the preview."""
    from app.services import nasa_video

    if nasa_video.is_video(item):
        return nasa_video.window_frames(item, folder)
    return [_download(item.source_info["preview"], folder)]


def cache_key(item, signature: str, backend: str, model: str, anchors: Sequence[str]) -> str:
    info = item.source_info if isinstance(item.source_info, dict) else {}
    parts = [str(PROMPT_VERSION), backend, model, str(getattr(item, "provider", "") or info.get("provider")),
             str(info.get("asset_id") or getattr(item, "url", "")), signature,
             ",".join(sorted(str(anchor).strip().lower() for anchor in anchors))]
    return utils.md5("|".join(parts))


def _cache_path(key: str) -> str:
    return os.path.join(utils.storage_dir("cache_vision", create=True), f"{key}.json")


def _load(key: str) -> dict | None:
    try:
        with open(_cache_path(key), encoding="utf-8") as file:
            data = json.load(file)
        return data if isinstance(data, dict) and data.get("prompt_version") == PROMPT_VERSION else None
    except (OSError, ValueError):
        return None


def _store(key: str, data: dict) -> None:
    path = _cache_path(key)
    try:
        with open(f"{path}.part", "w", encoding="utf-8") as file:
            json.dump(data, file, ensure_ascii=False)
        os.replace(f"{path}.part", path)
    except OSError:
        pass


# ---------------------------------------------------------------- per task


@dataclass
class Session:
    anchors: list
    subject: str = ""
    backend: str = "claude"
    model: str = ""
    max_per_term: int = DEFAULT_MAX_PER_TERM
    max_per_task: int = DEFAULT_MAX_PER_TASK
    calls: int = 0
    cached: int = 0  # decisions read from the disk cache (an earlier task)
    repeats: int = 0  # the same candidate asked again in this task (chain decision, use, download)
    accepted: int = 0
    rejected: int = 0
    unscreened: int = 0
    seconds: float = 0.0
    stopped: str = ""
    # material_vision_fallback_model: (backend, model) taken once when the first one fails.
    fallback: tuple | None = None
    switched_from: str = ""  # "<backend>:<model>" of the model the task started with, once switched
    # (term, source) -> the candidates screened for it, cached decisions included: the limit is
    # on candidates, so a task decides the same with a cold or a warm cache. Both this and
    # ``decisions`` are keyed by the candidate (``identity``), not by the model that answered.
    screened: dict = field(default_factory=dict)
    decisions: dict = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)
    call_lock: threading.Lock = field(default_factory=threading.Lock)

    def stop(self, reason: str) -> None:
        with self.lock:
            if self.stopped:
                return
            self.stopped = reason
        logger.warning(f"vision screening stopped: {reason}; the remaining candidates are used unscreened")

    def _switch(self, reason: str) -> bool:
        """Take the fallback model for the rest of the task (once); False when there is none."""
        with self.lock:
            if not self.fallback or self.switched_from or self.fallback == (self.backend, self.model):
                return False
            self.switched_from = f"{self.backend}:{self.model}"
            self.backend, self.model = self.fallback
        logger.warning(f"vision screening: {self.switched_from} unavailable ({reason}); "
                       f"switching to {self.backend}:{self.model} for this task")
        return True

    def _ask(self, images: list[str], prompt: str) -> tuple[dict | None, str, str]:
        """The parsed answer (asked twice when unreadable) and the model that gave it; a model
        that cannot answer is replaced by the fallback once, then VisionUnavailable."""
        while True:
            backend, model = self.backend, self.model
            try:
                result = None
                for _attempt in range(2):
                    result = parse(BACKENDS[backend](images, prompt, model))
                    if result is not None:
                        break
                return result, backend, model
            except VisionUnavailable as exc:
                reason = str(exc)
            except Exception as exc:  # the task never fails because of the screening
                reason = redact(f"{type(exc).__name__}: {exc}", _gemini_key())
            if not self._switch(reason):
                raise VisionUnavailable(reason)

    def screen(self, item) -> bool:
        """True when the candidate may be used; the decision goes to source_info["vision"] and materials.json."""
        info = item.source_info if isinstance(item.source_info, dict) else {}
        term = str(info.get("search_term") or "")
        provider = str(getattr(item, "provider", "") or info.get("provider") or "")
        signature = _signature(item)
        if signature is None:
            return self._unscreened(item, "no_image")
        # The candidate within the task, whichever model screens it (a switch to the fallback
        # must not turn a used candidate into a new one); the disk cache is per model.
        identity = cache_key(item, signature, "", "", self.anchors)
        with self.lock:
            known = self.decisions.get(identity)
            if known is not None:
                self.repeats += 1
            else:
                seen = self.screened.setdefault((term, provider), set())
                limited = identity not in seen and len(seen) >= self.max_per_term
                if not limited:
                    seen.add(identity)
            backend, model = self.backend, self.model
        if known is not None:
            return self._apply(item, known, cached=True, count=False)
        if limited:
            # This source has had its share of candidates for the term: the chain moves on.
            task_materials.rejected(item, "vision", f"limit: {self.max_per_term} screened for {term!r}")
            return False
        known = _load(cache_key(item, signature, backend, model, self.anchors))
        if known is not None:
            with self.lock:
                self.decisions[identity] = known
                self.cached += 1
            return self._apply(item, known, cached=True)
        with self.lock:
            if self.stopped:
                stopped = True
            elif self.calls >= self.max_per_task:
                stopped = None
            else:
                stopped = False
                self.calls += 1
        if stopped is None:
            self.stop(f"material_vision_max_per_task={self.max_per_task} reached")
            return self._unscreened(item, "stopped")
        if stopped:
            return self._unscreened(item, "stopped")
        with tempfile.TemporaryDirectory(prefix="vision-") as folder:
            try:
                frames = _frames(item, folder)
            except Exception as exc:
                logger.warning(f"vision screening: no image for {info.get('asset_id')}: {type(exc).__name__}: {exc}")
                frames = []
            if not frames:
                return self._unscreened(item, "no_image")
            prompt = build_prompt(len(frames), self.subject or term, term, self.anchors, context_text(item))
            images = [_encode(frame) for frame in frames]
        started = time.perf_counter()
        try:
            with self.call_lock:  # one model call at a time per task
                result, backend, model = self._ask(images, prompt)
        except VisionUnavailable as exc:
            self.stop(str(exc))
            return self._unscreened(item, "stopped")
        finally:
            with self.lock:
                self.seconds += time.perf_counter() - started
        if result is None:
            logger.warning(f"vision screening: unreadable answer for {info.get('asset_id')}; used unscreened")
            return self._unscreened(item, "unreadable")
        use, reasons, label = decide(result, self.anchors)
        decision = {"prompt_version": PROMPT_VERSION, "model": f"{backend}:{model}", "result": result,
                    "use": use, "reasons": reasons, "label": label, "frames": len(frames),
                    "seconds": round(time.perf_counter() - started, 2)}
        _store(cache_key(item, signature, backend, model, self.anchors), decision)
        with self.lock:
            self.decisions[identity] = decision
        return self._apply(item, decision, cached=False)

    def _apply(self, item, decision: dict, *, cached: bool, count: bool = True) -> bool:
        """Record the decision on the item; ``count`` False for a candidate asked again in this task."""
        info = item.source_info if isinstance(item.source_info, dict) else {}
        record = {**decision.get("result", {}), "decision": "use" if decision["use"] else "reject",
                  "reasons": list(decision.get("reasons") or []), "model": decision.get("model"),
                  "prompt_version": decision.get("prompt_version"), "cached": cached}
        if decision.get("label"):
            record["label"] = decision["label"]
        if isinstance(item.source_info, dict):
            item.source_info["vision"] = record
        if not count:
            return bool(decision["use"])
        with self.lock:
            if decision["use"]:
                self.accepted += 1
            else:
                self.rejected += 1
        if not decision["use"]:
            logger.info(f"vision screening: skipped {info.get('asset_id')} for {info.get('search_term')!r} "
                        f"({', '.join(record['reasons'])}; {record.get('subject') or ''})")
            task_materials.rejected(item, "vision", ", ".join(record["reasons"]))
        return bool(decision["use"])

    def _unscreened(self, item, reason: str) -> bool:
        if isinstance(item.source_info, dict):
            item.source_info["vision"] = {"decision": "unscreened", "reasons": [reason]}
        with self.lock:
            self.unscreened += 1
        return True

    def summary(self) -> str:
        model = f"{self.backend}:{self.model or 'default'}"
        if self.switched_from:
            model = f"{self.switched_from}→{model}"  # no space: the batch report reads one token
        return (f"vision screening: model={model}, calls={self.calls}, "
                f"cached={self.cached}, repeats={self.repeats}, accepted={self.accepted}, rejected={self.rejected}, "
                f"unscreened={self.unscreened}, seconds={self.seconds:.1f}, "
                f"status={'stopped: ' + self.stopped if self.stopped else 'complete'}")


_current: contextvars.ContextVar[Session | None] = contextvars.ContextVar("vision_session", default=None)


def start(anchors: Sequence[str], subject: str = "") -> Session | None:
    """The screening session of a task (None when material_vision_screening is off)."""
    if not enabled():
        return None
    backend, model = model_setting()
    fallback = fallback_setting((backend, model))
    session = Session(anchors=[str(a) for a in anchors or () if str(a).strip()], subject=str(subject or ""),
                      backend=backend, model=model, fallback=fallback,
                      max_per_term=_count("material_vision_max_per_term", DEFAULT_MAX_PER_TERM),
                      max_per_task=_count("material_vision_max_per_task", DEFAULT_MAX_PER_TASK))
    _current.set(session)
    logger.info(f"vision screening on: {backend}:{model or 'default'}"
                f"{', fallback ' + ':'.join(fallback) if fallback else ''}, at most {session.max_per_term} per term "
                f"and source, {session.max_per_task} per task")
    return session


def finish(session: Session | None) -> None:
    """Log the task's screening summary (the batch report reads this line)."""
    if session is not None:
        logger.info(session.summary())
