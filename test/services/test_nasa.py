"""NASA Image and Video Library 图片素材源。全部使用 test/resources/nasa 下的录制响应，不访问网络。"""

import json
import os
import random
import sys
import tomllib
from pathlib import Path
from unittest.mock import patch
from urllib.parse import unquote

import pytest
from PIL import Image, ImageDraw, ImageFilter

ROOT_DIR = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT_DIR))
sys.path.insert(0, str(ROOT_DIR / "docs" / "skill"))

import cli  # noqa: E402
import mpt_agent  # noqa: E402
from app.config import config  # noqa: E402
from app.models.schema import (  # noqa: E402
    ImageMotion,
    MaterialInfo,
    MaterialTagRelevance,
    TaskVideoRequest,
    VideoAspect,
    VideoConcatMode,
    VideoParams,
)
from app.services import material, material_cache, nasa  # noqa: E402
from app.services import task as tm  # noqa: E402

FIXTURES = ROOT_DIR / "test" / "resources" / "nasa"
PORTRAIT = VideoAspect.portrait
# 录制的元数据缺少尺寸时，测试替代图片头探测返回的尺寸。
PROBED_SIZES = {
    "sl4-143-4707": (3890, 3844),
    "GSFC_20171208_Archive_e000504": (1280, 1280),
    "GSFC_20171208_Archive_e001027": (2400, 2400),
}


def _fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _fake_get_json(url, params=None):
    if url.endswith("/search"):
        return _fixture("search_images.json")
    if "/asset/" in url:
        return _fixture(f"asset_{unquote(url.rsplit('/', 1)[1])}.json")
    if url.endswith("metadata.json"):
        return _fixture(f"metadata_{unquote(url.split('/')[-2])}.json")
    raise AssertionError(f"unexpected request: {url}")


def _fake_probe(url):
    return PROBED_SIZES.get(unquote(url.split("/")[-2]))


def _search(term="star field", **kwargs):
    with (
        patch.object(nasa, "_get_json", side_effect=_fake_get_json) as get_json,
        patch.object(nasa, "_probe_image_size", side_effect=_fake_probe),
    ):
        items = nasa.search_images(term, 5, PORTRAIT, crop_to_aspect=True, **kwargs)
    return items, get_json


def _data(nasa_id):
    for item in _fixture("search_images.json")["collection"]["items"]:
        if item["data"][0]["nasa_id"] == nasa_id:
            return item["data"][0]
    raise KeyError(nasa_id)


# ------------------------------------------------------------------ 搜索


def test_search_parses_results_and_skips_filtered_candidates():
    items, get_json = _search()

    assert [i.source_info["asset_id"] for i in items] == [
        "PIA14103", "sl4-143-4707", "GSFC_20171208_Archive_e001027", "KSC-20251111-PH-JBS01_0011",
        "iss074e0472536",
    ]
    nebula = items[0]
    assert nebula.provider == "nasa"
    assert nebula.url == "https://images-assets.nasa.gov/image/PIA14103/PIA14103~orig.jpg"
    assert nebula.duration == 5
    assert nebula.source_info["source_page"] == "https://images.nasa.gov/details/PIA14103"
    assert nebula.source_info["rendition"] == {"id": "orig", "width": 7700, "height": 6300}
    assert nebula.source_info["credit"] == "NASA/JPL-Caltech/UCLA"
    assert nebula.source_info["ai_generated"] is False
    assert isinstance(nebula.source_info["tags"], list)
    # 光谱图被文字过滤，没有再请求资产清单；1280 px 的图片因分辨率被淘汰。KSC 的极光照片
    # 描述里没有人物角色词，单凭编号不再排除。
    requested_assets = {unquote(c.args[0].rsplit("/", 1)[1]) for c in get_json.call_args_list if "/asset/" in c.args[0]}
    assert "KSC-20251111-PH-JBS01_0011" in requested_assets
    assert "PIA07395" not in requested_assets
    assert "GSFC_20171208_Archive_e000504" in requested_assets


def test_search_request_uses_image_media_type():
    _, get_json = _search("black hole")

    url, params = get_json.call_args_list[0].args
    assert url == "https://images-api.nasa.gov/search"
    assert params == {"q": "black hole", "media_type": "image", "page_size": 50}


@pytest.mark.parametrize("body", [None, {}, {"collection": {"items": "x"}}])
def test_failed_or_unsupported_search_returns_no_candidates(body):
    with patch.object(nasa, "_get_json", return_value=body):
        assert nasa.search_images("black hole", 5, PORTRAIT) == []


@pytest.mark.parametrize(
    ("value", "searched"),
    [(["image"], True), (["image", "video"], True), ("image", True), (["video"], False), ([], False)],
)
def test_media_types_setting(value, searched):
    with (
        patch.dict(config.app, {"nasa_media_types": value}),
        patch.object(nasa, "_get_json", return_value=None) as get_json,
    ):
        nasa.search_images("black hole", 5, PORTRAIT)

    assert get_json.called is searched


def test_media_types_default_to_images():
    with patch.dict(config.app, {}, clear=False):
        config.app.pop("nasa_media_types", None)
        assert nasa.configured_media_types() == ("image",)


# ------------------------------------------------------------------ 规格选择


VARIANTS = {name: f"https://x/{name}.jpg" for name in ("orig", "large", "medium", "small")}


@pytest.mark.parametrize(
    ("size", "margin", "expected"),
    [
        ((7700, 6300), 1.15, ("orig", 7700, 6300)),  # large 只有 1920x1571，裁剪后不够
        ((2160, 3840), 1.15, ("orig", 2160, 3840)),  # large 刚好 1080x1920，缺放大余量
        ((2160, 3840), 1.0, ("large", 1080, 1920)),  # 不需要余量时选最小规格
        ((3840, 6827), 1.15, ("orig", 3840, 6827)),
        ((1125, 1125), 1.15, None),
        ((434, 434), 1.15, None),
        ((600, 4000), 1.15, None),
    ],
)
def test_choose_variant_smallest_that_meets_the_target(size, margin, expected):
    chosen = nasa.choose_variant(VARIANTS, *size, PORTRAIT, margin)

    assert (None if chosen is None else (chosen[0], chosen[2], chosen[3])) == expected


def test_choose_variant_prefers_a_variant_with_zoom_margin():
    # 4000x7111：large 1080x1920 只满足目标，medium/small 都不满足；orig 满足余量。
    assert nasa.choose_variant(VARIANTS, 4000, 7111, PORTRAIT, 1.15)[0] == "orig"
    assert nasa.choose_variant({"large": VARIANTS["large"]}, 4000, 7111, PORTRAIT, 1.15)[0] == "large"


def test_choose_variant_without_crop_requires_native_orientation():
    assert nasa.choose_variant(VARIANTS, 7700, 6300, PORTRAIT, 1.15, crop_to_aspect=False) is None
    assert nasa.choose_variant(VARIANTS, 2160, 3840, PORTRAIT, 1.15, crop_to_aspect=False)[0] == "orig"


def test_variant_files_keeps_image_renditions_only():
    files = [
        "http://images-assets.nasa.gov/image/a b/a b~orig.jpg",
        "http://images-assets.nasa.gov/image/a b/a b~large.jpg",
        "http://images-assets.nasa.gov/image/a b/a b~thumb.jpg",
        "http://images-assets.nasa.gov/image/a b/a b~orig.tif",
        "http://images-assets.nasa.gov/image/a b/metadata.json",
    ]

    assert nasa.variant_files(files) == {
        "orig": "https://images-assets.nasa.gov/image/a%20b/a%20b~orig.jpg",
        "large": "https://images-assets.nasa.gov/image/a%20b/a%20b~large.jpg",
    }


def test_image_size_from_metadata_fields():
    assert nasa.image_size(_fixture("metadata_PIA14103.json")) == (7700, 6300)
    assert nasa.image_size(_fixture("metadata_KSC-20251111-PH-JBS01_0011.json")) == (6240, 4160)
    assert nasa.image_size(_fixture("metadata_sl4-143-4707.json")) is None


# ------------------------------------------------------------------ 过滤


@pytest.mark.parametrize(
    ("description", "metadata", "expected"),
    [
        ("", _fixture("metadata_KSC-20251111-PH-JBS01_0011.json"), None),  # NASA 标准文字 + ICC
        ("", _fixture("metadata_iss074e0472536.json"), None),  # 只有 ICC 配置文件版权
        ("", {"XMP:Rights": "NASA", "IPTC:CopyrightNotice": "NASA/Ben Smegelsky"}, None),
        ("For copyright and restrictions, refer to http://www.nasa.gov/multimedia/guidelines", {}, None),
        ("Photo © 2015 Jane Doe Photography. Used with permission.", {}, "Jane Doe Photography"),
        ("Image (c) 2020 Some University", {}, "Some University"),
        ("", {"IPTC:CopyrightNotice": "Copyright ESA"}, "Copyright ESA"),
        ("", {"XMP:Rights": "ESA and NASA"}, "ESA and NASA"),
    ],
)
def test_third_party_copyright(description, metadata, expected):
    assert nasa.third_party_copyright({"description": description}, metadata) == expected


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        ({"nasa_id": "x", "title": "Astronaut portrait of Jane Doe"}, "astronaut"),
        ({"nasa_id": "x", "title": "Spacewalk", "keywords": ["Expedition 74 Crew"]}, "crew"),
        ({"nasa_id": "x", "title": "Media Briefing Preview"}, "briefing"),
        ({"nasa_id": "x", "title": "We Asked a NASA Expert"}, "expert"),
        ({"nasa_id": "x", "title": "Mars Express views Olympus Mons"}, None),
        ({"nasa_id": "x", "title": "Pressure suit test chamber", "keywords": ["vacuum"]}, None),
        # 国际空间站照片标题里的拍摄者说明不算人物
        ({"nasa_id": "iss006e28028", "title": "Southern Cross photographed by the Expedition 6 crew"}, None),
        ({"nasa_id": "iss034e039331", "title": "London at night", "keywords": ["Crew Earth Observations"]}, None),
        # NASA 总部摄影直接排除
        ({"nasa_id": "NHQ202606040047", "title": "Beyond the Deep Field"}, "NHQ202606040047"),
        # 中心摄影编号只在描述里有人物角色词时排除
        ({"nasa_id": "KSC-20251111-PH-JBS01_0011", "title": "Aurora Borealis at Kennedy Space Center"}, None),
        ({"nasa_id": "KSC-07pd3362", "title": "STA training",
          "description": "STS-122 Commander Steve Frick prepares to practice shuttle landings."},
         "KSC-07pd3362: commander"),
        ({"nasa_id": "05pd0883", "title": "STA", "description": "STS-114 Pilot James Kelly suits up for flights."},
         "05pd0883: pilot"),
        ({"nasa_id": "KSC-2010-4693", "title": "NuSTAR",
          "description": "Technicians move the second stage of the Pegasus XL rocket."}, None),
        ({"nasa_id": "PIA00001", "title": "Clouds", "description": "An astronaut photograph of clouds."}, None),
        ({"nasa_id": "KSC-20181212-PH_CSH02_0002", "title": "Applied Physics Lab",
          "description": "Dr. Mark Nurge, a physicist in the Applied Physics Laboratory, adjusted cables."},
         "KSC-20181212-PH_CSH02_0002: physicist"),
        ({"nasa_id": "KSC-20181212-PH_CSH02_0003", "title": "Magnet demo",
          "description": "Principal investigator, Dr. Janine Captain, demonstrates a magnet."},
         "KSC-20181212-PH_CSH02_0003: principal investigator"),
        ({"nasa_id": "GRC-1965-C-00811", "title": "Laser", "description": "Dr. Theodore Maiman examines the laser."},
         "GRC-1965-C-00811: dr. theodore"),
        # 姓名写法只对中心摄影编号生效，且区分大小写
        ({"nasa_id": "PIA00001", "title": "Galaxy", "description": "Dr. Jane Smith said the galaxy is old."}, None),
        ({"nasa_id": "KSC-2010-4693", "title": "Telescope", "description": "Scientists use the telescope daily."},
         None),
        # 活动用语（描述里也找）
        ({"nasa_id": "x", "title": "Hubble legacy",
          "description": "Julia Roman-Duval, second from right, speaks about the history of the Hubble Space "
                         "Telescope during an event titled “Beyond the Deep Field”."}, "speaks"),
        ({"nasa_id": "x", "title": "New Year", "description": "Guests celebrate New Years at the laboratory."},
         "guests"),
        ({"nasa_id": "x", "title": "Webb", "description": "Scientists join a panel discussion on exoplanets."},
         "panel discussion"),
        ({"nasa_id": "x", "title": "Center tour", "description": "Seen during an awards event at Goddard."},
         "during an awards event"),
        ({"nasa_id": "AFRC2019-0248-01", "title": "Confined Space Exercise Improves Response",
          "description": "A mannequin is used to simulate a worker who has collapsed."}, "mannequin"),
    ],
)
def test_identifiable_person_risk(data, expected):
    assert nasa.identifiable_person_risk(data) == expected


@pytest.mark.parametrize(
    "description",
    [
        "Lava tubes originate during eruption event, when the margins of a flow harden.",
        "Cliff faces at the center of the image were created during the collapse event that formed the caldera.",
        "This two-panel illustration shows a black hole surrounded by a disk of gas.",
        "The OWS solar panel on the left side was also lost on workshop launch day.",
        "This NASA/ESA Hubble Space Telescope image presents the Arches Cluster.",
        "The rover will search for signs of ancient microbial life.",
        "The auroras rotate from left to right, curving around the planet.",
        "As Cassini watches the rings pass in front of the red giant star Aldebaran.",
        "For more information about the DSN, visit: the mission page.",
        'Credit: NASA <a href="http://www.nasa.gov/audience/formedia/features/MP_Photo_Guidelines.html">'
        "NASA image use policy.</a>",
    ],
)
def test_event_language_ignores_scientific_wording(description):
    # 实测这些说法在科学图片说明里常见，单独出现时不代表有人
    assert nasa.identifiable_person_risk({"nasa_id": "PIA00001", "title": "Science", "description": description}) is None


NHQ_PANEL_KEYWORDS = [
    "Amber Straughn", "Dave Leckrone", "Hubble Space Telescope", "Jennifer Wiseman", "Julia Roman-Duval",
    "Ken Carpenter", "Mary W. Jackson NASA Headquarters Building", "Ray Villard", "Washington",
]
POWER_BEAMING_KEYWORDS = [
    "Power-Beaming", "power beaming", "experimental radio-controlled model aircraft", "spotlight", "solar panel",
    "Dave Bushman", "Tony Frackowiak", "Dale Reed", "Alex Sim", "Ryan Warner", "Derrick Barrett",
]


@pytest.mark.parametrize(
    ("data", "names", "expected"),
    [
        # 9 个关键词里 6 个是人名（67%）：排除
        ({"nasa_id": "x", "title": "Hubble", "keywords": NHQ_PANEL_KEYWORDS}, (6, 9), "person names 6/9"),
        # 模型飞机照片列出 6 位项目成员（55%）：不到 60%，不排除
        ({"nasa_id": "x", "title": "Power beaming", "keywords": POWER_BEAMING_KEYWORDS}, (6, 11), None),
        ({"nasa_id": "x", "title": "Galaxy", "keywords": ["Milky Way", "Spitzer Space Telescope", "New Horizons"]},
         (0, 3), None),
        # 摄影师本人不算；剩下 1 个人名不够
        ({"nasa_id": "x", "title": "Launch", "keywords": ["Bill Ingalls", "Jane Doe", "Soyuz"],
          "photographer": "Bill Ingalls"}, (1, 3), None),
    ],
)
def test_person_name_keywords(data, names, expected):
    assert nasa.person_name_keywords(data) == names
    assert nasa.identifiable_person_risk(data) == expected


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        ({"title": "NASA Insignia"}, "insignia"),
        ({"title": "Artemis II", "keywords": ["Mission patch"]}, "patch"),
        ({"title": "Climate infographic"}, "infographic"),
        ({"title": "COMING UP: Lucy Science Briefing"}, "coming up"),
        ({"title": "NASA DIRECT! Too Cool Pulsar"}, "nasa direct"),
        ({"title": "Fingerprints in the Light", "description": "This spectrum shows the light from a galaxy."},
         "this spectrum"),
        ({"title": "Blowin in the Stellar Wind", "description": "This image of the Elephant Trunk nebula shows dust."},
         None),
        ({"title": "Earth photographed from orbit", "description": "An astronaut photograph of clouds."}, None),
        ({"title": "Headquarters building", "description": "A close-up view of the NASA insignia at the entrance."},
         "insignia"),
        ({"title": "STS-70", "description": "Designed by the crew members, the STS-70 crew patch depicts Discovery."},
         "crew patch"),
        ({"title": "WISE", "description": "This image shows a patch of sky in the constellation Pisces."}, None),
        ({"title": "Nebula", "description": 'See <a href="https://example.org/logo.png">the page</a>.'}, None),
    ],
)
def test_text_graphic_risk(data, expected):
    assert nasa.text_graphic_risk(data) == expected


# ------------------------------------------------------------------ 来源标注


def test_credit_from_description_line_without_html():
    assert nasa.extract_credit(_data("GSFC_20171208_Archive_e000504")) == (
        "ESA/Hubble & NASA, Acknowledgement: Judy Schmidt"
    )
    assert nasa.extract_credit(_data("iss074e0472536")) == "NASA/Chris Williams"


@pytest.mark.parametrize(
    ("description", "expected"),
    [
        # 段落之间只剩两个空格：署名后的说明文字不再并入
        ("Barred Spiral Galaxy NGC 1300  Credit: NASA, ESA, and The Hubble Heritage Team (STScI/AURA) "
         "Acknowledgment: P. Knezek (WIYN)  The Hubble Space Telescope is a project of international "
         "cooperation between NASA and the European Space Agency.",
         "NASA, ESA, and The Hubble Heritage Team (STScI/AURA) Acknowledgment: P. Knezek (WIYN)"),
        # 姓名缩写的句号不是句子结束
        ("Credits: NASA, ESA, and D. Coe, J. Anderson, and R. van der Marel (STScI)  More info: Astronomers "
         "have uncovered a near-record breaking supermassive black hole.",
         "NASA, ESA, and D. Coe, J. Anderson, and R. van der Marel (STScI)"),
        ("Credit: NASA/JPL-Caltech. The image was taken in 2010 by the spacecraft.", "NASA/JPL-Caltech"),
        ("in Washington. Photo Credit: (NASA/Keegan Barber)", "NASA/Keegan Barber"),
        ("Image credit: (NASA) and (ESA)", "(NASA) and (ESA)"),
    ],
)
def test_credit_line_is_cut_at_sentence_end_and_unwrapped(description, expected):
    assert nasa.extract_credit({"description": description}) == expected


def test_credit_falls_back_to_metadata_secondary_creator_and_nasa():
    assert nasa.extract_credit({"description": ""}, {"XMP:Credit": "NASA/JPL-Caltech/UCLA"}) == "NASA/JPL-Caltech/UCLA"
    assert nasa.extract_credit({"secondary_creator": "NASA/JPL-Caltech/Cornell"}) == "NASA/JPL-Caltech/Cornell"
    assert nasa.extract_credit({"photographer": "NASA/Ben Smegelsky"}) == "NASA/Ben Smegelsky"
    assert nasa.extract_credit({}) == "NASA"


# ------------------------------------------------------------------ 相关度


def _score(term, data):
    item = MaterialInfo(provider="nasa", url="x", duration=5, source_info={"tags": nasa.relevance_tags(data)})
    ratio, hits = material._tag_relevance(term, item)
    return hits, len(material._relevance_words(term))


def test_all_star_target_field_does_not_match_star_field():
    assert _score("star field", _data("GSFC_20171208_Archive_e001027")) == (0, 2)


def test_institution_keyword_does_not_match_physics_laboratory():
    apl_video = {  # 测量中 "physics laboratory" 选中的 Van Allen Probes 访谈视频
        "title": "Orbiting in the Danger Zone - Behind the Scenes of the Van Allen Probes",
        "keywords": ["Van Allen Probes", "Johns Hopkins Applied Physics Laboratory", "APL"],
    }
    assert _score("physics laboratory", apl_video) == (0, 2)


@pytest.mark.parametrize(
    ("term", "data", "expected"),
    [
        ("elliptical galaxy", _data("GSFC_20171208_Archive_e000504"), (2, 2)),
        ("deep field galaxies", {"title": "Hubble Ultra Deep Field", "keywords": ["galaxies"]}, (3, 3)),
        ("magnetic field", {"title": "Earth's Magnetic Field", "keywords": []}, (2, 2)),
        ("space center", {"title": "Launch pad", "keywords": ["Kennedy Space Center", "launch"]}, (0, 2)),
        ("laser beam", {"title": "Lab test", "keywords": ["laser", "beam"]}, (2, 2)),
    ],
)
def test_relevance_tags_examples(term, data, expected):
    assert _score(term, data) == expected


def test_relevance_tags_treat_long_keywords_as_titles():
    tags = nasa.relevance_tags(_data("GSFC_20171208_Archive_e001027"))

    assert tags and all("Target Field" not in tag and "All-Star" not in tag for tag in tags)
    assert any("AllStar Game" in tag for tag in tags)


# ------------------------------------------------------------------ 图片处理


def _image(path, size):
    Image.new("RGB", size, (30, 60, 90)).save(path, "JPEG", quality=80)
    return str(path)


@pytest.mark.parametrize(
    ("size", "motion", "expected"),
    [
        ((8000, 6000), ImageMotion.zoom, (1242, 2208)),  # 缩小到 目标 × (1 + 3% × 5 秒)
        ((8000, 6000), ImageMotion.static, (1080, 1920)),
        ((1200, 2400), ImageMotion.zoom, (1200, 2133)),  # 比所需尺寸小：只裁剪，不放大
        ((2160, 3840), ImageMotion.zoom, (1242, 2208)),
    ],
)
def test_prepare_image_crops_to_aspect_and_never_upscales(tmp_path, size, motion, expected):
    prepared = nasa.prepare_image(_image(tmp_path / "a.jpg", size), PORTRAIT, 5, motion)

    with Image.open(prepared) as image:
        assert image.size == expected
    assert expected[0] <= nasa._center_crop_size(*size, PORTRAIT)[0]


@pytest.mark.parametrize("size", [(1000, 3000), (1600, 900)])
def test_prepare_image_rejects_images_below_the_output_resolution(tmp_path, size):
    assert nasa.prepare_image(_image(tmp_path / "a.jpg", size), PORTRAIT, 5) == ""


def test_save_image_clip_prepares_then_renders(tmp_path):
    source = _image(tmp_path / "nasa-x.jpg", (8000, 6000))
    item = MaterialInfo(provider="nasa", url="https://images-assets.nasa.gov/image/x/x~orig.jpg", duration=5)
    with (
        patch.object(nasa, "_download_image", return_value=source) as download,
        patch.object(nasa.video, "render_image_zoom_video", return_value=str(tmp_path / "clip.mp4")) as render,
    ):
        clip = nasa.save_image_clip(item, str(tmp_path), video_aspect=PORTRAIT, clip_duration=5,
                                    image_motion=ImageMotion.static)

    assert clip == str(tmp_path / "clip.mp4")
    download.assert_called_once_with(item.url, str(tmp_path))
    prepared, duration = render.call_args.args
    assert duration == 5 and render.call_args.kwargs == {"image_motion": ImageMotion.static}
    with Image.open(prepared) as image:
        assert image.size == (1080, 1920)


def test_save_image_clip_fails_softly(tmp_path):
    item = MaterialInfo(provider="nasa", url="https://x/x~orig.jpg", duration=5)
    with patch.object(nasa, "_download_image", return_value=""):
        assert nasa.save_image_clip(item, str(tmp_path), video_aspect=PORTRAIT, clip_duration=5) == ""


def test_prepare_image_reuses_the_prepared_file(tmp_path):
    source = _image(tmp_path / "a.jpg", (2160, 3840))
    first = nasa.prepare_image(source, PORTRAIT, 5)
    Path(first).write_bytes(b"cached")

    assert nasa.prepare_image(source, PORTRAIT, 5) == first
    assert Path(first).read_bytes() == b"cached"
    assert not list(tmp_path.glob("*.part"))


# ------------------------------------------------------------------ 人脸检查


def _drawn_face(path, size=(1242, 2208)):
    """画一张简化的人脸（不是真人照片），YuNet 能认出来。"""
    face = Image.new("L", (440, 440), 40)
    draw = ImageDraw.Draw(face)
    draw.ellipse([80, 40, 360, 400], fill=200)
    draw.ellipse([124, 150, 196, 190], fill=60)
    draw.ellipse([244, 150, 316, 190], fill=60)
    draw.rectangle([116, 120, 204, 132], fill=90)
    draw.rectangle([236, 120, 324, 132], fill=90)
    draw.polygon([(220, 190), (196, 270), (244, 270)], fill=170)
    draw.ellipse([170, 300, 270, 330], fill=90)
    image = Image.new("L", size, 40)
    image.paste(face.filter(ImageFilter.GaussianBlur(6)), ((size[0] - 440) // 2, (size[1] - 440) // 2))
    image.convert("RGB").save(path, "JPEG", quality=90)
    return str(path)


def _star_field(path, size=(1242, 2208)):
    """随机星点和几团模糊的星云，代表天文图片。"""
    rng = random.Random(7)
    nebula = Image.new("L", size, 5)
    draw = ImageDraw.Draw(nebula)
    for _ in range(6):
        x, y, r = rng.randrange(size[0]), rng.randrange(size[1]), rng.randrange(100, 400)
        draw.ellipse([x - r, y - r, x + r, y + r], fill=rng.randrange(40, 120))
    image = nebula.filter(ImageFilter.GaussianBlur(60))
    draw = ImageDraw.Draw(image)
    for _ in range(4000):
        x, y, r = rng.randrange(size[0]), rng.randrange(size[1]), rng.choice((1, 1, 1, 2, 4))
        draw.ellipse([x - r, y - r, x + r, y + r], fill=rng.randrange(120, 256))
    image.convert("RGB").save(path, "JPEG", quality=90)
    return str(path)


def test_count_faces_finds_a_drawn_face_and_not_a_star_field(tmp_path):
    pytest.importorskip("cv2")

    assert nasa.count_faces(_drawn_face(tmp_path / "face.jpg")) >= 1
    assert nasa.count_faces(_star_field(tmp_path / "stars.jpg")) == 0
    assert nasa.face_filter_active()


def test_face_filter_without_opencv_falls_back_to_text_filters(tmp_path, monkeypatch):
    monkeypatch.setattr(nasa, "_face_detector_state", {})
    monkeypatch.setitem(sys.modules, "cv2", None)  # import cv2 失败
    face = _drawn_face(tmp_path / "face.jpg")

    with patch.object(nasa.logger, "warning") as warning:
        assert nasa.count_faces(face) is None
        assert nasa.face_filter_active() is False
        assert nasa.count_faces(face) is None
    # 只警告一次，并说明怎样安装
    assert warning.call_count == 1 and "uv sync --extra face" in warning.call_args.args[0]

    item = MaterialInfo(provider="nasa", url="https://images-assets.nasa.gov/image/x/x~orig.jpg", duration=5)
    with (
        patch.object(nasa, "_download_image", return_value=_image(tmp_path / "nasa-x.jpg", (2160, 3840))),
        patch.object(nasa.video, "render_image_zoom_video", return_value="clip.mp4"),
    ):
        assert nasa.save_image_clip(item, str(tmp_path), video_aspect=PORTRAIT, clip_duration=5) == "clip.mp4"


def test_image_with_a_face_is_skipped_after_cropping(tmp_path):
    item = MaterialInfo(provider="nasa", url="https://x/x~orig.jpg", duration=5, source_info={"asset_id": "NHQ1"})
    source = _image(tmp_path / "nasa-x.jpg", (2160, 3840))
    with (
        patch.object(nasa, "_download_image", return_value=source),
        patch.object(nasa, "count_faces", return_value=2) as count_faces,
        patch.object(nasa.video, "render_image_zoom_video") as render,
    ):
        assert nasa.save_image_clip(item, str(tmp_path), video_aspect=PORTRAIT, clip_duration=5) == ""
        assert nasa.screen_image(item, str(tmp_path), video_aspect=PORTRAIT, clip_duration=5) is False
        with patch.dict(config.app, {"nasa_face_filter": False}):
            assert nasa.screen_image(item, str(tmp_path), video_aspect=PORTRAIT, clip_duration=5) is True

    render.assert_not_called()
    # 检查的是裁剪、缩小后的图片
    assert count_faces.call_args.args[0].endswith("-portrait-1242x2208.jpg")


def test_screen_image_rejects_images_that_cannot_be_prepared(tmp_path):
    item = MaterialInfo(provider="nasa", url="https://x/x~orig.jpg", duration=5)
    with patch.object(nasa, "_download_image", return_value=""):
        assert nasa.screen_image(item, str(tmp_path), video_aspect=PORTRAIT, clip_duration=5) is False


class _FakeResponse:
    def __init__(self, payload):
        self.payload, self.status_code = payload, 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def raise_for_status(self):
        pass

    def iter_content(self, chunk_size):
        yield self.payload


def test_download_image_publishes_a_verified_file_and_reuses_it(tmp_path):
    jpeg = tmp_path / "source.jpg"
    _image(jpeg, (640, 480))
    calls = []

    def fake_trace(method, function, url, **kwargs):
        calls.append(url)
        return _FakeResponse(jpeg.read_bytes())

    with patch.object(nasa, "trace_http_call", side_effect=fake_trace):
        first = nasa._download_image("https://images-assets.nasa.gov/image/x/x~orig.jpg", str(tmp_path))
        second = nasa._download_image("https://images-assets.nasa.gov/image/x/x~orig.jpg", str(tmp_path))

    assert first == second and os.path.basename(first).startswith("nasa-") and len(calls) == 1
    assert not [name for name in os.listdir(tmp_path) if name.startswith(".nasa-")]


# ------------------------------------------------------------------ 缓存与任务记录


def test_cache_keeps_credit_page_and_tags():
    items, _ = _search()
    material_cache.save_material_search_cache("nasa", "star field", 5, PORTRAIT, items, crop_to_aspect=True)

    cached = material_cache.load_material_search_cache("nasa", "star field", 5, PORTRAIT, crop_to_aspect=True)

    assert [c.url for c in cached] == [i.url for i in items]
    for original, restored in zip(items, cached):
        for field in ("credit", "source_page", "tags", "rendition", "ai_generated"):
            assert restored.source_info[field] == original.source_info[field]


def test_material_source_record_includes_credit_and_page():
    items, _ = _search()

    record = material._material_source_record(items[0], "/tmp/clip.mp4")

    assert record["credit"] == "NASA/JPL-Caltech/UCLA"
    assert record["source_page"] == "https://images.nasa.gov/details/PIA14103"
    assert record["provider"] == "nasa" and record["local_file"] == "clip.mp4"


# ------------------------------------------------------------------ 与下载流程的集成


def _nasa_item(term, name, tags, *, landscape=False):
    width, height = (7700, 6300) if landscape else (2160, 3840)
    return MaterialInfo(
        provider="nasa", url=f"https://images-assets.nasa.gov/image/{name}/{name}~orig.jpg", duration=5,
        source_info={"provider": "nasa", "search_term": term, "asset_id": name,
                     "source_page": f"https://images.nasa.gov/details/{name}", "credit": f"NASA/{name}",
                     "rendition": {"id": "orig", "width": width, "height": height},
                     "tags": tags, "ai_generated": False},
    )


def _run_nasa(results, audio_duration, *, faces=(), screened=None, **kwargs):
    """``faces``：画面里有人脸的图片；给出时人脸检查视为可用。``screened`` 收集被检查的图片。"""
    rendered, sources = [], []
    screened = [] if screened is None else screened

    def fake_search(search_term, minimum_duration, video_aspect, **options):
        return results[search_term]

    def fake_clip(item, save_dir="", **options):
        rendered.append((item.source_info["asset_id"], options))
        return f"/tmp/{item.source_info['asset_id']}.mp4"

    def fake_screen(item, save_dir="", **options):
        screened.append(item.source_info["asset_id"])
        return item.source_info["asset_id"] not in faces

    with (
        patch.dict(config.app, {"material_directory": ""}),
        patch.object(nasa, "search_images", side_effect=fake_search),
        patch.object(nasa, "save_image_clip", side_effect=fake_clip),
        patch.object(nasa, "face_filter_active", return_value=bool(faces)),
        patch.object(nasa, "screen_image", side_effect=fake_screen),
        patch.object(material, "save_video") as save_video,
        patch.object(material.material_cache, "load_material_search_cache", return_value=None),
        patch.object(material.material_cache, "save_material_search_cache"),
        patch.object(material.task_artifacts, "patch_script_data",
                     side_effect=lambda task_id, material_sources: sources.extend(material_sources) or True),
    ):
        paths = material.download_videos(
            task_id="nasa", search_terms=list(results), source="nasa", audio_duration=audio_duration,
            max_clip_duration=5, **kwargs,
        )
    save_video.assert_not_called()
    return [os.path.basename(p).removesuffix(".mp4") for p in paths], rendered, sources


def test_script_order_renders_nasa_images_with_spread_relevance_and_native_first():
    results = {
        "black hole": [_nasa_item("black hole", "bh-landscape", ["black hole"], landscape=True),
                       _nasa_item("black hole", "bh-off", ["galaxy"]),
                       _nasa_item("black hole", "bh", ["black hole"])],
        "nebula": [_nasa_item("nebula", "nebula", ["nebula"])],
        "star": [_nasa_item("star", "star", ["star"])],
    }

    paths, rendered, sources = _run_nasa(
        results, 9, match_script_order=True, crop_to_aspect=True, tag_relevance=MaterialTagRelevance.rank,
        image_motion=ImageMotion.static,
    )

    # 9 秒需要 2 段：等间距选第一个和最后一个关键词；相关度 > 同方向。
    assert paths == ["bh", "star"]
    assert rendered[0][1] == {"video_aspect": PORTRAIT, "clip_duration": 5, "image_motion": ImageMotion.static}
    assert [(s["credit"], s["source_page"]) for s in sources] == [
        ("NASA/bh", "https://images.nasa.gov/details/bh"), ("NASA/star", "https://images.nasa.gov/details/star"),
    ]


def test_script_order_replaces_an_image_with_a_face_by_the_same_terms_next_candidate():
    results = {
        "black hole": [_nasa_item("black hole", "bh", ["black hole"])],
        "nebula": [_nasa_item("nebula", "nebula-face", ["nebula"]), _nasa_item("nebula", "nebula", ["nebula"])],
        "star": [_nasa_item("star", "star", ["star"])],
    }

    paths, rendered, _ = _run_nasa(results, 14, faces={"nebula-face"}, match_script_order=True)

    # 被拒绝的图片换成同一关键词的下一个候选，片段仍在第二个位置
    assert paths == ["bh", "nebula", "star"]
    assert "nebula-face" not in [asset for asset, _ in rendered]


def test_script_order_skips_a_term_whose_candidates_all_show_faces():
    results = {
        "galaxy": [_nasa_item("galaxy", "g1", ["galaxy"]), _nasa_item("galaxy", "g2", ["galaxy"])],
        "telescope": [_nasa_item("telescope", "panel", ["telescope"])],
    }

    paths, _, _ = _run_nasa(results, 9, faces={"panel"}, match_script_order=True)

    assert paths == ["g1", "g2"]


def test_script_order_without_face_filter_does_not_screen():
    results = {"black hole": [_nasa_item("black hole", "bh", ["black hole"])]}
    screened = []

    paths, _, _ = _run_nasa(results, 4, screened=screened, match_script_order=True)

    assert paths == ["bh"] and screened == []


def test_pooled_mode_also_renders_nasa_images():
    results = {"black hole": [_nasa_item("black hole", "a", ["black hole"]), _nasa_item("black hole", "b", [])]}

    paths, _, _ = _run_nasa(results, 4, video_concat_mode=VideoConcatMode.sequential)

    assert paths == ["a"]


def test_download_without_save_material_still_uses_save_video():
    item = MaterialInfo(provider="pixabay", url="https://cdn.example/a.mp4", duration=5)
    with (
        patch.object(material, "_get_material_concurrency", return_value=1),
        patch.object(material, "save_video", return_value="/tmp/a.mp4") as save_video,
        patch.object(nasa, "save_image_clip") as save_clip,
    ):
        downloaded = material._download_materials_in_parallel([("t", item)], "")

    save_video.assert_called_once_with(video_url="https://cdn.example/a.mp4", save_dir="")
    save_clip.assert_not_called()
    assert downloaded == [("t", item, "/tmp/a.mp4")]


def test_pixabay_download_does_not_touch_nasa():
    item = MaterialInfo(provider="pixabay", url="https://cdn.example/a.mp4", duration=5,
                        source_info={"provider": "pixabay", "rendition": {"width": 1080, "height": 1920}})
    with (
        patch.dict(config.app, {"material_directory": ""}),
        patch.object(material, "search_videos_pixabay", return_value=[item]),
        patch.object(material, "save_video", return_value="/tmp/a.mp4"),
        patch.object(material.material_cache, "load_material_search_cache", return_value=None),
        patch.object(material.material_cache, "save_material_search_cache"),
        patch.object(material.task_artifacts, "patch_script_data", return_value=True),
        patch.object(nasa, "search_images") as nasa_search,
        patch.object(nasa, "save_image_clip") as nasa_clip,
    ):
        paths = material.download_videos(task_id="p", search_terms=["sky"], source="pixabay", audio_duration=4,
                                         max_clip_duration=5, match_script_order=True)

    assert paths == ["/tmp/a.mp4"]
    nasa_search.assert_not_called()
    nasa_clip.assert_not_called()


# ------------------------------------------------------------------ 各层


def test_layers_accept_nasa():
    assert "nasa" in tm._SUPPORTED_VIDEO_SOURCES
    assert "nasa" in cli._CLI_VIDEO_SOURCES
    assert "nasa" in mpt_agent.SUPPORTED_SOURCES
    assert {"nasa"} <= material.CROP_TO_ASPECT_SOURCES & material.MATERIAL_TAG_SOURCES
    assert VideoParams(video_subject="t", video_source="nasa").video_source == "nasa"
    assert TaskVideoRequest.model_validate({"video_subject": "t", "video_source": "nasa"}).video_source == "nasa"


def test_cli_flag_selects_nasa():
    with patch.dict(config.ui, {}, clear=True):
        params = cli.build_video_params(cli.parse_args(["--video-subject", "t", "--video-source", "nasa"]))

    assert params.video_source == "nasa"


def test_agent_needs_no_key_for_nasa(tmp_path):
    config_file = tmp_path / "config.toml"
    config_file.write_text('[app]\nllm_provider = "claude_code"\nvideo_source = "nasa"\n', encoding="utf-8")

    provider, missing = mpt_agent.missing_config(config_file, ["--video-subject", "t", "--video-source", "nasa"])

    assert missing == []


def test_example_config_documents_nasa():
    app = tomllib.loads((ROOT_DIR / "config.example.toml").read_text(encoding="utf-8"))["app"]

    assert app["nasa_media_types"] == ["image"]
    assert app["nasa_face_filter"] is True


def test_face_detection_is_an_optional_extra():
    project = tomllib.loads((ROOT_DIR / "pyproject.toml").read_text(encoding="utf-8"))["project"]

    # 默认依赖里没有 OpenCV；YuNet 模型随源码提供
    assert project["optional-dependencies"]["face"] == ["opencv-python-headless>=4.8"]
    assert not [dependency for dependency in project["dependencies"] if dependency.startswith("opencv")]
    assert os.path.getsize(nasa.FACE_MODEL_FILE) == 232589
