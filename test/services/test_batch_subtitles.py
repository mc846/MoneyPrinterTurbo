"""Toplu üretim .srt altyazıları：校验器、拆分规则（边界值、土耳其语字母、超长单词）、标点恢复、时间规范化和翻译（假 LLM）。"""

import json

import pytest

from app.services import batch_subtitles as bst
from app.services.batch_subtitles import Cue


def srt(*blocks, newline="\r\n"):
    return (newline * 2).join(newline.join(block) for block in blocks) + newline


GOOD = srt(["1", "00:00:00,100 --> 00:00:02,000", "Merhaba dünya"], ["2", "00:00:02,000 --> 00:00:03,500", "İkinci"])


# ---------------------------------------------------------------- doğrulayıcı

def test_valid_file_has_no_problems():
    assert bst.validate_srt(GOOD, 3500) == []
    assert bst.validate_srt(GOOD.replace("\r\n", "\n"), 3500) == []  # yalnız LF de tutarlı


@pytest.mark.parametrize("text, problem", [
    ("﻿" + GOOD, "BOM var"),
    (GOOD.replace("\r\n", "\n", 1), "satır sonları karışık (CRLF ve LF)"),
    (GOOD.replace("2\r\n00:00:02", "3\r\n00:00:02"), "2. altyazı: sıra numarası '3'"),
    (GOOD.replace("00:00:00,100", "0:00:00.100"), "1. altyazı: zaman biçimi"),
    (GOOD.replace("--> 00:00:02,000\r\nMerhaba", "--> 00:00:00,100\r\nMerhaba"), "1. altyazı: bitiş başlangıçtan önce"),
    (GOOD.replace("00:00:02,000 --> 00:00:03,500", "00:00:01,500 --> 00:00:03,500"), "2. altyazı: öncekiyle çakışıyor"),
    (srt(["1", "00:00:00,100 --> 00:00:02,000", "a", "b", "c"]), "1. altyazı: 3 satır"),
    (srt(["1", "00:00:00,100 --> 00:00:02,000", "x" * 43]), "1. altyazı: 43 karakterlik satır"),
    (srt(["1", "00:00:00,100 --> 00:00:02,000", " "]), "1. altyazı: boş"),
])
def test_validator_reports_each_rule(text, problem):
    assert problem in bst.validate_srt(text, 3500)


def test_validator_checks_the_video_duration():
    assert bst.validate_srt(GOOD, 3400) == ["son altyazı video süresini aşıyor"]


def test_parse_and_format_round_trip_with_bom_and_lf():
    cues = bst.parse_srt("﻿1\n00:01:02,003 --> 00:01:04,500\nİlk satır\nikinci satır\n\n2\n00:01:05,000 --> "
                         "00:01:06,000\nSon\n")
    assert [(cue.start, cue.end, cue.text) for cue in cues] == [(62003, 64500, "İlk satır ikinci satır"),
                                                                (65000, 66000, "Son")]
    text = bst.format_srt(cues)
    assert text.startswith("1\r\n00:01:02,003 --> 00:01:04,500\r\nİlk satır ikinci satır\r\n\r\n2\r\n")
    assert bst.validate_srt(text) == []


# ---------------------------------------------------------------- satır kuralları

@pytest.mark.parametrize("length, lines", [(41, 1), (42, 1), (43, 2), (84, 2)])
def test_layout_boundaries(length, lines):
    text = " ".join(["çğışöü"] * 20)[:length].rstrip()
    while len(text) < length:  # tam uzunluk; Türkçe harfler tek karakter sayılır
        text += "ş"
    result = bst.layout(text)
    assert len(result) == lines and all(len(line) <= bst.MAX_LINE for line in result)


def test_two_lines_are_balanced_and_prefer_a_break_after_punctuation():
    assert bst.layout("Güneş'ten daha ağır bir kütlenin, yalnızca yirmi kilometre çapında") == [
        "Güneş'ten daha ağır bir kütlenin,", "yalnızca yirmi kilometre çapında"]


def test_a_word_longer_than_a_line_is_hyphenated():
    word = "Muvaffakiyetsizleştiricileştiriveremeyebileceklerimizdenmişsinizcesine"
    lines = bst.layout(f"Örnek: {word}")
    assert all(len(line) <= bst.MAX_LINE for line in lines)
    assert "".join(line.replace("-", "") for line in lines).replace(" ", "") == f"Örnek:{word}"


def test_long_cue_is_split_at_word_boundaries_with_time_shared_by_characters():
    text = ("Satürn eksenini eğik tutarak Güneş çevresinde yaklaşık 29 yılda dolanır, bu yüzden halka düzlemi "
            "zaman zaman tam olarak bize dönük hale gelir.")
    pieces = bst.split_cue(Cue(10_000, 20_000, text))
    assert len(pieces) == 2 and " ".join(piece.text for piece in pieces) == text
    assert pieces[0].text.endswith("dolanır,")  # noktalamadan bölündü
    assert pieces[0].start == 10_000 and pieces[-1].end == 20_000 and pieces[0].end == pieces[1].start
    share = len(pieces[0].text) / (len(pieces[0].text) + len(pieces[1].text))
    assert abs((pieces[0].end - 10_000) / 10_000 - share) < 0.001
    assert all(len(bst.layout(piece.text)) <= 2 for piece in pieces)


def test_balanced_split_avoids_a_tiny_last_piece():
    # Açgözlü bölme ilk parçayı 84 karaktere kadar doldurur ve sona tek bir kelime bırakırdı.
    text = ("Halkalar yüz binlerce kilometre genişken kalınlıkları çoğu yerde bir kilometreden azdır kenarından "
            "bakılınca")
    pieces = bst.split_cue(Cue(0, 9_000, text))
    lengths = [len(piece.text) for piece in pieces]
    assert len(pieces) == 2 and max(lengths) - min(lengths) <= 20, lengths


def test_short_cue_is_not_split():
    cue = Cue(0, 1000, "Kısa bir altyazı")
    assert bst.split_cue(cue) == [cue]


# ---------------------------------------------------------------- Türkçe dosya

def test_punctuation_is_restored_from_the_narration():
    cues = [Cue(0, 1000, "Bir çay kaşığı madde nasıl ağır olabilir"), Cue(1000, 2000, "Nötron yıldızlarında"),
            Cue(2000, 3000, "1967'de Jocelyn Bell")]
    narration = "Bir çay kaşığı madde nasıl ağır olabilir? Nötron yıldızlarında — 1967'de Jocelyn Bell, keşfetti."
    restored, aligned = bst.restore_punctuation(cues, narration)
    assert not aligned  # "keşfetti." altyazıda yok
    restored, aligned = bst.restore_punctuation(cues + [Cue(3000, 4000, "keşfetti")], narration)
    assert aligned
    assert [cue.text for cue in restored] == ["Bir çay kaşığı madde nasıl ağır olabilir?", "Nötron yıldızlarında",
                                              "— 1967'de Jocelyn Bell,", "keşfetti."]


def test_unaligned_text_is_kept_as_it_is():
    cues = [Cue(0, 1000, "tamamen başka kelimeler")]
    assert bst.restore_punctuation(cues, "Bambaşka bir metin.") == (cues, False)


def test_normalize_removes_empty_and_overlapping_and_clamps_to_the_video():
    cues = [Cue(0, 1500, " Bir "), Cue(1200, 2000, ""), Cue(1300, 2500, "iki"), Cue(2400, 2400, "üç"),
            Cue(2500, 9000, "son")]
    assert [(c.start, c.end, c.text) for c in bst.normalize_cues(cues, duration_ms=4000)] == [
        (0, 1300, "Bir"), (1300, 2400, "iki üç"), (2500, 4000, "son")]


def test_parser_tolerates_crlf_written_in_text_mode():
    # Windows'ta metin kipinde yazılan CRLF "\r\r\n" olur; altyazı metni zamanlamadan ayrılmamalı.
    cues = bst.parse_srt("1\r\r\n00:00:00,000 --> 00:00:01,000\r\r\nKara delik\r\r\n")
    assert [(cue.start, cue.end, cue.text) for cue in cues] == [(0, 1000, "Kara delik")]


def test_source_file_follows_every_rule(tmp_path):
    subtitle = tmp_path / "subtitle.srt"
    subtitle.write_text(srt(
        ["1", "00:00:00,100 --> 00:00:04,412", "1612'de Galileo teleskobunu Satürn'e çevirdiğinde şaşkına döndü"],
        ["2", "00:00:04,300 --> 00:00:30,000", "Halkalar yüz binlerce kilometre genişken kalınlıkları çoğu yerde "
                                               "bir kilometreden azdır kenarından bakıldığında incecik görünür"],
        newline="\n"), encoding="utf-8")
    narration = ("1612'de Galileo teleskobunu Satürn'e çevirdiğinde şaşkına döndü: Halkalar yüz binlerce kilometre "
                 "genişken kalınlıkları çoğu yerde bir kilometreden azdır; kenarından bakıldığında incecik görünür.")
    cues, info = bst.build_source_srt(subtitle, narration, duration_ms=20_000)
    text = bst.format_srt(cues)
    assert bst.validate_srt(text, 20_000) == []
    assert info == {"kaynak_altyazi": 2, "altyazi": 3, "bolunen": 1, "noktalama_geri_geldi": True}
    assert " ".join(cue.text for cue in cues) == narration
    assert cues[0].end == 4300 and cues[-1].end == 20_000


# ---------------------------------------------------------------- çeviri

def llm(*responses):
    calls = []

    def ask(prompt):
        calls.append(prompt)
        response = responses[min(len(calls), len(responses)) - 1]
        return response(prompt) if callable(response) else response

    ask.calls = calls
    return ask


def echo_translation(prompt):
    """İstemdeki satırları "EN:" önekiyle aynı uzunlukta döndürür."""
    lines = json.loads(prompt.split("## Lines to translate")[1].split("\n", 1)[1])
    return json.dumps([f"EN: {line}" for line in lines])


def test_translation_in_one_call_keeps_the_order():
    ask = llm(echo_translation)
    result, info = bst.translate_texts(["bir", "iki", "üç"], "en", "bağlam", ask)
    assert result == ["EN: bir", "EN: iki", "EN: üç"] and info == {"yontem": "tek çağrı", "cagri": 1}


def test_translation_retries_once_when_the_length_differs():
    ask = llm(json.dumps(["yalnız bir"]), echo_translation)
    result, info = bst.translate_texts(["bir", "iki"], "en", "", ask)
    assert result == ["EN: bir", "EN: iki"] and info == {"yontem": "yeniden deneme", "cagri": 2}


def test_translation_falls_back_to_chunks_then_single_lines():
    texts = [f"satır {number}" for number in range(8)]

    def answer(prompt):
        lines = json.loads(prompt.split("## Lines to translate")[1].split("\n", 1)[1])
        if len(lines) == 8:
            return json.dumps(lines[:5])  # bütün liste her zaman eksik döner
        if len(lines) == 6:
            return "Çeviri: " + json.dumps(lines)  # ilk parça bozuk (metin içinde dizi yine okunur)
        return f"```json\n{json.dumps([f'EN {line}' for line in lines])}\n```"

    ask = llm(answer)
    result, info = bst.translate_texts(texts, "en", "", ask)
    assert info == {"yontem": "parça parça", "cagri": 4}  # 2 tam + 6'lık parça + 2'lik parça
    assert result == texts[:6] + ["EN satır 6", "EN satır 7"]


def test_translation_of_a_broken_chunk_goes_line_by_line():
    def answer(prompt):
        lines = json.loads(prompt.split("## Lines to translate")[1].split("\n", 1)[1])
        return json.dumps([f"EN {line}" for line in lines]) if len(lines) == 1 else "bilmiyorum"

    result, info = bst.translate_texts(["a", "b"], "en", "", llm(answer))
    assert result == ["EN a", "EN b"] and info["cagri"] == 5  # 2 tam + 1 parça + 2 tek satır


def test_translation_prompt_states_the_rules():
    ask = llm(echo_translation)
    bst.translate_texts(["Kara delik"], "en", "Kara delik.", ask)
    prompt = ask.calls[0]
    assert "into English (en)" in prompt and "established English terms" in prompt
    assert "Keep proper names" in prompt and 'no "according to NASA"' in prompt
    assert "Respond ONLY with a JSON array" in prompt and "exactly 1 strings" in prompt


@pytest.mark.parametrize("source, target, code, warning", [
    ("Kara delikler ışığı yutar", "", "en", "1. altyazı: çeviri boş"),
    ("Kara delikler ışığı yutar", "Kara delikler ışığı yutar", "en", "1. altyazı: çevrilmemiş görünüyor"),
    ("Güneş bir gün sönecek mi?", "Will the Sun ever burn out? Ğ", "en", "1. altyazı: çevrilmemiş görünüyor"),
    ("Kara deliklerin içinde zaman durur mu?", "No.", "en", "1. altyazı: uzunluk oranı 0.08"),
])
def test_translation_warnings(source, target, code, warning):
    assert any(item.startswith(warning) for item in bst.translation_warnings([source], [target], code))


def test_german_umlauts_and_short_lines_do_not_warn():
    assert bst.translation_warnings(["Kara delik", "Güneş sönecek mi?"], ["Schwarzes Loch", "Wird die Sonne erlöschen?"],
                                    "de") == []


def test_translation_keeps_the_source_timing_and_splits_only_overlong_lines():
    cues = [Cue(0, 2000, "Kısa"), Cue(2000, 6000, "Uzun")]
    long = ("This translation is far too long to fit into two subtitle lines of forty-two characters, "
            "so it has to be split inside its own time window.")
    translated, info = bst.build_translation(cues, "en", "", llm(json.dumps(["Short", long])))
    assert (translated[0].start, translated[0].end, translated[0].text) == (0, 2000, "Short")
    assert translated[1].start == 2000 and translated[-1].end == 6000 and len(translated) == 3
    assert info["uyarilar"][-1] == "1 altyazı iki satıra sığmadı, kendi süresi içinde bölündü"


def test_write_subtitles_writes_utf8_without_bom_and_crlf(tmp_path):
    subtitle = tmp_path / "subtitle.srt"
    subtitle.write_bytes(srt(["1", "00:00:00,000 --> 00:00:01,000", "Kara delikler ışığı yutar"]).encode("utf-8"))
    result = bst.write_subtitles(tmp_path, subtitle, "Kara delikler ışığı yutar.", 1500, "tr", ["en"],
                                 llm(json.dumps(["Black holes swallow light."])))
    assert result["dosyalar"] == ["altyazi.tr.srt", "altyazi.en.srt"] and result["uyarilar"] == []
    for name, text in (("altyazi.tr.srt", "Kara delikler ışığı yutar."), ("altyazi.en.srt", "Black holes swallow light.")):
        data = (tmp_path / name).read_bytes()
        assert not data.startswith(b"\xef\xbb\xbf") and b"\n" not in data.replace(b"\r\n", b"")
        assert data.decode("utf-8") == f"1\r\n00:00:00,000 --> 00:00:01,000\r\n{text}\r\n"


def test_missing_task_subtitle_is_a_warning(tmp_path):
    result = bst.write_subtitles(tmp_path, tmp_path / "yok.srt", "metin", 1000, "tr", ["en"], llm("[]"))
    assert result == {"dosyalar": [], "uyarilar": ["görevde subtitle.srt yok (altyazı kapalı mı?)"]}
