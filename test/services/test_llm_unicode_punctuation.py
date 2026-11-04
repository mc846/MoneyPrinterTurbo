"""LLM 回复中字幕字体缺字的标点：不换行连字符、数字连字符和不换行空格统一成普通字符；
字母之间的弯撇号统一成 ASCII '。"""

import json
import types
from unittest.mock import patch

import pytest

from app.config import config
from app.services import llm


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("uzay‑zaman", "uzay-zaman"),
        ("100‒200 milyar", "100-200 milyar"),
        ("X‐ışınları", "X-ışınları"),
        ("yaklaşık 100", "yaklaşık 100"),
        ("38 mikrosaniye", "38 mikrosaniye"),
    ],
)
def test_unsupported_punctuation_becomes_plain(raw, expected):
    assert llm._normalize_text_response(raw, "ollama") == expected


def test_turkish_letters_quotes_dashes_and_paragraphs_are_kept():
    text = (
        "Çığ, ğüşıöç İĞÜŞÖÇ – “tırnak” ‘tek’ «guillemet» — uzun tire ― yatay çizgi.\n\n"
        "İkinci paragraf: Samanyolu'nun merkezi."
    )

    assert llm._normalize_text_response(text, "ollama") == text


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Milky Way’in yakınında", "Milky Way'in yakınında"),
        ("Einstein’ın kuramı", "Einstein'ın kuramı"),
        ("Güneş’in ve Dünya’nın", "Güneş'in ve Dünya'nın"),
        ("İSTANBUL’DA, Çağ’ı, Öykü’ye, ışık’ı", "İSTANBUL'DA, Çağ'ı, Öykü'ye, ışık'ı"),
        ("Hawaiʼi ve Einsteinʼın", "Hawai'i ve Einstein'ın"),
        ("SN 2012Z’nin ışığı", "SN 2012Z'nin ışığı"),
    ],
)
def test_apostrophe_between_letters_becomes_ascii(raw, expected):
    assert llm._normalize_text_response(raw, "ollama") == expected


@pytest.mark.parametrize(
    "text",
    [
        "Ona ‘zombi yıldız’ deniyor.",
        "Bilim insanları ‘yarım kalmış’, ‘kısmi’ patlama diyor.",
        "‘Zombi’ yalnız bir takma ad’",
        "Sonuç: “tam” değil.",
        "1990’larda ve 2025’te",
        "rock ’n’ roll",
    ],
)
def test_quotes_not_between_two_letters_are_kept(text):
    assert llm._normalize_text_response(text, "ollama") == text


def _ollama_reply(content):
    """通过 Ollama（OpenAI 兼容）分支返回给定正文，走真实的 _generate_response 清理。"""

    class FakeCompletions:
        def create(self, **kwargs):
            message = types.SimpleNamespace(content=content)
            return types.SimpleNamespace(choices=[types.SimpleNamespace(message=message)])

    client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=FakeCompletions()))
    return (
        patch.dict(config.app, {"llm_provider": "ollama", "ollama_api_key": "",
                                "ollama_base_url": "http://localhost:11434/v1",
                                "ollama_model_name": "gpt-oss:120b-cloud"}),
        patch.object(llm, "OpenAI", return_value=client),
        patch.object(llm, "ChatCompletion", types.SimpleNamespace),
    )


def test_term_json_with_these_characters_still_parses():
    reply = json.dumps(["space‑time grid", "neutron star", "1054‒supernova"], ensure_ascii=False)
    settings, openai, completion = _ollama_reply(f"```json\n{reply}\n```")
    with settings, openai, completion:
        terms = llm.generate_terms("Zombi yıldızlar", "Metin.", amount=3)

    assert terms == ["space-time grid", "neutron star", "1054-supernova"]


def test_term_json_with_letter_apostrophes_still_parses():
    reply = json.dumps(["Einstein’s ring", "Kepler’s supernova", "‘zombie’ star"], ensure_ascii=False)
    settings, openai, completion = _ollama_reply(f"Terimler:\n{reply}")
    with settings, openai, completion:
        terms = llm.generate_terms("Zombi yıldızlar", "Metin.", amount=3)

    assert terms == ["Einstein's ring", "Kepler's supernova", "‘zombie’ star"]


def test_script_from_ollama_is_normalized():
    settings, openai, completion = _ollama_reply("Uzay‑zaman bükülür; Einstein’ın saatleri yavaşlar.")
    with settings, openai, completion:
        script = llm.generate_script("Görelilik", language="tr-TR")

    assert script == "Uzay-zaman bükülür; Einstein'ın saatleri yavaşlar."
