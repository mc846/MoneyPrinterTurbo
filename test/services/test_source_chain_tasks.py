"""The fallback chain in main's task layers: the quality report's log summary, share-text credits,
the space preset, the batch page summary and the revision panel's new search. No network."""
# The task fixtures come from test_task_revisions; tests name them as arguments.
# ruff: noqa: F811

import json
from pathlib import Path

import pytest
from PIL import Image

from app.config import config
from app.models.schema import MaterialInfo
from app.services import batch_package, material, scene_edits, source_chain, task_materials
from app.services import batch_production as bp
from test.services.test_task_revisions import (  # noqa: F401  (fixtures)
    _config_file_hash,
    _make_task,
    _revision_page,
    storage,
)

ROOT_DIR = Path(__file__).resolve().parents[2]


def test_the_quality_report_reads_the_chain_lines():
    lines = [
        "material source chain: term='astronaut food', tried=nasa:0/0,pixabay:8/3, using=pixabay, reason=met",
        "material source chain: term='mars rover', tried=nasa:6/4, using=nasa, reason=met",
        "material source chain: term='rare term', tried=nasa:0/0,pixabay:0/0,coverr:0/0:skipped, using=None, "
        "reason=no_candidates",
        "material fallback: term='old line', nasa=0/0 qualified, pixabay=2/2 qualified, min_relevance=0.5, "
        "min_candidates=1, using=pixabay",
    ]
    summary = batch_package.log_summary(lines, ["astronaut food", "mars rover", "rare term", "old line"])
    assert summary["yedege_gecen_terimler"] == ["astronaut food", "old line"]
    assert summary["bos_terimler"] == ["rare term"]
    assert summary["onbellekten_gelen_aramalar"] == 0


def test_share_text_credits_name_every_stock_source():
    nasa_image = {"provider": "nasa", "asset_id": "N1"}
    pixabay = {"provider": "pixabay", "asset_id": "P1"}
    coverr = {"provider": "coverr", "asset_id": "C1", "creator": {"name": "Jane Doe"}}
    pexels = {"provider": "pexels", "asset_id": "X1"}
    assert batch_package.credit_lines([nasa_image, pixabay]) == [
        "Görüntüler: NASA ve ortakları (images.nasa.gov)", batch_package.PIXABAY_CREDIT]
    assert batch_package.credit_lines([pixabay, coverr, pexels]) == [
        "Ek görüntüler: Pixabay (pixabay.com); Coverr (coverr.co): Jane Doe; Pexels (pexels.com)"]
    assert batch_package.credit_lines([{"provider": "coverr", "asset_id": "C2"}]) == [
        "Ek görüntüler: Coverr (coverr.co)"]


def test_the_space_preset_chains_pixabay_then_coverr_and_a_missing_coverr_key_only_warns(caplog_loguru):
    preset = bp.load_preset(ROOT_DIR / "presets" / "uzay.toml")
    assert preset.video["video_source_fallback"] == ["pixabay", "coverr"]
    argv = bp.video_option_argv({"video_source_fallback": preset.video["video_source_fallback"]})
    assert argv == ["--video-source-fallback", "pixabay, coverr"]
    import cli

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(config, "ui", {})
        params = cli.build_video_params(cli.parse_args(["--video-subject", "x", "--video-source", "nasa", *argv]))
    assert params.video_source_fallback == ["pixabay", "coverr"]
    keys = {"pixabay_api_keys": ["p-key"], "coverr_api_keys": []}
    assert source_chain.available("nasa", params.video_source_fallback, keys) == ["pixabay"]
    assert len([m for m in caplog_loguru if "coverr is skipped for this task" in m]) == 1


def test_the_batch_page_shows_the_chain():
    from webui import batch_page

    preset = bp.Preset(path="p.toml", sha256="", video={"video_source": "nasa",
                                                       "video_source_fallback": ["pixabay", "coverr"]})
    rows = dict(batch_page.preset_summary(preset, lambda key: key))
    assert rows["batch.preset.source"] == "nasa → pixabay → coverr"
    single = bp.Preset(path="p.toml", sha256="", video={"video_source": "nasa", "video_source_fallback": "pixabay"})
    assert dict(batch_page.preset_summary(single, lambda key: key))["batch.preset.source"] == "nasa → pixabay"


# ---------------------------------------------------------------- revision: new search with a source


def _fake_search(calls):
    def stock_source_search(source, **options):
        def search(search_term, minimum_duration, video_aspect):
            calls.append(source)
            items = [MaterialInfo(provider=source, url=f"https://cdn.example/{source}-{n}.mp4", duration=8,
                                  source_info={"provider": source, "asset_id": f"{source}-{n}",
                                               "tags": [search_term], "ai_generated": False})
                     for n in range(2)] if source != "nasa" else []
            task_materials.searched(source, search_term, items)
            return items

        return search, {"tag_relevance": options["tag_relevance"], "crop_to_aspect": False,
                        "exclude_ai_generated": False}
    return stock_source_search


def _with_chain(storage, task_id, chain):
    path = storage / "tasks" / task_id / "script.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["params"]["video_source_fallback"] = chain
    path.write_text(json.dumps(data), encoding="utf-8")


def test_a_new_search_uses_the_task_chain_or_one_chosen_source(storage, monkeypatch):
    task_id = _make_task(storage)
    _with_chain(storage, task_id, ["pixabay", "coverr"])
    calls = []
    monkeypatch.setattr(material, "_stock_source_search", _fake_search(calls))
    monkeypatch.setattr(task_materials, "_download_preview",
                        lambda url, target: Image.new("RGB", (8, 8)).save(target, "JPEG"))
    monkeypatch.setitem(config.app, "pixabay_api_keys", ["p-key"])
    monkeypatch.setitem(config.app, "coverr_api_keys", [])
    monkeypatch.setitem(config.app, "pexels_api_keys", ["x-key"])
    where = scene_edits.search_sources(task_id)
    assert where == {"chain": ["nasa", "pixabay", "coverr"], "sources": ["pexels", "pixabay", "nasa"]}
    scene = {"id": "v1-s03", "term_index": 3}
    # The task's chain: NASA has nothing, Pixabay is enough, Coverr (no key) is skipped.
    found = scene_edits.search(task_id, scene, "earth rise")
    assert calls == ["nasa", "pixabay"]
    assert found["provider"] == "pixabay" and {c["provider"] for c in found["candidates"]} == {"pixabay"}
    assert found["source_chain"]["chosen"] == "pixabay"
    # One chosen source: only that one is asked; its candidates keep their provider.
    calls.clear()
    found = scene_edits.search(task_id, scene, "earth rise", "pexels")
    assert calls == ["pexels"] and found["provider"] == "pexels"
    manifest = json.loads((storage / "tasks" / task_id / "materials" / "materials.json").read_text(encoding="utf-8"))
    assert [(t["term"], t["provider"]) for t in manifest["terms"] if t.get("scene")] == [
        ("earth rise", "pixabay"), ("earth rise", "pexels")]
    with pytest.raises(scene_edits.SceneEditError, match="not a stock source with an API key"):
        scene_edits.search(task_id, scene, "earth rise", "coverr")


def test_the_replace_dialog_offers_the_chain_and_each_source(storage, monkeypatch):
    task_id = _make_task(storage)
    _with_chain(storage, task_id, ["pixabay"])
    monkeypatch.setitem(config.app, "pixabay_api_keys", ["p-key"])
    monkeypatch.setitem(config.app, "coverr_api_keys", [])
    monkeypatch.setitem(config.app, "pexels_api_keys", [])
    searched = []
    monkeypatch.setattr(scene_edits, "search", lambda task, scene, words, source="": searched.append(
        (scene["id"], words, source)) or {"candidates": [], "term": words, "provider": source or "nasa"})
    monkeypatch.setattr(config, "save_config", lambda *a, **k: pytest.fail("config must not be saved"))
    before = _config_file_hash()
    app = _revision_page(task_id).run()
    app.button(key="revision_replace_btn_v1-s02").click().run()
    key = f"revision_replace_{task_id}_v1-s02"
    box = app.selectbox(key=f"{key}_source")
    assert box.options[0].endswith("(NASA → Pixabay)") and box.options[1:] == ["Pixabay", "NASA"]
    app.text_input(key=f"{key}_words").set_value("earth rise")
    box.set_value("Pixabay")
    app.button(key=f"{key}_search").click().run()
    assert searched == [("v1-s02", "earth rise", "pixabay")]
    assert _config_file_hash() == before


@pytest.fixture
def caplog_loguru():
    from loguru import logger

    messages = []
    handler = logger.add(lambda message: messages.append(str(message)), level="WARNING")
    yield messages
    logger.remove(handler)
