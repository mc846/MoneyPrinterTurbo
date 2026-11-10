"""NASA description words in the tag relevance, with a lower weight than keywords and titles. No network."""

import sys
from fractions import Fraction
from pathlib import Path

ROOT_DIR = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT_DIR))

from app.models.schema import MaterialInfo, MaterialTagRelevance, VideoAspect  # noqa: E402
from app.services import material, material_cache, nasa  # noqa: E402

TORTILLA = {
    "nasa_id": "iss042e277408",
    "title": "iss042e277408",
    "keywords": ["International Space Station (ISS)", "Expedition 42"],
    "description": "iss042e277408 (02/23/2015) --- A tortilla with beef and cheese floats inside the "
                   "International Space Station. Credit: NASA/Johnson Space Center. Follow us @Space_Station",
}


def _item(data, term="tortilla"):
    return MaterialInfo(provider="nasa", url=f"https://images-assets.nasa.gov/image/{data['nasa_id']}.jpg",
                        duration=5, source_info={"provider": "nasa", "search_term": term,
                                                 "asset_id": data["nasa_id"], "tags": nasa.relevance_tags(data),
                                                 "description_tags": nasa.description_tags(data),
                                                 "ai_generated": False})


def test_description_tags_drop_credits_links_and_institutions():
    data = {"description": "The Johns Hopkins Applied Physics Laboratory built the probe. It studies the "
                           "solar wind. See https://nasa.gov/x for more. Credit: NASA/JHUAPL"}
    tags = nasa.description_tags(data)
    words = {word for tag in tags for word in material._relevance_words(tag)}
    assert {"probe", "solar", "wind"} <= words
    assert not {"physics", "laboratory", "jhuapl", "nasa"} & words  # institution, credit, link
    assert nasa.description_tags({"description": "All-Star pulsar"}) == ["AllStar pulsar"]  # one word, as in tags


def test_description_tags_keep_only_the_opening_words():
    long_text = " ".join(f"word{i}" for i in range(200))
    words = [word for tag in nasa.description_tags({"description": long_text}) for word in tag.split()]
    assert len(words) == nasa.DESCRIPTION_TAG_WORDS and words[-1] == f"word{nasa.DESCRIPTION_TAG_WORDS - 1}"
    assert all(len(tag) <= 100 for tag in nasa.description_tags({"description": long_text}))


def test_a_word_only_in_the_description_counts_half():
    item = _item(TORTILLA)
    assert material._tag_relevance("tortilla", item) == (Fraction(1, 2), 1)
    # Reaches the default fallback threshold 0.5: the right ISS image is suitable again.
    assert material._qualified_candidate_count("tortilla", [item], MaterialTagRelevance.rank, 0.5) == 1
    # One word in the tags and one only in the description: (1 + 1/2) / 2.
    assert material._tag_relevance("station tortilla", item)[0] == Fraction(3, 4)
    # A word in the tags is not counted twice.
    assert material._tag_relevance("space station", item)[0] == 1


def test_the_description_supplies_at_most_one_missing_word():
    scattered = _item({"nasa_id": "S1", "title": "ChemCam", "keywords": ["MSL"],
                       "description": "The instrument uses a pulsed laser beam to vaporize a target, "
                                      "producing a flash of light from the plasma."}, "light beam")
    adjacent = _item({"nasa_id": "A1", "title": "Earth Observations", "keywords": [],
                      "description": "A beam of light shines down on the Earth."}, "light beam")
    # Two words missing from the tags: the description does not count, scattered or not.
    assert material._tag_relevance("light beam", scattered) == (0, 0)
    assert material._tag_relevance("light beam", adjacent) == (0, 0)
    # One word missing: the description supplies it at half weight.
    titled = _item({"nasa_id": "T2", "title": "Laser beam test", "keywords": [],
                    "description": "A narrow beam crosses the room. The light is green."}, "light beam")
    assert material._tag_relevance("light beam laser", titled) == (Fraction(2, 3) + Fraction(1, 6), 3)


def test_keywords_and_titles_still_rank_above_the_description():
    titled = _item({"nasa_id": "T1", "title": "Tortilla on the station", "keywords": [], "description": ""})
    described = _item(TORTILLA)
    unrelated = _item({"nasa_id": "U1", "title": "Earth at night", "keywords": ["Earth"], "description": "City lights."})
    ordered = material._order_material_candidates([unrelated, described, titled], VideoAspect.portrait,
                                                  tag_relevance=MaterialTagRelevance.rank)
    assert [item.source_info["asset_id"] for item in ordered] == ["T1", "iss042e277408", "U1"]
    strict = material._order_material_candidates([described, titled], VideoAspect.portrait,
                                                 tag_relevance=MaterialTagRelevance.strict)
    assert [item.source_info["asset_id"] for item in strict] == ["T1"]  # strict needs every word in the tags


def test_sources_without_description_tags_score_as_before():
    pixabay = MaterialInfo(provider="pixabay", url="https://cdn.example/p.mp4", duration=5,
                           source_info={"provider": "pixabay", "tags": ["tortilla", "food"], "ai_generated": False})
    assert material._tag_relevance("tortilla wrap", pixabay) == (Fraction(1, 2), 1)


def test_the_search_cache_keeps_the_description_tags():
    cached = material_cache._cached_source_info(_item(TORTILLA))
    assert cached["description_tags"] == nasa.description_tags(TORTILLA)
    assert all(len(tag) <= 100 for tag in cached["description_tags"])


def test_the_filter_version_changed_with_the_description_tags():
    # Older cached NASA results have no description_tags; the new version keys them out.
    assert nasa.FILTER_VERSION >= 5
