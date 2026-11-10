"""NASA videos (nasa_video): search and filters on recorded responses, the clean window on
synthetic videos (title card, face, shot changes, black frames), the remote window command
and its protocol whitelist, type priority, records, cache, credits, the report and the
gercek preset. No network: remote inputs are pointed at local files."""

import random
import subprocess
import sys
from fractions import Fraction
from pathlib import Path
from unittest.mock import patch

import pytest
from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT_DIR = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT_DIR))

from app.config import config  # noqa: E402
from app.models.schema import MaterialInfo, MaterialTagRelevance, VideoAspect, VideoFitMode  # noqa: E402
from app.services import batch_package, batch_production, material, material_cache, nasa, nasa_video  # noqa: E402
from app.utils import ffmpeg_inputs, utils  # noqa: E402

PORTRAIT = VideoAspect.portrait
FONT = str(ROOT_DIR / "resource" / "fonts" / "STHeitiMedium.ttc")
ASSETS = "https://images-assets.nasa.gov/video"


# ---------------------------------------------------------------- recorded responses


def _search_item(nasa_id, title, keywords=(), description="Footage."):
    return {"data": [{"nasa_id": nasa_id, "media_type": "video", "title": title, "keywords": list(keywords),
                      "description": description}]}


def _asset(nasa_id, names=("orig", "large", "medium", "preview", "mobile")):
    base = f"http://images-assets.nasa.gov/video/{nasa_id}/{nasa_id}"
    hrefs = [f"{base}~{name}.mp4" for name in names] + [f"{base}.srt", f"{base}~small.jpg",
                                                         f"http://images-assets.nasa.gov/video/{nasa_id}/metadata.json"]
    return {"collection": {"items": [{"href": href} for href in hrefs]}}


def _metadata(duration="0:01:33", size="1920x1080", title="", credit=""):
    data = {"QuickTime:Duration": duration, "Composite:ImageSize": size, "AVAIL:Title": title}
    if credit:
        data["AVAIL:Photographer"] = credit
    return data


def _fake_api(search_items, assets, metadata):
    def get_json(url, params=None):
        if url.endswith("/search"):
            return {"collection": {"items": search_items, "metadata": {"total_hits": len(search_items)}}}
        if "/asset/" in url:
            return assets.get(url.rsplit("/", 1)[1].replace("%20", " "))
        if url.endswith("/metadata.json"):
            return metadata.get(url.split("/video/")[1].split("/")[0].replace("%20", " "))
        raise AssertionError(url)
    return get_json


@pytest.fixture
def video_types():
    with patch.dict(config.app, {"nasa_media_types": ["video", "image"]}):
        yield


def test_search_keeps_footage_and_skips_programmes_people_and_limits(video_types):
    items = [
        _search_item("moon1", "Lunar surface b-roll", ["Moon", "LRO"]),
        _search_item("expert1", "What is the Moon? | We Asked a NASA Expert"),
        _search_item("brief1", "Artemis Media Briefing"),
        _search_item("person1", "Astronaut Jane Doe on the Moon", ["Jane Doe"]),
        _search_item("long1", "Moon lecture footage"),
        _search_item("small1", "Moon flyover"),
        _search_item("hosted1", "Moon facts"),
        _search_item("short1", "Moon flash"),
    ]
    assets = {nasa_id: _asset(nasa_id) for nasa_id in ("moon1", "long1", "small1", "hosted1", "short1")}
    metadata = {
        "moon1": _metadata(),
        "long1": _metadata(duration="0:25:00"),
        "small1": _metadata(size="640x360"),
        "hosted1": _metadata(title="Moon facts | NASA Science Live"),
        "short1": _metadata(duration="0:00:03"),
    }
    with patch.object(nasa, "_get_json", side_effect=_fake_api(items, assets, metadata)):
        found = nasa_video.search_videos("lunar surface", 5)
    assert [item.source_info["asset_id"] for item in found] == ["moon1"]
    info = found[0].source_info
    assert info["media_type"] == "video" and info["ai_generated"] is False and found[0].provider == "nasa"
    assert info["video"]["duration"] == 93 and (info["video"]["width"], info["video"]["height"]) == (1920, 1080)
    assert set(info["video"]["renditions"]) == {"orig", "large", "medium", "preview", "mobile"}
    assert all(url.startswith("https://images-assets.nasa.gov/") for url in info["video"]["renditions"].values())
    assert info["rendition"] == {"id": "video", "width": 1920, "height": 1080}
    assert {"moon", "lro"} <= {word for tag in info["tags"] for word in material._relevance_words(tag)}
    assert info["source_page"] == "https://images.nasa.gov/details/moon1"


def test_search_counts_what_it_skipped(video_types):
    items = [_search_item("expert1", "Why the Moon? | We Asked a NASA Expert"),
             _search_item("ok1", "Moon"), _search_item("long1", "Moon")]
    assets = {"ok1": _asset("ok1"), "long1": _asset("long1")}
    metadata = {"ok1": _metadata(), "long1": _metadata(duration="1:02:03")}
    with (patch.object(nasa, "_get_json", side_effect=_fake_api(items, assets, metadata)),
          patch.object(nasa_video.task_materials, "source_filtered") as filtered):
        assert len(nasa_video.search_videos("moon", 5)) == 1
    assert filtered.call_args.args[2] == {"format": 1, "too_long": 1}


def test_video_search_needs_the_media_type():
    with (patch.dict(config.app, {"nasa_media_types": ["image"]}),
          patch.object(nasa, "_get_json") as get_json):
        assert nasa_video.search_videos("moon", 5) == []
    get_json.assert_not_called()


def test_media_types_keep_their_order_and_drop_duplicates():
    with patch.dict(config.app, {"nasa_media_types": ["Video", "image", "video", "audio"]}):
        assert nasa.configured_media_types() == ("video", "image")


def test_search_media_follows_the_type_order():
    video, image = MaterialInfo(provider="nasa"), MaterialInfo(provider="nasa")
    with (patch.object(nasa_video, "search_videos", return_value=[video]),
          patch.object(nasa, "search_images", return_value=[image])):
        with patch.dict(config.app, {"nasa_media_types": ["video", "image"]}):
            assert nasa_video.search_media("moon", 5) == [video, image]
        with patch.dict(config.app, {"nasa_media_types": ["image", "video"]}):
            assert nasa_video.search_media("moon", 5) == [image, video]


@pytest.mark.parametrize(("value", "seconds"), [("0:01:33", 93), ("1:02:03.5", 3723.5), ("12.5 s", 12.5),
                                                 (7, 7.0), ("", None), ("n/a", None), (True, None)])
def test_durations(value, seconds):
    assert nasa_video._seconds(value) == seconds


def test_video_info_falls_back_to_width_and_height():
    assert nasa_video.video_info({"QuickTime:MediaDuration": "0:00:20", "QuickTime:ImageWidth": 3840,
                                  "QuickTime:ImageHeight": 2160}) == (20.0, 3840, 2160)


def test_illustrations_are_skipped_only_when_asked():
    concept = {"title": "Kepler-22b", "keywords": ["exoplanet"],
               "description": "This artist's concept shows the planet orbiting its star."}
    animation = {"title": "Moon orbit animation", "keywords": [], "description": ""}
    footage = {"title": "Apollo 15 lunar rover", "keywords": [], "description": "Film of the rover on the Moon."}
    assert nasa.illustration_risk(concept) is None  # off by default
    with patch.dict(config.app, {"nasa_exclude_illustrations": True}):
        assert nasa.illustration_risk(concept) == "artist's concept"
        assert nasa.illustration_risk(animation) == "animation"
        assert nasa.illustration_risk(footage) is None
        assert nasa.search_cache_variant()["exclude_illustrations"] is True
    assert "exclude_illustrations" not in nasa.search_cache_variant()  # older cache keys stay valid


def test_an_animation_named_only_in_the_asset_id_or_metadata_is_skipped(video_types):
    # E2E 10.11: "PROMISE: Bringing Mars Rover Innovation to the Moon" said "animation" only in its id.
    teaser_id = "20261105 PROMISE Reveal - UHD-animation"
    items = [_search_item(teaser_id, "PROMISE: Bringing Mars Rover Innovation to the Moon", [],
                          "Meet PROMISE, a lunar rover."),
             _search_item("render1", "Lunar rover drive", ["rover"]), _search_item("real1", "Lunar rover drive")]
    assets = {key: _asset(key) for key in (teaser_id, "render1", "real1")}
    metadata = {teaser_id: _metadata(), "real1": _metadata(),
                "render1": {**_metadata(), "AVAIL:Description": "This rendering shows the rover."}}
    with patch.dict(config.app, {"nasa_exclude_illustrations": True}):
        assert nasa.illustration_risk({"nasa_id": "Moon_animation_01", "title": "Moon"}) == "animation"
        with (patch.object(nasa, "_get_json", side_effect=_fake_api(items, assets, metadata)),
              patch.object(nasa_video.task_materials, "source_filtered") as filtered):
            found = nasa_video.search_videos("lunar rover", 5)
    assert [item.source_info["asset_id"] for item in found] == ["real1"]
    assert filtered.call_args.args[2] == {"illustration": 2}
    assert nasa_video.blocked_format({"title": "Artemis III Teaser"}) == "teaser"


# ---------------------------------------------------------------- clean window (pure)


def _samples(problems_by_second):
    return [(float(second), list(problems)) for second, problems in enumerate(problems_by_second)]


def test_choose_window_takes_the_earliest_clean_stretch_of_one_shot():
    # 0-4 title card, 4-10 face, 10-20 clean; cuts at 4 and 10.
    samples = _samples([["text"]] * 4 + [["face"]] * 6 + [[]] * 10)
    result = nasa_video.choose_window(samples, [4.0, 10.0], 5, 20)
    assert result == {"start": 10.0, "duration": 5.0, "rejected": {"text": 1, "face": 1}, "cuts": 2}


def test_choose_window_skips_past_a_bad_sample_inside_a_shot():
    samples = _samples([[]] * 3 + [["face"]] + [[]] * 8)
    assert nasa_video.choose_window(samples, [], 5, 12)["start"] == 4.0


def test_choose_window_rejects_short_shots():
    samples = _samples([[]] * 12)
    result = nasa_video.choose_window(samples, [3.0, 6.0, 9.0], 5, 12)
    assert result["start"] is None and result["rejected"] == {"short_shot": 4}


def test_choose_window_without_a_clean_window():
    result = nasa_video.choose_window(_samples([["text"], [], [], ["text"], [], [], []]), [], 5, 7)
    assert result["start"] is None and result["rejected"] == {"text": 1}


def test_text_area_follows_what_the_fit_mode_shows():
    assert nasa_video.text_area("cover", PORTRAIT, 1920, 1080) == PORTRAIT  # the centre strip
    assert nasa_video.text_area("blur", PORTRAIT, 1920, 1080) is None  # the whole frame
    assert nasa_video.text_area("contain", PORTRAIT, 1920, 1080) is None
    assert nasa_video.text_area("cover", PORTRAIT, 1080, 1920) is None  # already vertical


@pytest.mark.parametrize(("size", "fit", "crop", "expected"), [
    ((1920, 1080), VideoFitMode.blur, True, "blur"),
    ((1280, 720), VideoFitMode.blur, True, "blur"),
    ((3840, 2160), VideoFitMode.blur, True, "cover"),  # the 9:16 crop needs no enlarging
    ((1920, 1080), VideoFitMode.contain, True, "cover"),
    ((1920, 1080), VideoFitMode.contain, False, "contain"),
    ((1920, 1080), "blur", False, "blur"),
])
def test_effective_fit_mode(size, fit, crop, expected):
    item = MaterialInfo(provider="nasa", source_info={"media_type": "video",
                                                      "video": {"width": size[0], "height": size[1]}})
    assert nasa_video.effective_fit_mode(item, PORTRAIT, fit, crop) == expected


# ---------------------------------------------------------------- clean window (synthetic videos)


def _stars(size=(960, 540), seed=3):
    rng = random.Random(seed)
    image = Image.new("L", size, 6)
    draw = ImageDraw.Draw(image)
    for _ in range(6):
        x, y, r = rng.randrange(size[0]), rng.randrange(size[1]), rng.randrange(40, 160)
        draw.ellipse([x - r, y - r, x + r, y + r], fill=rng.randrange(40, 110))
    image = image.filter(ImageFilter.GaussianBlur(30))
    draw = ImageDraw.Draw(image)
    for _ in range(1500):
        x, y, r = rng.randrange(size[0]), rng.randrange(size[1]), rng.choice((1, 1, 2, 3))
        draw.ellipse([x - r, y - r, x + r, y + r], fill=rng.randrange(140, 256))
    return image.convert("RGB")


def _card(text="THE FAR SIDE OF THE MOON", at=None, size=(960, 540)):
    image = _stars(size)
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype(FONT, 54)
    width = draw.textlength(text, font=font)
    draw.text(at or ((size[0] - width) // 2, size[1] // 2 - 30), text, font=font, fill=(245, 245, 245))
    return image


def _face(size=(960, 540)):
    face = Image.new("L", (300, 300), 40)
    draw = ImageDraw.Draw(face)
    draw.ellipse([55, 27, 245, 273], fill=200)
    draw.ellipse([85, 102, 134, 130], fill=60)
    draw.ellipse([166, 102, 215, 130], fill=60)
    draw.rectangle([79, 82, 139, 90], fill=90)
    draw.rectangle([161, 82, 221, 90], fill=90)
    draw.polygon([(150, 130), (134, 184), (166, 184)], fill=170)
    draw.ellipse([116, 205, 184, 225], fill=90)
    image = Image.new("L", size, 40)
    image.paste(face.filter(ImageFilter.GaussianBlur(4)), ((size[0] - 300) // 2, (size[1] - 300) // 2))
    return image.convert("RGB")


def _earth(size=(960, 540)):
    image = Image.new("RGB", size, (30, 90, 190))
    draw = ImageDraw.Draw(image)
    rng = random.Random(5)
    for _ in range(40):
        x, y, r = rng.randrange(size[0]), rng.randrange(size[1]), rng.randrange(20, 90)
        draw.ellipse([x - r, y - r, x + r, y + r], fill=(235, 240, 245))
    return image.filter(ImageFilter.GaussianBlur(6))


def _black(size=(960, 540)):
    return Image.new("RGB", size, (0, 0, 0))


def _video(path, segments, fps=15):
    """A video of still shots: [(seconds, PIL image), ...]."""
    command = [utils.get_ffmpeg_binary(), "-y", "-loglevel", "error"]
    for index, (seconds, image) in enumerate(segments):
        frame = path.with_name(f"{path.stem}-{index}.png")
        image.save(frame)
        command += ["-loop", "1", "-framerate", str(fps), "-t", str(seconds), "-i", str(frame)]
    graph = "".join(f"[{index}:v]" for index in range(len(segments))) + f"concat=n={len(segments)}:v=1:a=0[v]"
    command += ["-filter_complex", graph, "-map", "[v]", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-g", "15",
                str(path)]
    subprocess.run(command, check=True, capture_output=True)
    return path


@pytest.fixture
def detectors():
    pytest.importorskip("cv2")
    with patch.dict(config.app, {"nasa_face_filter": True, "nasa_text_filter": True}):
        if not nasa_video.detectors_ready():
            pytest.skip("face and text detectors are not installed (uv sync --extra face)")
        yield


@pytest.fixture
def local_reads(tmp_path):
    """Remote reads go to the local file named in place of the URL; the decisions are cached in tmp_path."""
    reads = []

    def remote(url, hosts):
        reads.append(url)
        return ffmpeg_inputs.ffmpeg_input(url)

    with (patch.object(nasa_video.ffmpeg_inputs, "remote_input", side_effect=remote),
          patch.object(nasa_video, "_analysis_path", side_effect=lambda item, *args: str(
              tmp_path / f"analysis-{item.source_info['asset_id']}-{args[0]}-{args[1]}.json"))):
        yield reads


def _video_item(path, asset="synthetic", width=1920, height=1080, duration=20.0):
    return MaterialInfo(provider="nasa", url=str(path), duration=int(duration), source_info={
        "provider": "nasa", "media_type": "video", "asset_id": asset, "search_term": "moon",
        "video": {"duration": duration, "width": width, "height": height,
                  "renditions": {"preview": str(path), "large": str(path), "orig": str(path)}},
    })


def test_title_card_and_face_are_passed_over(tmp_path, detectors, local_reads):
    path = _video(tmp_path / "card-face-stars.mp4", [(4, _card()), (6, _face()), (8, _stars())])
    item = _video_item(path, "card-face", duration=18)
    assert nasa_video.screen(item, clip_seconds=5, video_aspect=PORTRAIT, fit_mode="blur")
    segment = item.source_info["segment"]
    assert segment["start"] == pytest.approx(10.0, abs=0.1)
    assert segment["rejected"] == {"text": 1, "face": 1} and segment["cuts"] == 2
    assert segment["analysis_rendition"] == "preview" and segment["fit"] == "blur"


def test_black_frames_are_not_a_clean_window(tmp_path, detectors, local_reads):
    path = _video(tmp_path / "black-stars.mp4", [(3, _black()), (8, _stars())])
    item = _video_item(path, "black", duration=11)
    assert nasa_video.screen(item, clip_seconds=5, video_aspect=PORTRAIT, fit_mode="blur")
    assert item.source_info["segment"]["start"] == pytest.approx(3.0, abs=0.1)


def test_shot_changes_too_close_together_reject_the_video(tmp_path, detectors, local_reads):
    path = _video(tmp_path / "cuts.mp4", [(3, _stars()), (3, _earth()), (3, _stars(seed=9))])
    item = _video_item(path, "cuts", duration=9)
    with patch.object(nasa_video.task_materials, "rejected") as rejected:
        assert not nasa_video.screen(item, clip_seconds=5, video_aspect=PORTRAIT, fit_mode="blur")
    segment = item.source_info["segment"]
    assert segment["start"] is None and segment["rejected"] == {"short_shot": 3} and segment["cuts"] == 2
    assert rejected.call_args.args[1] == "clean_window"


def test_text_outside_the_cover_strip_matters_only_when_the_whole_frame_shows(tmp_path, detectors, local_reads):
    path = _video(tmp_path / "edge-text.mp4", [(7, _card("NASA", at=(30, 40)))])
    cover = _video_item(path, "edge-cover", duration=7)
    blur = _video_item(path, "edge-blur", duration=7)
    assert nasa_video.screen(cover, clip_seconds=5, video_aspect=PORTRAIT, fit_mode="cover")
    assert not nasa_video.screen(blur, clip_seconds=5, video_aspect=PORTRAIT, fit_mode="blur")
    assert blur.source_info["segment"]["rejected"] == {"text": 1}


def test_chunks_stop_early_and_see_a_cut_at_their_boundary(tmp_path, detectors, local_reads):
    path = _video(tmp_path / "chunks.mp4", [(4, _card()), (16, _earth())])
    item = _video_item(path, "chunks", duration=20)
    with patch.object(nasa_video, "CHUNK_SECONDS", 4):
        assert nasa_video.screen(item, clip_seconds=5, video_aspect=PORTRAIT, fit_mode="blur")
    segment = item.source_info["segment"]
    assert segment["start"] == pytest.approx(4.0, abs=0.1) and segment["cuts"] == 1
    assert segment["analysed_seconds"] < 20  # stopped once a window was found
    assert len(local_reads) == 3  # 0-4, 3-8, 7-12


def test_the_decision_is_cached(tmp_path, detectors, local_reads):
    path = _video(tmp_path / "cached.mp4", [(7, _stars())])
    item = _video_item(path, "cached", duration=7)
    assert nasa_video.screen(item, clip_seconds=5, video_aspect=PORTRAIT, fit_mode="blur")
    reads = len(local_reads)
    assert nasa_video.screen(_video_item(path, "cached", duration=7), clip_seconds=5, video_aspect=PORTRAIT,
                             fit_mode="blur")
    assert len(local_reads) == reads


def test_save_clip_cuts_the_window_from_the_right_copy(tmp_path, detectors, local_reads):
    path = _video(tmp_path / "window.mp4", [(3, _card()), (7, _stars())])
    item = _video_item(path, "window", duration=10)
    clip = nasa_video.save_clip(item, str(tmp_path / "out"), clip_seconds=5, video_aspect=PORTRAIT, fit_mode="blur")
    assert clip and Path(clip).is_file()
    duration = float(subprocess.run([utils.get_ffmpeg_binary(), "-i", clip], capture_output=True, text=True)
                     .stderr.split("Duration: ")[1].split(",")[0].split(":")[-1])
    assert duration == pytest.approx(5.0, abs=0.1)
    assert item.source_info["segment"]["rendition"] == "large"


@pytest.mark.parametrize(("size", "fit", "rendition"), [
    ((1920, 1080), "blur", "large"), ((1920, 1080), "cover", "large"),  # same pixels, a third of the data
    ((3840, 2160), "cover", "orig"), ((3840, 2160), "blur", "large"), ((4000, 3000), "cover", "orig"),
])
def test_the_copy_depends_on_the_fit_and_the_frame_size(size, fit, rendition):
    item = _video_item("x", "copy", *size)
    item.source_info["video"]["renditions"] = {name: f"{ASSETS}/a/a~{name}.mp4" for name in ("orig", "large", "medium")}
    item.source_info["segment"] = {"start": 2.0}
    with (patch.object(nasa_video, "screen", return_value=True),
          patch.object(nasa_video, "fetch_window", return_value=1234) as fetch):
        nasa_video.save_clip(item, "", clip_seconds=5, video_aspect=PORTRAIT, fit_mode=fit)
    assert fetch.call_args.args[0].endswith(f"~{rendition}.mp4") and fetch.call_args.args[1:3] == (2.0, 5.0)
    assert fetch.call_args.kwargs["max_width"] == (None if fit == "cover" else 1920)
    assert item.source_info["segment"]["window_bytes"] == 1234


def test_without_detectors_videos_are_skipped():
    item = _video_item("x")
    with (patch.object(nasa_video, "detectors_ready", return_value=False),
          patch.object(nasa_video, "analyse") as analyse):
        assert not nasa_video.screen(item, clip_seconds=5, video_aspect=PORTRAIT, fit_mode="blur")
    analyse.assert_not_called()


# ---------------------------------------------------------------- remote window command


def test_remote_input_allows_https_on_the_asset_host_only():
    url = f"{ASSETS}/Moon%20Clip/Moon%20Clip~large.mp4"
    assert ffmpeg_inputs.remote_input(url, ("images-assets.nasa.gov",)) == [
        "-protocol_whitelist", "https,tls,tcp", "-f", "mov", "-i", url]
    for bad in ("http://images-assets.nasa.gov/video/a/a~large.mp4", "https://example.com/a.mp4",
                "https://user:pw@images-assets.nasa.gov/a.mp4", "https://images-assets.nasa.gov:8443/a.mp4",
                "file:///C:/a.mp4", "C:/a.mp4", ""):
        with pytest.raises(ValueError):
            ffmpeg_inputs.remote_input(bad, ("images-assets.nasa.gov",))


def test_the_window_command_seeks_before_the_remote_input():
    url = f"{ASSETS}/a/a~orig.mp4"
    command = nasa_video.window_command(url, 12.5, 5, "out.mp4")
    i = command.index("-i")
    assert command[i - 4:i + 2] == ["-protocol_whitelist", "https,tls,tcp", "-f", "mov", "-i", url]
    assert command[command.index("-ss") + 1] == "12.500" and command[command.index("-t") + 1] == "5.000"
    assert command.index("-ss") < i and command.index("-t") < i  # input seeking: only the window is read
    assert "-an" in command and command[command.index("-map") + 1] == "0:v:0"
    assert command[command.index("-loglevel") + 1] == "verbose"  # the bytes read are reported
    assert command.count("-i") == 1


def test_the_remote_whitelist_refuses_local_files(tmp_path):
    """The remote options open neither files nor plain http: a playlist cannot pull in local or other inputs."""
    clip = _video(tmp_path / "local.mp4", [(1, _stars())])
    result = subprocess.run([utils.get_ffmpeg_binary(), "-hide_banner", "-protocol_whitelist", "https,tls,tcp",
                             "-f", "mov", "-i", str(clip), "-f", "null", "-"], capture_output=True, text=True)
    assert result.returncode != 0 and "not on whitelist" in result.stderr


def test_bytes_read_from_ffmpeg_statistics():
    stderr = ("[AVIOContext @ 1] Statistics: 5555597 bytes written, 2 seeks, 25 writeouts\n"
              "[AVIOContext @ 2] Statistics: 7178135 bytes read, 1 seeks\n")
    assert nasa_video.bytes_read(stderr) == 7178135


def test_the_application_has_no_other_remote_input():
    sources = [path for path in (ROOT_DIR / "app").rglob("*.py") if "REMOTE_PROTOCOLS" in path.read_text("utf-8")]
    assert [path.relative_to(ROOT_DIR).as_posix() for path in sources] == ["app/utils/ffmpeg_inputs.py"]


# ---------------------------------------------------------------- type priority and the material layer


def _candidate(asset, media_type, tags, provider="nasa"):
    info = {"provider": provider, "asset_id": asset, "search_term": "lunar surface", "tags": tags,
            "ai_generated": False, "rendition": {"width": 1920, "height": 1080}}
    if media_type == "video":
        info.update(media_type="video", video={"width": 1920, "height": 1080, "renditions": {}})
    return MaterialInfo(provider=provider, url=f"https://x/{asset}", duration=10, source_info=info)


def _order(items, relevance=MaterialTagRelevance.rank):
    return [item.source_info["asset_id"] for item in
            material._order_material_candidates(items, PORTRAIT, tag_relevance=relevance)]


def test_suitable_videos_come_before_suitable_images():
    image_full = _candidate("img-full", "image", ["lunar surface"])
    video_half = _candidate("vid-half", "video", ["lunar"])
    video_none = _candidate("vid-none", "video", ["mars"])
    with patch.dict(config.app, {"nasa_media_types": ["video", "image"]}):
        assert _order([image_full, video_none, video_half]) == ["vid-half", "img-full", "vid-none"]
        assert _order([image_full, video_none, video_half], MaterialTagRelevance.off)[:2] == ["vid-none", "vid-half"]
    with patch.dict(config.app, {"nasa_media_types": ["image", "video"]}):
        assert _order([image_full, video_none, video_half]) == ["img-full", "vid-half", "vid-none"]


def test_other_sources_keep_their_order():
    items = [_candidate("p-half", "image", ["lunar"], "pixabay"), _candidate("p-full", "image",
                                                                              ["lunar surface"], "pixabay")]
    assert _order(items) == ["p-full", "p-half"]


def test_videos_pass_the_orientation_check_of_the_cache():
    video = _candidate("vid", "video", ["moon"])
    landscape_image = _candidate("img", "image", ["moon"], "pixabay")
    kept = material._filter_materials_by_aspect([video, landscape_image], PORTRAIT, crop_to_aspect=True)
    assert kept == [video]


def test_the_source_record_keeps_the_type_and_the_window():
    item = _candidate("vid", "video", ["moon"])
    item.source_info.update(credit="NASA", segment={
        "start": 12.0, "duration": 5.0, "rejected": {"text": 1, "face": 2, "x" * 50: True}, "cuts": 3,
        "rendition": "large", "analysis_rendition": "preview", "window_bytes": 7178135, "analysis_bytes": 2600000,
        "fit": "blur", "problem_samples": {"text": 4}, "path": "C:/Users/someone/secret.mp4"})
    with patch.object(material.task_materials, "downloaded"):
        record = material._material_source_record(item, "C:/tmp/nasa-video-abc.mp4")
    assert record["media_type"] == "video" and record["local_file"] == "nasa-video-abc.mp4"
    assert record["segment"] == {"start": 12.0, "duration": 5.0, "cuts": 3, "analysis_bytes": 2600000,
                                 "window_bytes": 7178135, "rendition": "large", "analysis_rendition": "preview",
                                 "fit": "blur", "rejected": {"text": 1, "face": 2}, "problem_samples": {"text": 4}}


def test_the_search_cache_keeps_public_video_copies_only():
    item = _candidate("vid", "video", ["moon"])
    item.source_info["video"] = {"duration": 93.0, "width": 1920, "height": 1080, "renditions": {
        "large": f"{ASSETS}/a/a~large.mp4?token=1", "orig": "http://images-assets.nasa.gov/a~orig.mp4",
        "evil": f"{ASSETS}/a/a~evil.mp4"}}
    cached = material_cache._cached_source_info(item)
    assert cached["media_type"] == "video"
    assert cached["video"] == {"renditions": {"large": f"{ASSETS}/a/a~large.mp4"}, "duration": 93.0,
                               "width": 1920, "height": 1080}


# ---------------------------------------------------------------- credits, report, preset


def test_credits_name_the_window_of_a_video():
    sources = [{"provider": "nasa", "asset_id": "moon1", "credit": "NASA/Goddard", "media_type": "video",
                "duration": 5, "segment": {"start": 12.0, "duration": 5.0}},
               {"provider": "nasa", "asset_id": "PIA1", "credit": "NASA/JPL"}]
    text = nasa.credits_text(sources)
    assert "1. [NASA] NASA/Goddard (video, 12-17 s)" in text and "https://images.nasa.gov/details/moon1" in text
    assert "2. [NASA] NASA/JPL" in text and "NASA/JPL (video" not in text  # images unchanged


def test_the_report_counts_videos_and_their_filters():
    lines = [
        "nasa image search: term='moon', total_hits=9, results=9, checked=5, kept=0, skipped={'person': 2}",
        "nasa video search: term='moon', total_hits=9, results=9, checked=5, kept=3, "
        "skipped={'format': 4, 'illustration': 1, 'credit': 2}",
        "nasa video filter: skipped moon1, no clean 5s window ({'text': 3})",
    ]
    summary = batch_package.log_summary(lines, ["moon"])
    assert summary["bos_terimler"] == []  # three videos, no image: the term was not empty
    eliminations = summary["filtre_elemeleri"]
    assert (eliminations["kisi"], eliminations["program"], eliminations["cizim"], eliminations["kredi"],
            eliminations["temiz_bolum"]) == (2, 4, 1, 2, 1)
    sources = [{"provider": "nasa", "media_type": "video"}, {"provider": "nasa"}, {"provider": "nasa",
               "media_type": "video"}, {"provider": "pixabay"}]
    assert batch_package.material_summary(sources) == {"NASA video": 2, "NASA görsel": 1, "Pixabay": 1}


def test_the_gercek_preset():
    preset = batch_production.load_preset(ROOT_DIR / "presets" / "gercek.toml")
    assert preset.video["video_source"] == "nasa"
    assert preset.video["video_source_fallback"] == ["pixabay", "coverr"]
    assert preset.video["exclude_ai_generated_materials"] is True
    assert preset.video["video_fit_mode"] == "blur" and preset.video["crop_materials_to_aspect"] is True
    assert preset.app["nasa_media_types"] == ["video", "image"]
    assert preset.app["nasa_exclude_illustrations"] is True and preset.app["nasa_face_filter"] is True
    assert preset.video.get("nasa_text_filter", True) is True
    argv = batch_production.video_option_argv(preset.video)
    assert argv[argv.index("--video-fit-mode") + 1] == "blur"


def test_the_description_score_reaches_videos_too():
    item = _candidate("vid", "video", ["iss042e277408"])
    item.source_info["description_tags"] = ["A tortilla floats inside the station."]
    assert material._tag_relevance("tortilla", item) == (Fraction(1, 2), 1)
