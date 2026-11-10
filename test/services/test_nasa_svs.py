"""NASA SVS (nasa_svs): search on recorded responses, data visualizations only (concept animations
and produced videos out), the media choice (native 9:16 first), the remote probe and its host
rule, the clean window and the silent cut, the scene label, credits, the cache and the chain.
No network."""

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT_DIR = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT_DIR))

from app.config import config  # noqa: E402
from app.models.schema import MaterialInfo, MaterialTagRelevance, VideoAspect  # noqa: E402
from app.services import (  # noqa: E402
    batch_package, batch_production, material, material_cache, nasa, nasa_svs, nasa_video, source_chain, task,
)
from app.utils import ffmpeg_inputs, utils  # noqa: E402

RESOURCES = ROOT_DIR / "test" / "resources" / "nasa_svs"
PORTRAIT, LANDSCAPE = VideoAspect.portrait, VideoAspect.landscape


def _page(page_id):
    return json.loads((RESOURCES / f"page_{page_id}.json").read_text(encoding="utf-8"))


def _fake_get(url, params=None, *, as_json=True):
    if url.endswith("/api/search/"):
        return json.loads((RESOURCES / "search_moon_far_side.json").read_text(encoding="utf-8"))
    if "/api/" in url:
        path = RESOURCES / f"page_{url.rstrip('/').rsplit('/', 1)[1]}.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    if not as_json:
        path = RESOURCES / f"page_{url.rstrip('/').rsplit('/', 1)[1]}_credit.html"
        return path.read_text(encoding="utf-8") if path.exists() else "<html>no credit here</html>"
    raise AssertionError(url)


def _probe(url):
    return (292.0, 1080, 1920) if "_v_" in url or "_v1_" in url else (61.0, 1920, 1080)


@pytest.fixture
def svs_api():
    with (patch.object(nasa_svs, "_get", side_effect=_fake_get),
          patch.object(nasa_svs, "probe", side_effect=_probe)):
        yield


# ---------------------------------------------------------------- search


def test_search_keeps_data_visualizations_only(svs_api):
    with patch.object(nasa_svs.task_materials, "source_filtered") as filtered:
        found = nasa_svs.search_videos("moon far side", 5)
    assert [item.source_info["asset_id"] for item in found] == ["4253", "5587"]
    skipped = filtered.call_args.args[2]
    assert skipped["type"] == 2  # two Produced Video results
    assert skipped["no_page"] == 2  # 5588 and 5415 have no recorded page
    info = found[0].source_info
    assert found[0].provider == "nasa_svs" and info["media_type"] == "video" and info["ai_generated"] is False
    assert info["source_page"] == "https://svs.gsfc.nasa.gov/4253/"
    assert info["credit"] == "NASA's Scientific Visualization Studio"  # from the page's HTML
    assert "LRO (Lunar Reconnaissance Orbiter)" in info["tags"] and info["tags"][-1].startswith("Moon Phase")
    assert info["scene_label"] == {"tr": "NASA SVS · LRO verisinden görselleştirme",
                                   "en": "NASA SVS · LRO data visualization"}
    assert info["description_tags"] and info["video"]["duration"] == 61.0


@pytest.mark.parametrize(("page_id", "reason"), [(20369, "type:Animation"), (11197, "type:Produced Video")])
def test_concept_animations_and_produced_videos_are_not_data(page_id, reason):
    assert nasa_svs.concept_reason(_page(page_id)) == reason


def test_a_visualization_that_calls_itself_a_concept_is_skipped():
    page = {**_page(4253), "description": "This artist's concept shows a future lunar base."}
    assert nasa_svs.concept_reason(page) == "concept:artist's concept"
    assert nasa_svs.concept_reason({**_page(4253), "studio": "cil"}) == "studio:cil"
    assert nasa_svs.concept_reason(_page(4253)) is None


# ---------------------------------------------------------------- media


def test_a_native_vertical_version_comes_first_for_portrait_videos():
    best, analysis = nasa_svs.choose_media(nasa_svs.movies(_page(5587)), PORTRAIT)
    assert (best["width"], best["height"]) == (1080, 1920) and "plain" in best["filename"]
    assert "fancy" not in best["filename"] and "YT" not in best["filename"]
    assert analysis["height"] > analysis["width"]  # the analysis copy has the same orientation


def test_landscape_videos_take_the_largest_clean_16_9_version():
    best, analysis = nasa_svs.choose_media(nasa_svs.movies(_page(5587)), LANDSCAPE)
    assert best["width"] > best["height"] and min(best["width"], best["height"]) >= 1080
    assert not nasa_svs._DIRTY.search(best["filename"])
    assert min(analysis["width"], analysis["height"]) <= min(best["width"], best["height"])


def test_the_analysis_copy_is_the_same_version_in_a_smaller_size():
    best, analysis = nasa_svs.choose_media(nasa_svs.movies(_page(3874)), PORTRAIT)
    assert best["filename"] == "tour_1080p60.mp4" and analysis["filename"] == "tour_360p30.mp4"
    assert nasa_svs._family("tour_nodata_720p30.mp4") != nasa_svs._family("tour_1080p60.mp4")


def test_graphic_music_and_master_versions_are_skipped():
    movies = [{"url": "https://svs.gsfc.nasa.gov/a/x_fancy_1080p30.mp4", "filename": "x_fancy_1080p30.mp4",
               "width": 1920, "height": 1080},
              {"url": "https://svs.gsfc.nasa.gov/a/xYT_1080p30.mp4", "filename": "phasesYT_1080p30.mp4",
               "width": 1920, "height": 1080},
              {"url": "https://svs.gsfc.nasa.gov/a/x_music_1080.mp4", "filename": "x_music_1080.mp4",
               "width": 1920, "height": 1080},
              {"url": "https://svs.gsfc.nasa.gov/a/x_360p.mp4", "filename": "x_360p.mp4", "width": 640, "height": 360}]
    assert nasa_svs.choose_media(movies, PORTRAIT) is None  # only a 360p clean version is left


def test_media_links_become_https_on_the_svs_host_only():
    assert nasa_svs.https("http://svs.gsfc.nasa.gov/vis/a000000/a004200/a004253/a b.mp4") == \
        "https://svs.gsfc.nasa.gov/vis/a000000/a004200/a004253/a%20b.mp4"
    assert nasa_svs.https("http://example.com/x.mp4") == ""


# ---------------------------------------------------------------- remote probe and protocols


def test_the_probe_reads_the_header_with_the_remote_rule():
    stderr = ("Input #0, mov,mp4,m4a,3gp,3g2,mj2, from 'https://svs.gsfc.nasa.gov/x.mp4':\n"
              "  Duration: 00:04:52.00, start: 0.000000, bitrate: 2293 kb/s\n"
              "  Stream #0:0[0x1](und): Video: h264 (High) (avc1 / 0x31637661), yuv420p(tv, bt709), "
              "1080x1920 [SAR 1:1 DAR 9:16], 2290 kb/s, 30 fps\n")
    with patch.object(nasa_svs.subprocess, "run",
                      return_value=subprocess.CompletedProcess([], 1, "", stderr)) as run:
        assert nasa_svs.probe("https://svs.gsfc.nasa.gov/x.mp4") == (292.0, 1080, 1920)
    command = run.call_args.args[0]
    i = command.index("-i")
    assert command[i - 4:i + 2] == ["-protocol_whitelist", "https,tls,tcp", "-f", "mov", "-i",
                                    "https://svs.gsfc.nasa.gov/x.mp4"]
    with pytest.raises(ValueError):
        nasa_svs.probe("https://example.com/x.mp4")


def test_svs_is_one_of_the_two_remote_hosts():
    assert nasa_video.REMOTE_HOSTS == ("images-assets.nasa.gov", "svs.gsfc.nasa.gov")
    assert ffmpeg_inputs.remote_input("https://svs.gsfc.nasa.gov/a.mp4", nasa_video.REMOTE_HOSTS)[-1] == \
        "https://svs.gsfc.nasa.gov/a.mp4"
    with pytest.raises(ValueError):
        ffmpeg_inputs.remote_input("https://svs.gsfc.nasa.gov.evil.com/a.mp4", nasa_video.REMOTE_HOSTS)


# ---------------------------------------------------------------- clean window and the cut


def _video(path, card_seconds=3, clean_seconds=7, audio=True):
    from PIL import Image, ImageDraw, ImageFont

    card = Image.new("RGB", (960, 540), (10, 10, 30))
    ImageDraw.Draw(card).text((120, 230), "MOON PHASE AND LIBRATION 2026",
                              font=ImageFont.truetype(str(ROOT_DIR / "resource" / "fonts" / "STHeitiMedium.ttc"), 48),
                              fill=(250, 250, 250))
    card.save(path.with_name("card.png"))
    globe = Image.new("RGB", (960, 540), (0, 0, 0))
    ImageDraw.Draw(globe).ellipse([330, 120, 630, 420], fill=(170, 170, 160))
    globe.save(path.with_name("globe.png"))
    command = [utils.get_ffmpeg_binary(), "-y", "-loglevel", "error",
               "-loop", "1", "-framerate", "15", "-t", str(card_seconds), "-i", str(path.with_name("card.png")),
               "-loop", "1", "-framerate", "15", "-t", str(clean_seconds), "-i", str(path.with_name("globe.png"))]
    graph = "[0:v][1:v]concat=n=2:v=1:a=0[v]"
    if audio:
        command += ["-f", "lavfi", "-t", str(card_seconds + clean_seconds), "-i", "sine=f=440"]
    command += ["-filter_complex", graph, "-map", "[v]", *(["-map", "2:a"] if audio else []),
                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-g", "15", str(path)]
    subprocess.run(command, check=True, capture_output=True)
    return path


def _svs_item(path, asset="5587"):
    return MaterialInfo(provider="nasa_svs", url=str(path), duration=10, source_info={
        "provider": "nasa_svs", "media_type": "video", "asset_id": asset, "search_term": "moon phases",
        "video": {"duration": 10.0, "width": 1920, "height": 1080,
                  "renditions": {"preview": str(path), "large": str(path), "orig": str(path)}},
        "scene_label": nasa_svs.scene_label(_page(5587)), "credit": "NASA's Scientific Visualization Studio",
    })


@pytest.fixture
def local_reads(tmp_path):
    with (patch.object(nasa_video.ffmpeg_inputs, "remote_input",
                       side_effect=lambda url, hosts: ffmpeg_inputs.ffmpeg_input(url)),
          patch.object(nasa_video, "_analysis_path", side_effect=lambda item, *args: str(
              tmp_path / f"analysis-{item.source_info['asset_id']}.json"))):
        yield


def test_the_title_card_is_passed_over_and_the_cut_is_silent(tmp_path, local_reads):
    pytest.importorskip("cv2")
    with patch.dict(config.app, {"nasa_face_filter": True, "nasa_text_filter": True}):
        if not nasa_video.detectors_ready():
            pytest.skip("face and text detectors are not installed")
        path = _video(tmp_path / "svs.mp4")
        item = _svs_item(path)
        assert nasa_video.is_video(item)
        clip = nasa_video.save_clip(item, str(tmp_path / "out"), clip_seconds=5, video_aspect=PORTRAIT,
                                    fit_mode="blur")
    segment = item.source_info["segment"]
    assert segment["start"] == pytest.approx(3.0, abs=0.1)  # after the title card
    assert segment["problem_samples"]["text"] >= 2
    info = subprocess.run([utils.get_ffmpeg_binary(), "-i", clip], capture_output=True, text=True).stderr
    assert "Video:" in info and "Audio:" not in info  # the source's (licensed) music never comes along


# ---------------------------------------------------------------- label, credits, records


def test_the_label_names_the_mission_or_falls_back():
    assert nasa_svs.mission_name(_page(5535)) == "LRO"  # an acronym before "Artemis Program"
    assert nasa_svs.scene_label({"missions": []}) == {"tr": "NASA SVS · veri görselleştirmesi",
                                                     "en": "NASA SVS · data visualization"}


def test_the_credit_falls_back_to_the_studio():
    with patch.object(nasa_svs, "_get", return_value="<html>nothing</html>"):
        assert nasa_svs.page_credit(5587, {"studio": "gms"}) == "NASA's Goddard Space Flight Center"
        assert nasa_svs.page_credit(5587, {}) == "NASA's Scientific Visualization Studio"
    assert nasa_svs.people(_page(5587))[0] == "Visualizer: Ernie Wright"


def test_credits_and_the_share_text_name_the_studio():
    record = {"provider": "nasa_svs", "asset_id": "5587", "source_page": "https://svs.gsfc.nasa.gov/5587/",
              "credit": "NASA's Scientific Visualization Studio", "media_type": "video",
              "creator": {"name": "Visualizer: Ernie Wright"}, "segment": {"start": 12.0, "duration": 5.0}}
    text = nasa.credits_text([record])
    assert "1. [NASA SVS] NASA's Scientific Visualization Studio (video, 12-17 s) — Visualizer: Ernie Wright" in text
    assert "https://svs.gsfc.nasa.gov/5587/" in text and nasa.SVS_CREDIT_LINE in text
    lines = batch_package.credit_lines([record, {"provider": "pixabay"}])
    assert batch_package.SVS_CREDIT in lines and batch_package.SVS_NOTE in lines
    assert batch_package.material_summary([record]) == {"NASA SVS": 1}


def test_the_task_record_keeps_the_svs_window_for_the_credits():
    """The record written at download keeps the clean window, so credits.txt names the range read."""
    item = _svs_item("x.mp4")
    item.source_info["segment"] = {"start": 4.0, "duration": 5.0, "rendition": "large", "window_bytes": 2_500_000,
                                   "analysis_bytes": 900_000}
    with patch.object(material.task_materials, "downloaded"):
        record = material._material_source_record(item, "C:/x/svs.mp4")
    assert record["media_type"] == "video"
    assert record["segment"]["start"] == 4.0 and record["segment"]["window_bytes"] == 2_500_000
    assert "[NASA SVS] NASA's Scientific Visualization Studio (video, 4-9 s)" in nasa.credits_text([record])


def test_the_label_comes_from_the_source_not_from_the_screening():
    """A NASA archive clip the screening calls a data visualization gets no label (the VIPER case)."""
    nasa_item = MaterialInfo(provider="nasa", url="https://images-assets.nasa.gov/x.mp4", duration=5, source_info={
        "provider": "nasa", "asset_id": "VIPER", "media_type": "video",
        "vision": {"decision": "use", "image_type": "data_visualization", "label": "data_visualization"}})
    svs_item = _svs_item("x.mp4")
    with patch.object(material.task_materials, "downloaded"):
        nasa_record = material._material_source_record(nasa_item, "C:/x/viper.mp4")
        svs_record = material._material_source_record(svs_item, "C:/x/svs.mp4")
    assert "scene_label" not in nasa_record and svs_record["scene_label"]["tr"].startswith("NASA SVS · LRO")
    labelled = {"svs.mp4": svs_record["scene_label"]}
    timeline = [{"source": "C:/c/viper.mp4", "start": 0.0, "end": 2.0}, {"source": "C:/c/svs.mp4", "start": 2.0, "end": 4.0}]
    assert [label["start"] for label in task.scene_labels_for(timeline, labelled, "tr-TR")] == [2.0]


def test_the_search_cache_keeps_the_label_and_the_copies():
    cached = material_cache._cached_source_info(_svs_item("x"))
    assert cached["scene_label"]["en"] == "NASA SVS · LRO data visualization"
    item = _svs_item("x")
    item.source_info["video"]["renditions"] = {"preview": "https://svs.gsfc.nasa.gov/a_360p.mp4",
                                               "large": "https://svs.gsfc.nasa.gov/a_1080p.mp4"}
    assert material_cache._cached_source_info(item)["video"]["renditions"]["large"].endswith("a_1080p.mp4")


# ---------------------------------------------------------------- the chain


def test_nasa_svs_is_a_keyless_tagged_stock_source():
    assert "nasa_svs" in source_chain.STOCK_SOURCES and "nasa_svs" in material.MATERIAL_TAG_SOURCES
    assert source_chain.validate("nasa", ["nasa_svs", "pixabay"]) == ["nasa_svs", "pixabay"]
    assert source_chain.has_api_key({}, "nasa_svs")
    assert "nasa_svs" in nasa.ATTRIBUTION_REQUIRED_SOURCES


def test_the_gercek_preset_puts_svs_first_in_the_chain():
    preset = batch_production.load_preset(ROOT_DIR / "presets" / "gercek.toml")
    assert preset.video["video_source"] == "nasa"
    assert preset.video["video_source_fallback"] == ["nasa_svs", "pixabay", "coverr"]
    uzay = batch_production.load_preset(ROOT_DIR / "presets" / "uzay.toml")
    assert uzay.video["video_source_fallback"] == ["pixabay", "coverr"]  # untouched


def test_download_cuts_svs_videos_instead_of_downloading_them():
    item = _svs_item("https://svs.gsfc.nasa.gov/a.mp4")
    with (patch.object(material, "resolve_fallback_chain", return_value=["nasa_svs"]),
          patch.object(material, "_download_videos_by_script_order", return_value=["a.mp4"]) as by_order,
          patch.object(material, "chain_source_search") as chain,
          patch.object(nasa_video, "save_clip", return_value="clip.mp4") as save_clip,
          patch.object(nasa_video, "screen", return_value=True),
          patch.object(material, "save_video") as save_video):
        chain.return_value = (lambda *a, **k: [], {"crop_to_aspect": False, "tag_relevance": MaterialTagRelevance.rank,
                                                   "exclude_ai_generated": False})
        material.download_videos("t", ["moon"], source="pixabay", match_script_order=True,
                                 tag_relevance=MaterialTagRelevance.rank, fallback_source="nasa_svs")
        save_material = by_order.call_args.kwargs["save_material"]
        assert save_material(item, "dir") == "clip.mp4"
        assert by_order.call_args.kwargs["screen_material"](item) is True
    save_clip.assert_called_once()
    save_video.assert_not_called()
    assert "nasa_svs" in chain.call_args.kwargs["screens"]


def test_a_rate_limited_svs_is_skipped_for_the_task():
    class Response:
        status_code = 429

    with (patch.object(nasa_svs.requests, "get", return_value=Response()),
          patch.object(source_chain, "note_rate_limited") as noted):
        assert nasa_svs._get("https://svs.gsfc.nasa.gov/api/search/") is None
    noted.assert_called_once_with("nasa_svs")
