"""material_require_anchor: a candidate is suitable only when it names one of the task's anchors
(tags, title, NASA description; adjectives count as their noun). No network."""

import sys
from pathlib import Path
from unittest.mock import patch

ROOT_DIR = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT_DIR))

from app.config import config  # noqa: E402
from app.models.schema import MaterialInfo, MaterialTagRelevance, VideoAspect  # noqa: E402
from app.services import batch_production, material, source_chain  # noqa: E402

PORTRAIT = VideoAspect.portrait
RANK = MaterialTagRelevance.rank


def _item(asset, tags, term="impact basin", provider="nasa", description=()):
    info = {"provider": provider, "asset_id": asset, "search_term": term, "tags": list(tags),
            "ai_generated": False, "rendition": {"width": 1080, "height": 1920}}
    if description:
        info["description_tags"] = list(description)
    return MaterialInfo(provider=provider, url=f"https://x/{asset}", duration=5, source_info=info)


def _ids(items):
    return [item.source_info["asset_id"] for item in items]


def test_anchor_words_use_the_relevance_normalisation_and_adjectives():
    assert material._anchor_words("Lunar surfaces") == {"moon", "surface"}
    assert material._anchor_words("The Moon's craters") == {"moon", "crater"}
    assert material._anchor_words("Martian dust storm") == {"mars", "dust", "storm"}
    assert material._anchor_words("solar flare") == {"sun", "flare"}
    assert material._anchor_words("Jovian moons") == {"jupiter", "moon"}
    assert material._anchor_words("galactic core") == {"galaxy", "core"}


def test_anchor_hit_reads_tags_titles_and_the_nasa_description():
    anchors = ["moon", "far side"]
    assert material._anchor_hit(_item("a", ["Apollo 16", "Lunar surface panorama"]), anchors)
    assert not material._anchor_hit(_item("b", ["Mars Reconnaissance Orbiter", "Crater Ejecta"]), anchors)
    described = _item("c", ["Orion", "Earthrise"], description=["The Moon passes in front of Earth."])
    assert material._anchor_hit(described, anchors)
    # Every word of an anchor must appear: "far" alone is not "far side".
    assert not material._anchor_hit(_item("d", ["far away galaxy"]), ["far side"])
    assert material._anchor_hit(_item("e", ["the far side"]), ["far side"])


def test_no_anchors_or_no_tags_means_no_rule():
    assert material._anchor_hit(_item("a", ["Mars"]), [])
    untagged = MaterialInfo(provider="pexels", url="https://x/p", duration=5, source_info={"provider": "pexels"})
    assert material._anchor_hit(untagged, ["moon"])


def test_ordering_drops_candidates_without_an_anchor_only_when_asked():
    mercury = _item("PIA13675", ["Mercury Caloris Basin, One of the Largest Impact Basins"])
    moon = _item("moon1", ["lunar impact basin"])
    items = [mercury, moon]
    common = {"tag_relevance": RANK, "anchors": ["moon"]}
    assert _ids(material._order_material_candidates(items, PORTRAIT, **common)) == ["PIA13675", "moon1"]
    assert _ids(material._order_material_candidates(items, PORTRAIT, require_anchor=True, **common)) == ["moon1"]
    # Without anchors, or with tag relevance off, the rule does nothing.
    assert len(material._order_material_candidates(items, PORTRAIT, tag_relevance=RANK, anchors=[],
                                                   require_anchor=True)) == 2
    assert len(material._order_material_candidates(items, PORTRAIT, anchors=["moon"], require_anchor=True)) == 2


def _stock_search(items, require, anchors=("moon",)):
    with patch.dict(config.app, {"material_require_anchor": require}):
        search, order = material._stock_source_search(
            "nasa", crop_to_aspect=False, tag_relevance=RANK, exclude_ai_generated=False, anchors=list(anchors))
    return search, order


def test_the_setting_reaches_the_ordering_and_the_record():
    mars = _item("PIA18065", ["Crater Ejecta", "Mars"], term="overlapping craters")
    moon = _item("as16", ["overlapping craters on the Moon"], term="overlapping craters")
    search, order = _stock_search([mars, moon], True)
    assert order["require_anchor"] is True
    with (patch.object(material, "_search_videos_with_cache", return_value=[mars, moon]),
          patch.object(material.task_materials, "recording", return_value=True),
          patch.object(material.task_materials, "searched") as searched,
          patch.dict(config.app, {"nasa_text_filter": False})):
        found = search("overlapping craters", 5, PORTRAIT)
    assert _ids(found) == ["as16"]
    dropped = searched.call_args.args[3]
    assert [(item.source_info["asset_id"], reason) for item, reason in dropped] == [("PIA18065", "anchor_missing")]
    # Off by default, and without anchors the option is not even set.
    assert "require_anchor" not in _stock_search([], False)[1]
    assert "require_anchor" not in _stock_search([], True, anchors=())[1]


def test_merged_pool_options_keep_the_rule():
    merged = source_chain.merged_options(
        [{"crop_to_aspect": False, "tag_relevance": RANK, "exclude_ai_generated": False, "anchors": ("moon",),
          "require_anchor": True},
         {"crop_to_aspect": False, "tag_relevance": RANK, "exclude_ai_generated": False}], ["moon"])
    assert merged["require_anchor"] is True


def test_the_chain_moves_on_when_no_candidate_names_the_anchor():
    nasa_items = [_item("PIA13675", ["Mercury Caloris Basin, One of the Largest Impact Basins"])]
    pixabay_items = [_item("p1", ["moon", "impact", "basin"], provider="pixabay")]

    def fake_search(provider, search_videos, search_term, **kwargs):
        return list(nasa_items if provider == "nasa" else pixabay_items)

    with (patch.dict(config.app, {"material_require_anchor": True, "nasa_text_filter": False,
                                  "pixabay_api_keys": ["k"]}),
          patch.object(material, "_search_videos_with_cache", side_effect=fake_search)):
        search, _ = material.chain_source_search("nasa", ["pixabay"], crop_to_aspect=False, tag_relevance=RANK,
                                                 exclude_ai_generated=False, anchors=["moon"])
        found = search("impact basin", 5, PORTRAIT)
    assert _ids(found) == ["p1"]
    decision = search.state.decisions["impact basin"]
    assert decision["chosen"] == "pixabay" and decision["tried"][0]["candidates"] == 0


def test_the_gercek_preset_requires_an_anchor():
    preset = batch_production.load_preset(ROOT_DIR / "presets" / "gercek.toml")
    assert preset.app["material_require_anchor"] is True
