"""WebUI Toplu Üretim bölümü：列表编辑、导入导出、校验、启动/停止（假运行器）、历史，以及不写 config.toml。"""

import json
from pathlib import Path
from string import Formatter
from unittest.mock import patch

import pytest
from streamlit.testing.v1 import AppTest

from app.config import config
from app.services import batch_production as bp
from app.services import batch_runner as br
from app.services import voice
from webui import batch_page

ROOT = Path(__file__).resolve().parents[2]
LOCALES = sorted(path.stem for path in (ROOT / "webui/i18n").glob("*.json"))
VOICES = ["tr-TR-EmelNeural-Female", "tr-TR-AhmetNeural-Male"]
SCRIPT = """
import json
from pathlib import Path
from webui.batch_page import render_batch_page
tr = json.loads(Path({path!r}).read_text(encoding="utf-8"))["Translation"].__getitem__
render_batch_page(tr)
"""


def translations(locale):
    return json.loads((ROOT / f"webui/i18n/{locale}.json").read_text(encoding="utf-8"))["Translation"]


EN = translations("en")


@pytest.fixture
def batches(tmp_path, monkeypatch):
    folder = tmp_path / "batches"
    folder.mkdir()
    monkeypatch.setattr(bp, "batches_dir", lambda: folder)
    monkeypatch.setattr(voice, "get_all_azure_voices", lambda filter_locals=None: list(VOICES))
    monkeypatch.setattr(br, "current_lock", lambda: None)
    return folder


@pytest.fixture
def no_config_writes():
    with patch.object(config, "try_save_config") as soft, patch.object(config, "save_config") as hard:
        yield
    soft.assert_not_called()
    hard.assert_not_called()


def page(locale="en"):
    app = AppTest.from_string(SCRIPT.format(path=str(ROOT / f"webui/i18n/{locale}.json")), default_timeout=60)
    run(app)
    return app


def run(app):
    app.run()
    assert not app.exception, [str(item.value) for item in app.exception]
    return app


def editor_key(app):
    return f"batch_editor_{app.session_state['batch_editor_version']}"


def edit(app, *, added=(), edited=None, deleted=()):
    """st.data_editor'ün düzenleme durumu (AppTest tabloya tıklayamaz)."""
    app.session_state[editor_key(app)] = {"edited_rows": edited or {}, "added_rows": list(added),
                                          "deleted_rows": list(deleted)}
    return run(app)


def messages(elements):
    return [item.value for item in elements]


# ---------------------------------------------------------------- liste

def test_empty_page_shows_the_preset_summary_and_refuses_to_start(batches, no_config_writes):
    app = page()
    summary = dict(app.dataframe[0].value.itertuples(index=False))
    assert summary == {"Source": "nasa → pixabay", "Voice": "tr-TR-EmelNeural-Female", "Aspect ratio": "9:16",
                       "Target words": "90–100", "LLM": "claude_code", "Subtitle translations": "en"}
    assert app.selectbox(key="batch_preset_select").value == "uzay.toml"
    app.button(key="batch_start").click()
    run(app)
    assert messages(app.error) == ["- Add at least one subject."]


def test_rows_can_be_added_edited_and_deleted(batches, no_config_writes):
    batch_page.save_draft([{"konu": "Ay"}, {"konu": "Silinecek"}, {"konu": "Güneş"}], "uzay.toml")
    app = page()
    edit(app, edited={0: {"talimat": "Kısa tut"}, 2: {"ses": VOICES[1]}}, deleted=[1],
         added=[{"konu": "Ay neden hep aynı yüzünü gösterir?"}])
    draft = json.loads((batches / batch_page.DRAFT_FILE).read_text(encoding="utf-8"))
    assert draft["rows"] == [
        {"konu": "Ay", "talimat": "Kısa tut", "ses": ""},
        {"konu": "Güneş", "talimat": "", "ses": VOICES[1]},
        {"konu": "Ay neden hep aynı yüzünü gösterir?", "talimat": "", "ses": ""},
    ]
    assert draft["preset"] == "uzay.toml"


def test_clear_empties_the_list(batches):
    batch_page.save_draft([{"konu": "Ay"}], "uzay.toml")
    app = page()
    app.button(key="batch_clear").click()
    run(app)
    assert app.session_state["batch_rows"] == [] and app.dataframe[1].value.empty
    assert json.loads((batches / batch_page.DRAFT_FILE).read_text(encoding="utf-8"))["rows"] == []


def test_empty_cells_of_new_rows_are_blank_not_none(batches):
    app = page()
    columns = json.loads(app.dataframe[1].proto.columns)
    assert columns["konu"]["default"] == "" and columns["talimat"]["default"] == ""
    # SelectboxColumn draws "" as a grey "None"; a single space is drawn blank.
    assert batch_page.EMPTY_VOICE == " "
    assert columns["ses"]["default"] == " " and columns["ses"]["type_config"]["options"] == [" ", *VOICES]
    assert columns["ses"]["help"] == "Empty: the preset's voice"
    hint = [item.value for item in app.caption if "preset's voice" in item.value]
    assert hint and hint[0].startswith("Empty voice cells use the preset's voice (tr-TR-EmelNeural-Female).")
    # Ses seçilmemiş eklenmiş satır taslakta "" olarak durur.
    edit(app, added=[{"konu": "Ay"}])
    assert json.loads((batches / batch_page.DRAFT_FILE).read_text(encoding="utf-8"))["rows"] == [
        {"konu": "Ay", "talimat": "", "ses": ""}]


def test_translation_languages_start_from_the_preset_and_apply_to_this_batch_only(batches, no_config_writes):
    preset_bytes = (ROOT / "presets/uzay.toml").read_bytes()
    app = page()
    choice = app.multiselect(key="batch_translations_uzay.toml")
    assert choice.value == ["en"] and "tr" not in choice.options and "de · Deutsch" in choice.options
    choice.select("de").unselect("en")
    run(app)
    edit(app, added=[{"konu": "Ay"}])
    with patch.object(br, "start_batch", return_value=4242) as start:
        app.button(key="batch_start").click()
        run(app)
    assert start.call_args.kwargs["translations"] == ["de"]
    assert (ROOT / "presets/uzay.toml").read_bytes() == preset_bytes


def test_no_translation_is_passed_as_an_empty_list(batches):
    app = page()
    app.multiselect(key="batch_translations_uzay.toml").unselect("en")
    run(app)
    edit(app, added=[{"konu": "Ay"}])
    with patch.object(br, "start_batch", return_value=4242) as start:
        app.button(key="batch_start").click()
        run(app)
    assert start.call_args.kwargs["translations"] == []


def test_draft_survives_a_new_session(batches):
    batch_page.save_draft([{"konu": "Kara delikler", "talimat": "Kısa tut", "ses": ""}], "uzay.toml")
    app = page()
    assert app.session_state["batch_rows"] == [{"konu": "Kara delikler", "talimat": "Kısa tut", "ses": ""}]
    assert app.dataframe[1].value["konu"].tolist() == ["Kara delikler"]


def test_empty_voice_of_rows_loaded_from_the_draft_is_blank_not_none(batches, no_config_writes):
    """Rows from the draft (or an older one with null/missing voices) reach the editor with a blank voice."""
    (batches / batch_page.DRAFT_FILE).write_text(json.dumps({"rows": [
        {"konu": "Ay", "talimat": "", "ses": ""},
        {"konu": "Güneş", "talimat": None, "ses": None},
        {"konu": "Mars"},
        {"konu": "Satürn", "talimat": "", "ses": VOICES[1]},
    ], "preset": "uzay.toml"}), encoding="utf-8")
    app = page()
    shown = app.dataframe[1].value
    assert shown["ses"].tolist() == [" ", " ", " ", VOICES[1]]
    assert shown["talimat"].tolist() == ["", "", "", ""]
    # The blank exists only in the table: the session, the draft and the CSV keep "".
    expected = [{"konu": subject, "talimat": "", "ses": voice_name}
                for subject, voice_name in (("Ay", ""), ("Güneş", ""), ("Mars", ""), ("Satürn", VOICES[1]))]
    assert app.session_state["batch_rows"] == expected
    edit(app, edited={0: {"talimat": "Kısa tut"}})
    draft = json.loads((batches / batch_page.DRAFT_FILE).read_text(encoding="utf-8"))
    assert [row["ses"] for row in draft["rows"]] == ["", "", "", VOICES[1]]
    assert draft["rows"][0] == {"konu": "Ay", "talimat": "Kısa tut", "ses": ""}
    assert batch_page.rows_csv(draft["rows"]).decode("utf-8-sig").splitlines()[1:3] == ["Ay;Kısa tut;", "Güneş;;"]


@pytest.mark.parametrize("rows, expected", [
    ([{"konu": " ", "talimat": "yalnız talimat"}], ["- Row 1: the subject is empty."]),
    ([{"konu": "Ay"}, {"konu": "ay "}], ["- Row 2: \"ay\" is already in row 1."]),
    ([{"konu": "Ay", "ses": "tr-TR-YokNeural-Female"}], ["- Row 1: unknown voice \"tr-TR-YokNeural-Female\"."]),
])
def test_start_validates_the_list(batches, rows, expected):
    app = page()
    edit(app, added=rows)
    with patch.object(br, "start_batch") as start:
        app.button(key="batch_start").click()
        run(app)
    start.assert_not_called()
    assert messages(app.error) == ["\n".join(expected)]


def test_import_csv_with_bom_and_semicolons(batches, no_config_writes):
    app = page()
    content = "konu;talimat;ses\r\nNötron yıldızları;;\r\n# taslak;;\r\nSatürn;Kısa;tr-TR-AhmetNeural-Male\r\n"
    app.file_uploader[0].set_value(("konular.csv", content.encode("utf-8-sig"), "text/csv"))
    run(app)
    assert app.session_state["batch_rows"] == [
        {"konu": "Nötron yıldızları", "talimat": "", "ses": ""},
        {"konu": "Satürn", "talimat": "Kısa", "ses": "tr-TR-AhmetNeural-Male"},
    ]
    assert "2 subjects imported." in messages(app.success)
    assert app.dataframe[1].value["konu"].tolist() == ["Nötron yıldızları", "Satürn"]


def test_import_txt_and_reject_an_empty_file(batches):
    app = page()
    app.file_uploader[0].set_value(("konular.txt", "# yorum\n\nAy\n".encode(), "text/plain"))
    run(app)
    assert app.session_state["batch_rows"] == [{"konu": "Ay", "talimat": "", "ses": ""}]
    app.file_uploader[0].set_value(("bos.txt", b"# yorum\n", "text/plain"))
    run(app)
    assert messages(app.error)[0].startswith("The list could not be read:")


def test_export_writes_the_format_batch_py_reads(tmp_path):
    data = batch_page.rows_csv([{"konu": "Ay; Dünya", "talimat": "", "ses": ""}, {"konu": "", "talimat": "", "ses": ""},
                                {"konu": "Güneş", "talimat": "Kısa tut", "ses": VOICES[0]}])
    (tmp_path / "k.csv").write_bytes(data)
    assert data.startswith("\ufeffkonu;talimat;ses".encode("utf-8"))
    assert bp.read_inputs(tmp_path / "k.csv") == [bp.BatchInput("Ay; Dünya"),
                                                  bp.BatchInput("Güneş", "Kısa tut", VOICES[0])]


# ---------------------------------------------------------------- başlatma ve durdurma

def test_start_writes_the_input_under_storage_batches_and_calls_the_runner(batches, no_config_writes):
    app = page()
    edit(app, added=[{"konu": "Ay neden hep aynı yüzünü gösterir?"}, {"konu": "Güneş bir gün sönecek mi?"}])
    with patch.object(br, "start_batch", return_value=4242) as start:
        app.button(key="batch_start").click()
        run(app)
    (input_path, preset_path, folder), _ = start.call_args
    assert folder.parent == batches and folder.name.endswith("_webui") and input_path == folder / "konular.csv"
    assert preset_path == batch_page.presets_dir() / "uzay.toml"
    assert [item.subject for item in bp.read_inputs(input_path)] == ["Ay neden hep aynı yüzünü gösterir?",
                                                                    "Güneş bir gün sönecek mi?"]
    assert f"Batch started: {folder.name}" in messages(app.success)


def test_start_reports_a_running_batch(batches):
    app = page()
    edit(app, added=[{"konu": "Ay"}])
    busy = br.BatchBusy({"pid": 1, "batch_dir": str(batches / "20261105-100000_webui")})
    with patch.object(br, "start_batch", side_effect=busy):
        app.button(key="batch_start").click()
        run(app)
    assert messages(app.error) == ["Another batch is running: 20261105-100000_webui"]


def running_status(folder):
    return br.BatchStatus(folder, "2026-11-05T10:00:00", "", True, [
        br.ItemStatus(1, "Ay", "done", seconds=200, attempts=1),
        br.ItemStatus(2, "Güneş", "running", stage="materials", seconds=65, attempts=1),
        br.ItemStatus(3, "Mars", "pending"),
    ], elapsed=265, remaining=335, log_tail=[
        '2026-11-05 10:04:25 | INFO | "./app/services/generation_progress.py:45": publish - generation activity: '
        "phase=downloading, provider=, remote_id=, elapsed=3s",
    ])


def test_running_batch_shows_status_progress_log_and_stops(batches, monkeypatch):
    folder = batches / "20261105-100000_webui"
    lock = {"pid": 1, "batch_dir": str(folder), "started": "2026-11-05T10:00:00"}
    locks = [lock]
    monkeypatch.setattr(br, "current_lock", lambda: locks[0])
    monkeypatch.setattr(br, "batch_status", lambda directory, lock=None, now=None: running_status(folder))
    app = page()
    assert app.button(key="batch_start").disabled
    table = app.dataframe[0].value
    assert table["Status"].tolist() == ["Done", "Running: materials", "Waiting"]
    assert table["Time"].tolist() == ["3:20", "1:05", ""]
    assert app.get("progress")[0].proto.text == "1 of 3 subjects finished · elapsed 4:25 · about 5:35 left"
    assert app.code[0].value == "10:04:25 INFO    generation activity: phase=downloading, provider=, remote_id=, elapsed=3s"

    def stop():
        locks[0] = None
        return "stopped"

    with patch.object(br, "stop_batch", side_effect=stop) as stopper:
        app.button(key="batch_stop").click()
        run(app)
    stopper.assert_called_once()
    assert "The batch stopped. You can resume it from History." in messages(app.success)
    assert not app.button(key="batch_start").disabled


def ended_after_running(batches, monkeypatch, prepare):
    """Önce çalışırken çizilir, sonra kilit kalkar: bölüm sonucu klasörden okur."""
    folder = batches / "20261105-100000_webui"
    folder.mkdir()
    prepare(folder)
    locks = [{"pid": 1, "batch_dir": str(folder)}]
    monkeypatch.setattr(br, "current_lock", lambda: locks[0])
    monkeypatch.setattr(br, "batch_status", lambda directory, lock=None, now=None: running_status(folder))
    app = page()
    locks[0] = None
    return run(app)


def test_batch_that_could_not_start_shows_the_last_error_line(batches, monkeypatch):
    def prepare(folder):
        (folder / bp.LOG_FILE).write_text(
            "2026-11-05 20:33:06.746 | INFO     | app.config.config:load_config:571 - load config\n"
            '2026-11-05 20:33:07 | ERROR | "./app/services/batch_production.py:631": main - başka bir toplu '
            "üretim çalışıyor\n", encoding="utf-8")

    app = ended_after_running(batches, monkeypatch, prepare)
    assert messages(app.error) == ["The batch process ended early: 20:33:07 ERROR   başka bir toplu üretim çalışıyor"]


@pytest.mark.parametrize("finished, kind, text", [
    ("2026-11-05T11:00:00", "success", "The batch ended: 20261105-100000_webui"),
    ("", "warning", "The batch stopped. You can resume it from History."),
])
def test_batch_end_is_reported_from_its_state(batches, monkeypatch, finished, kind, text):
    def prepare(folder):
        _, state = make_state(folder)
        state.finished = finished
        state.save(folder)

    app = ended_after_running(batches, monkeypatch, prepare)
    assert text in messages(getattr(app, kind))


def test_crashed_batch_shows_the_exception(batches, monkeypatch):
    def prepare(folder):
        make_state(folder)
        (folder / bp.LOG_FILE).write_text(
            "2026-11-05 20:39:35 | INFO | x: _run_in_folder - toplu üretim: 2 konu, 0 tanesi daha önce bitti\n"
            "Traceback (most recent call last):\n  File \"batch.py\", line 9, in <module>\n"
            "PermissionError: [WinError 5] Erişim engellendi\n", encoding="utf-8")

    app = ended_after_running(batches, monkeypatch, prepare)
    assert messages(app.error) == ["The batch process ended early: PermissionError: [WinError 5] Erişim engellendi"]


def make_state(folder):
    source = folder / "konular.csv"
    source.write_text("konu\nA\n", encoding="utf-8")
    state = bp.new_state(source, bp.load_preset(ROOT / "presets/uzay.toml"), bp.read_inputs(source))
    state.save(folder)
    return source, state


def test_forced_stop_is_reported(batches, monkeypatch):
    folder = batches / "x"
    locks = [{"pid": 1, "batch_dir": str(folder)}]
    monkeypatch.setattr(br, "current_lock", lambda: locks[0])
    monkeypatch.setattr(br, "batch_status", lambda directory, lock=None, now=None: running_status(folder))
    app = page()
    with patch.object(br, "stop_batch", side_effect=lambda: (locks.__setitem__(0, None), "terminated")[1]):
        app.button(key="batch_stop").click()
        run(app)
    assert messages(app.warning) == [EN["batch.stop.forced"]]


# ---------------------------------------------------------------- geçmiş

def history(batches, local):
    for name, created, finished in (("20261104-100000_webui", "2026-11-04T10:00:00", "2026-11-04T10:30:00"),
                                    ("20261105-100000_webui", "2026-11-05T10:00:00", "")):
        folder = batches / name
        folder.mkdir()
        source = folder / "konular.csv"
        source.write_text("konu\nA\nB\n", encoding="utf-8")
        state = bp.new_state(source, bp.load_preset(ROOT / "presets/uzay.toml"), bp.read_inputs(source))
        state.created, state.finished = created, finished
        state.items[0].status, state.items[0].quality = "done", {"kontrol_gerekli": True}
        if finished:
            state.items[1].status = "failed"
        state.save(folder)
        (folder / bp.REPORT_FILE).write_text("<html></html>", encoding="utf-8")
    patcher = patch.object(batch_page, "local_access", return_value=local)
    patcher.start()
    return patcher


def test_history_lists_the_srt_files_of_each_video(batches):
    patcher = history(batches, local=True)
    try:
        video = batches / "20261104-100000_webui" / "01-a"
        video.mkdir()
        for name in ("altyazi.en.srt", "altyazi.tr.srt"):
            (video / name).write_text("1\n", encoding="utf-8")
        app = page()
        expanders = [item for item in app.expander if item.label == "Subtitle files"]
        assert len(expanders) == 1  # yalnız .srt'si olan toplu işte
        assert expanders[0].markdown[0].value == "- 1. A: `altyazi.tr.srt`, `altyazi.en.srt`"
    finally:
        patcher.stop()


def test_history_lists_batches_and_opens_reports_and_folders_locally(batches, no_config_writes):
    patcher = history(batches, local=True)
    try:
        app = page()
        text = [item.value for item in app.markdown if "subjects" in item.value]
        assert text[0].startswith("**2026-11-05 10:00** · `20261105-100000_webui` · unfinished")
        assert "2 subjects · 1 done · 0 failed · 1 need a check" in text[0]
        assert "finished" in text[1] and "2 subjects · 1 done · 1 failed · 1 need a check" in text[1]
        resume_buttons = [button.key for button in app.button if str(button.key).startswith("batch_resume_")]
        assert resume_buttons == ["batch_resume_20261105-100000_webui"]
        with patch.object(batch_page.webbrowser, "open") as browser:
            app.button(key="batch_report_20261104-100000_webui").click()
            run(app)
        browser.assert_called_once_with((batches / "20261104-100000_webui" / bp.REPORT_FILE).resolve().as_uri())
        with patch.object(batch_page, "open_folder") as folder:
            app.button(key="batch_folder_20261105-100000_webui").click()
            run(app)
        folder.assert_called_once_with(batches / "20261105-100000_webui")
    finally:
        patcher.stop()


def test_history_shows_paths_instead_of_opening_on_a_remote_server(batches):
    patcher = history(batches, local=False)
    try:
        app = page()
        with patch.object(batch_page.webbrowser, "open") as browser:
            app.button(key="batch_report_20261104-100000_webui").click()
            run(app)
        browser.assert_not_called()
        report = batches / "20261104-100000_webui" / bp.REPORT_FILE
        assert messages(app.info) == [f"On the server: {report}"]
    finally:
        patcher.stop()


def test_resume_from_history(batches, no_config_writes):
    patcher = history(batches, local=True)
    try:
        app = page()
        with patch.object(br, "resume_batch", return_value=4242) as resume:
            app.button(key="batch_resume_20261105-100000_webui").click()
            run(app)
        resume.assert_called_once_with(batches / "20261105-100000_webui")
        assert "Batch resumed: 20261105-100000_webui" in messages(app.success)
        error = br.ResumeError("changed_preset", "C:/x/uzay.toml")
        with patch.object(br, "resume_batch", side_effect=error):
            app.button(key="batch_resume_20261105-100000_webui").click()
            run(app)
        assert messages(app.error) == ["The preset changed after the batch started: C:/x/uzay.toml"]
    finally:
        patcher.stop()


def test_local_access_needs_a_loopback_address(monkeypatch):
    monkeypatch.setattr(batch_page.st, "get_option", lambda name: "127.0.0.1")
    assert batch_page.local_access() is (batch_page.sys.platform.startswith("win")
                                         or batch_page.sys.platform == "darwin" or batch_page.local_access())
    monkeypatch.setattr(batch_page.st, "get_option", lambda name: "0.0.0.0")
    assert batch_page.local_access() is False


def test_open_helpers_stay_inside_storage_batches(batches, tmp_path):
    outside = tmp_path / "baska"
    outside.mkdir()
    (outside / "rapor.html").write_text("x", encoding="utf-8")
    with patch.object(batch_page.webbrowser, "open") as browser, patch.object(batch_page.subprocess, "Popen") as popen:
        batch_page.open_report(outside / "rapor.html")
        batch_page.open_folder(outside)
    browser.assert_not_called()
    popen.assert_not_called()


# ---------------------------------------------------------------- Main.py ve config.toml

def main_app(view=None):
    app = AppTest.from_file(str(ROOT / "webui/Main.py"), default_timeout=90)
    app.session_state["ui_language"] = "en"
    if view:
        app.query_params["view"] = view
    return app


def test_batch_view_in_main_does_not_render_the_form_or_write_config(batches):
    config_file = Path(config.config_file)
    before = config_file.read_bytes()
    with patch.object(config, "try_save_config") as soft, patch.object(config, "save_config") as hard:
        app = run(main_app("batch"))
    soft.assert_not_called()
    hard.assert_not_called()
    assert config_file.read_bytes() == before
    keys = {button.key for button in app.button}
    assert "batch_start" in keys and "generate_video_button" not in keys
    assert app.button(key="batch_view_toggle_button").label == "Single Video"


def test_single_video_page_warns_while_a_batch_runs(batches, monkeypatch):
    monkeypatch.setattr(br, "current_lock", lambda: {"pid": 1, "batch_dir": str(batches / "20261105-100000_webui")})
    with patch.object(config, "try_save_config", return_value=True):
        app = run(main_app())
    assert app.button(key="batch_view_toggle_button").label == "Batch Production"
    assert EN["batch.single_warning"].format(name="20261105-100000_webui") in messages(app.warning)
    assert any(button.key == "generate_video_button" for button in app.button)


# ---------------------------------------------------------------- 14 dil

@pytest.mark.parametrize("locale", LOCALES)
def test_every_batch_message_is_translated_with_matching_placeholders(locale):
    localized = translations(locale)
    keys = [key for key in EN if key.startswith("batch.")]
    for key in keys:
        assert localized.get(key, "").strip(), (locale, key)
        fields = [{name for _, name, _, _ in Formatter().parse(text) if name} for text in (EN[key], localized[key])]
        assert fields[0] == fields[1], (locale, key)


def test_every_key_used_by_the_page_exists():
    keys = {key for key in EN if key.startswith("batch.")}
    source = (ROOT / "webui/batch_page.py").read_text(encoding="utf-8") + (ROOT / "webui/Main.py").read_text(
        encoding="utf-8")
    import re

    used = set(re.findall(r"""tr\(["'](batch\.[a-z_.]+)["']\)""", source))
    assert used and used <= keys
    families = {"batch.status.": ("pending", "running", "done", "review", "failed"),
                "batch.stage.": set(br.STAGES.values()),
                "batch.history.state.": ("running", "finished", "unfinished"),
                "batch.resume.": ("state", "missing_input", "missing_preset", "changed_input", "changed_preset")}
    for prefix, names in families.items():
        assert {prefix + name for name in names} <= keys, prefix


@pytest.mark.parametrize("locale", ["tr", "zh"])
def test_page_renders_in_other_languages(batches, locale):
    app = page(locale)
    assert app.subheader[0].value == translations(locale)["batch.title"]
