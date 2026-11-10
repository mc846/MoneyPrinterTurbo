"""Ordered fallback chain of stock sources, decided per search term.

``video_source_fallback`` names the stock sources tried after the primary
``video_source`` when a term has too few suitable candidates: one value (as before,
``"pixabay"``) or an ordered list (``["pixabay", "coverr"]``). The rules live here;
``material.download_videos`` and the revision search build the links and call
``chain_search``.

Per term:

1. The primary, then the fallbacks in order. A source is suitable when it has at
   least ``min_candidates`` candidates whose tag relevance is at least
   ``min_relevance`` (``material._qualified_candidate_count``; with tag relevance off
   every candidate counts). The first suitable source is used and later sources get
   no request.
2. When none is suitable, the source with the most suitable candidates is used; a tie
   goes to the earlier one. A source without candidates is never chosen.
3. Untagged fallbacks (Pexels and Coverr carry no tags; ``tagged`` is False) cannot
   be measured, so they never count as suitable and are not asked in the first pass.
   They are the last resort: asked in chain order only when no tagged source has a
   single suitable candidate, and the first with candidates is used. The primary
   keeps today's rule (an untagged primary counts all its candidates).
4. A fallback without an API key is dropped for the task with one warning
   (``available``). A source answering HTTP 429 is skipped for the rest of the task
   with one warning (``ChainState``).
5. A term takes all its candidates from one source; pools never mix.

Each decision (sources tried with their candidate and suitable counts, the chosen
source and why) goes to ``on_decision`` (task_materials records it) and to the log.
"""

import threading
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Sequence

from loguru import logger

STOCK_SOURCES = ("pexels", "pixabay", "coverr", "nasa")
API_KEYS = {"pexels": "pexels_api_keys", "pixabay": "pixabay_api_keys", "coverr": "coverr_api_keys"}
_OFF = {"", "none", "off"}

MET = "met"
MOST_SUITABLE = "most_suitable"
UNTAGGED_LAST_RESORT = "untagged_last_resort"
NO_SUITABLE = "no_suitable"
NO_CANDIDATES = "no_candidates"


class SourceChainError(ValueError):
    """An invalid video_source_fallback: unknown, repeated, or the primary source again."""


def parse(value: Any) -> list[str]:
    """The chain as a list: "" / None / "none" → [], "pixabay" → ["pixabay"],
    "pixabay, coverr" or ["pixabay", "coverr"] → both, in order (lower case)."""
    if value is None:
        return []
    if isinstance(value, str):
        parts = value.split(",")
    elif isinstance(value, (list, tuple)):
        parts = [part for item in value for part in str(item if item is not None else "").split(",")]
    else:
        raise SourceChainError(f"video_source_fallback must be a source name or a list, got {value!r}")
    chain = [part.strip().lower() for part in parts]
    chain = [part for part in chain if part]
    if any(part in _OFF for part in chain):
        if len(chain) > 1:
            raise SourceChainError(f"video_source_fallback: 'none' cannot be combined with sources: {value!r}")
        return []
    return chain


def parse_lenient(value: Any) -> list[str]:
    """``parse`` that returns [] for an unreadable setting (for display only)."""
    try:
        return parse(value)
    except SourceChainError:
        return []


def is_set(value: Any) -> bool:
    """Whether the setting names any source (valid or not): not None, "", "none", "off" or []."""
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip().lower() not in _OFF
    if isinstance(value, (list, tuple)):
        return any(str(item or "").strip().lower() not in _OFF for item in value)
    return True


def validate(primary: str, value: Any) -> list[str]:
    """The parsed chain, or SourceChainError with a readable reason."""
    chain = parse(value)
    if not chain:
        return []
    unknown = [source for source in chain if source not in STOCK_SOURCES]
    if unknown:
        raise SourceChainError(
            f"video_source_fallback: unknown source {unknown[0]!r}; choose from {', '.join(STOCK_SOURCES)}"
        )
    repeated = sorted({source for source in chain if chain.count(source) > 1})
    if repeated:
        raise SourceChainError(f"video_source_fallback lists {repeated[0]} more than once: {chain}")
    primary = str(primary or "").strip().lower()
    if primary not in STOCK_SOURCES:
        raise SourceChainError(
            f"video_source_fallback needs a stock video_source ({', '.join(STOCK_SOURCES)}), not {primary!r}"
        )
    if primary in chain:
        raise SourceChainError(f"video_source_fallback must differ from video_source ({primary}): {chain}")
    return chain


def compact(chain: Sequence[str]) -> str | list[str]:
    """The setting's stored form: "" for none, the name for one source, a list for more."""
    chain = list(chain)
    if not chain:
        return ""
    return chain[0] if len(chain) == 1 else chain


def has_api_key(config_app: dict, source: str) -> bool:
    key = API_KEYS.get(source)
    if not key:
        return True  # NASA needs no key
    keys = config_app.get(key)
    if isinstance(keys, str):
        return bool(keys.strip())
    if isinstance(keys, (list, tuple)):
        return any(str(item or "").strip() for item in keys)
    return False


def available(primary: str, value: Any, config_app: dict,
              has_key: Callable[[str], bool] | None = None) -> list[str]:
    """The task's chain without sources that have no API key (one warning each).

    An invalid chain is ignored with a warning, as the single fallback was: the task
    still runs on its primary source (the CLI, API and presets reject it earlier).
    ``has_key(source)`` overrides the key check (material passes its own)."""
    try:
        chain = validate(primary, value)
    except SourceChainError as exc:
        logger.warning(f"{exc}; the fallback chain is ignored")
        return []
    usable = []
    for source in chain:
        if (has_key(source) if has_key is not None else has_api_key(config_app, source)):
            usable.append(source)
        else:
            logger.warning(
                f"video_source_fallback: {source} is skipped for this task: "
                f"{API_KEYS[source]} is not set in config.toml"
            )
    return usable


# ------------------------------------------------------------------ rate limits

_rate_limit_seen: ContextVar[set | None] = ContextVar("source_chain_rate_limit", default=None)


def note_rate_limited(provider: str) -> None:
    """Called by the stock searches on HTTP 429; the chain watching the call sees it."""
    seen = _rate_limit_seen.get()
    if seen is not None:
        seen.add(str(provider))


@contextmanager
def watch_rate_limit():
    seen: set = set()
    token = _rate_limit_seen.set(seen)
    try:
        yield seen
    finally:
        _rate_limit_seen.reset(token)


@dataclass
class ChainState:
    """Per task: sources that answered 429 are skipped until the task ends."""

    rate_limited: set = field(default_factory=set)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def mark_rate_limited(self, source: str) -> None:
        with self.lock:
            if source in self.rate_limited:
                return
            self.rate_limited.add(source)
        logger.warning(f"{source} answered HTTP 429 (rate limit): it is skipped for the rest of this task")

    def is_rate_limited(self, source: str) -> bool:
        with self.lock:
            return source in self.rate_limited


# ------------------------------------------------------------------ the decision


@dataclass
class Link:
    source: str
    search: Callable[..., list]
    tag_relevance: Any  # the effective MaterialTagRelevance of this source (off for untagged ones)
    tagged: bool  # the source's candidates carry tags (material.MATERIAL_TAG_SOURCES)
    primary: bool = False

    def measured(self, relevance_on: bool) -> bool:
        """Its suitable count means something: the primary, a tagged source, or the task's
        tag relevance is off (then every source simply counts its candidates)."""
        return self.primary or self.tagged or not relevance_on


def decide(entries: list[dict]) -> tuple[str | None, str]:
    """Pick from the sources tried (in chain order) when none met the minimum.

    ``entries`` have source, candidates, suitable (None when not measured)."""
    measured = [e for e in entries if e.get("suitable") is not None and e.get("candidates")]
    best = None
    for entry in measured:
        if entry["suitable"] > 0 and (best is None or entry["suitable"] > best["suitable"]):
            best = entry
    if best is not None:
        return best["source"], MOST_SUITABLE
    untagged = next((e for e in entries if e.get("suitable") is None and e.get("candidates")), None)
    if untagged is not None:
        return untagged["source"], UNTAGGED_LAST_RESORT
    if measured:
        return measured[0]["source"], NO_SUITABLE
    return None, NO_CANDIDATES


def chain_search(
    links: Sequence[Link],
    *,
    min_relevance: float,
    min_candidates: int,
    count_suitable: Callable[[str, list, Any, float, bool], int | None],
    relevance_on: bool,
    state: ChainState | None = None,
    on_decision: Callable[[str, dict], None] | None = None,
) -> Callable[..., list]:
    """A search function trying ``links`` (the primary first) per term by the rules above."""
    state = state or ChainState()
    links = list(links)

    def run(link: Link, search_term: str, kwargs: dict) -> tuple[list, dict]:
        entry: dict = {"source": link.source, "tagged": link.tagged}
        if state.is_rate_limited(link.source):
            entry.update(candidates=0, suitable=None, skipped="rate_limited")
            return [], entry
        with watch_rate_limit() as seen:
            try:
                items = list(link.search(search_term=search_term, **kwargs) or [])
            except Exception as exc:
                logger.error(
                    f"failed to search {link.source} materials: search_term={search_term!r}, "
                    f"error={type(exc).__name__}, detail={exc}"
                )
                items = []
                entry["error"] = type(exc).__name__
        if link.source in seen:
            state.mark_rate_limited(link.source)
            entry["rate_limited"] = True
        entry["candidates"] = len(items)
        # None: not measurable (an untagged fallback, or a tagged one whose candidates
        # came without tag data); such a source is only a last resort.
        entry["suitable"] = (
            count_suitable(search_term, items, link.tag_relevance, min_relevance, link.primary)
            if link.measured(relevance_on) else None
        )
        return items, entry

    def search_videos(search_term: str, minimum_duration: int, video_aspect) -> list:
        kwargs = {"minimum_duration": minimum_duration, "video_aspect": video_aspect}
        tried, results = [], {}
        chosen, reason = None, NO_CANDIDATES
        later = []
        for link in links:
            if not link.measured(relevance_on):
                later.append(link)  # untagged fallback: only as the last resort
                continue
            items, entry = run(link, search_term, kwargs)
            tried.append(entry)
            results[link.source] = items
            if entry["suitable"] is not None and entry["suitable"] >= min_candidates and items:
                chosen, reason = link.source, MET
                break
        if chosen is None:
            chosen, reason = decide(tried)
            if reason in (NO_SUITABLE, NO_CANDIDATES) and later:
                # No tagged source has a suitable candidate: ask the untagged ones in order.
                for link in later:
                    items, entry = run(link, search_term, kwargs)
                    tried.append(entry)
                    results[link.source] = items
                    if items:
                        chosen, reason = link.source, UNTAGGED_LAST_RESORT
                        break
        decision = {
            "tried": tried,
            "chosen": chosen,
            "reason": reason,
            "chain": [link.source for link in links],
            "min_relevance": min_relevance,
            "min_candidates": min_candidates,
        }
        if len(links) > 1:
            # One parseable line per term (batch_package reads it for the quality report):
            # tried=<source>:<candidates>/<suitable, - when not measured>[:429|:skipped],...
            logger.info(f"material source chain: term={search_term!r}, tried={describe_tried(tried)}, "
                        f"using={chosen}, reason={reason}")
        if on_decision is not None:
            try:
                on_decision(search_term, decision)
            except Exception as exc:  # recording must never break the search
                logger.warning(f"source chain decision was not recorded: {type(exc).__name__}: {exc}")
        return list(results.get(chosen) or []) if chosen else []

    return search_videos


def describe_tried(tried: list[dict]) -> str:
    """The sources tried as ``nasa:0/0,pixabay:12/3,pexels:5/-`` (candidates/suitable)."""
    parts = []
    for entry in tried:
        suitable = "-" if entry.get("suitable") is None else entry["suitable"]
        text = f"{entry['source']}:{entry.get('candidates', 0)}/{suitable}"
        if entry.get("skipped"):
            text += ":skipped"
        elif entry.get("rate_limited"):
            text += ":429"
        parts.append(text)
    return ",".join(parts)


def merged_options(options: Iterable[dict], anchors: Sequence[str]) -> dict:
    """Ordering options for a pool mixing terms from several sources: what any of them supports."""
    options = list(options)
    merged = {
        "crop_to_aspect": any(o["crop_to_aspect"] for o in options),
        "tag_relevance": next((o["tag_relevance"] for o in options if not _is_off(o["tag_relevance"])),
                              options[0]["tag_relevance"]),
        "exclude_ai_generated": any(o["exclude_ai_generated"] for o in options),
    }
    if any("anchors" in o for o in options):
        merged["anchors"] = tuple(anchors)
    return merged


def _is_off(value) -> bool:
    return str(getattr(value, "value", value) or "off") == "off"
