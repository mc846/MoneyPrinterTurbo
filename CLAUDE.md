# CLAUDE.md

MoneyPrinterTurbo, bir konu ya da anahtar kelimeden kendiliğinden kısa video üretir: metni LLM yazar, görüntüleri stok sitelerden ya da AI'dan alır, üstüne TTS seslendirme, altyazı ve arka plan müziği ekler. Bu dizin `mc846` fork'u; geliştirme Windows'ta, uv ile yerelde yapılır.

## Mimari

**Giriş noktaları**
- `main.py`: FastAPI uygulamasını (`app.asgi:app`) uvicorn ile `listen_host:listen_port` adresinde başlatır.
- `webui/Main.py`: Streamlit WebUI (tek dosya, ~9,5 bin satır). API'yi kullanmaz, görevi aynı süreçte `app/services/webui_task.py` → `task.start` üzerinden çalıştırır. Metinler `tr("key")` ile `webui/i18n/*.json` dosyalarından gelir.
- `cli.py`: tarayıcısız üretim yolu, `run_cli` → `build_video_params` → `task.start`. `--stop-at` şu aşamalardan biri olabilir: script, terms, audio, subtitle, materials, video. `--batch-file` JSON ya da JSONL alır. `--video-source` verilmezse `[app].video_source` kullanılır, o da yoksa `pexels`.
- `app/launcher.py`: pip ile kurulan `mpt` komutunun girişi (`mpt webui`, `mpt cli`, `mpt batch`).
- `batch.py`: toplu üretim (kişisel iş akışı; `app/services/batch_production.py` ve `batch_package.py`), bkz. "Toplu üretim" bölümü. WebUI'da üst çubuktaki "Toplu Üretim" (`webui/batch_page.py`, `app/services/batch_runner.py`).

**API katmanı**
- `app/asgi.py`: uygulama kurulumu, CORS (`CORS_ALLOWED_ORIGINS`), `app.api_key` doğrulaması, görev dosyalarının korunması.
- `app/router.py` iki yönlendiriciyi bağlar:
  - `app/controllers/ping.py` → `/ping`
  - `app/controllers/v1/` (`base.new_router` ile `/api/v1` öneki):
    - `video.py`: `POST /videos`, `/subtitle`, `/audio` görev oluşturur; `GET/DELETE /tasks/{id}`, `/musics`, `/video_materials`, `/stream/…`, `/download/…`
    - `llm.py`: `POST /scripts`, `/terms`, `/social-metadata`
- `app/controllers/manager/`: görev kuyruğu. Varsayılan `InMemoryTaskManager`; `app.enable_redis` açıksa `RedisTaskManager`.
- `app/models/schema.py`: Pydantic modelleri. Ana model `VideoParams`; API, WebUI ve CLI aynı modeli kullanır. LLM sağlayıcıları `app/models/llm_provider.py` içindeki `LLM_PROVIDER_REGISTRY`'de tanımlı.

**Video görevinin akışı**: `app/services/task.py::start` → `_run_pipeline`. Görev durumu `app/services/state.py`'de (bellekte ya da Redis'te) tutulur, çıktılar `storage/tasks/<task_id>/` altına yazılır.
1. Metin: `llm.generate_script`. `script_accuracy_rules` açıkken sistem prompt'una üç bilimsel doğruluk kuralı eklenir (`llm._script_system_prompt`).
2. Arama kelimeleri: `llm.generate_terms` (`video_source=local` ise atlanır). İkisi birlikte `script.json`'a kaydedilir. `video_terms_style = "visual"` iken `llm.generate_visual_terms` çağrılır; terimlerle birlikte yer/özne çapaları (`video_anchors`) döner ve `params`'a yazılır.
3. Ses: `voice.tts` → `audio.mp3`. Sağlayıcıyı ses adı belirler (`voice._single_tts` içindeki `is_<x>_voice` dalları). Edge (Azure V1) anahtarsız çalışır.
4. Altyazı: `task.generate_subtitle` → `subtitle.srt`. `app.subtitle_provider` iki değer alabilir: `edge` zaman damgalarını TTS'den alır (`voice.create_subtitle`); `whisper` faster-whisper ile üretir (`subtitle.create`), ardından metne göre düzeltir (`subtitle.correct`).
5. Materyal: `material.download_videos`. Kaynaklar `task._SUPPORTED_VIDEO_SOURCES`'ta listeli: pexels, pixabay, coverr, local, wavespeed, volcengine_seedance, ofox, metaso_minimax, muapi, loomloom, openai_image, codex_image, infistar, jalapeno, nasa. Önbellek `material_cache.py`'de. `crop_materials_to_aspect` açıkken Pexels, Pixabay ve NASA için yön filtresi yerine `material._center_crop_size` kuralı kullanılır; kırpılan adaylar yerel yöndekilerden sonra gelir. `material_tag_relevance` ve `exclude_ai_generated_materials` adayları `material._order_material_candidates` içinde sıralar ve süzer (Pixabay ve NASA; etiket taşıyan kaynaklar `material.MATERIAL_TAG_SOURCES`'ta). NASA görselleri indirilip `nasa.save_image_clip` ile doğrudan video boyutunda yakınlaşan klibe çevrilir (`download_videos`'taki `save_material` kancası; aynı görsel ve ayarlarla üretilmiş klip yeniden kullanılır, bkz. Planlanan iyileştirmeler'deki hızlandırmalar); diğer kaynaklar `save_video` ile iner. `nasa_face_filter` açıksa ve `face` extra'sı kuruluysa kırpılmış görselde yüz aranır, bulunmazsa orijinalde kırpma kenarına giren yüzler de aranır (`nasa.count_edge_faces`). `nasa_text_filter` (varsayılan açık) indirmeden önce sıralanmış adayların `~small` önizlemesinde kırpma bölgesine giren yazıyı arar (`nasa.screen_text`, `material._stock_source_search`'te sıralamadan sonra). Sıra koruma kipinde `screen_material` kancası reddedilen görseli aynı terimin sıradaki adayıyla aynı yerde değiştirir. `video_source_fallback` (görev başına, varsayılan boş) ikinci bir stok kaynağıdır: `material._search_with_fallback` terim başına birincilin uygun adayı azsa o terimi yedekte arar ve terimin bütün adaylarını tek kaynaktan alır (ayrıntı Planlanan iyileştirmeler'de).
6. Video: `task.generate_final_videos`. `video.combine_videos` klipleri `combined-N.mp4` olarak birleştirir (boyutlandırılacak klipler ffmpeg ile normalleştirilir, uygun kliplerde akış kopyalanır), `video.generate_video` ses, altyazı ve müziği (`bgm.py`) ekleyip `final-N.mp4` üretir; `final_render_engine = "ffmpeg"` iken katmanları ffmpeg yerleştirir, desteklenmeyen içerikte ya da hatada MoviePy kullanılır. NASA görseli kullanıldıysa ve `nasa_credit_overlay` açıksa son 2 saniyede küçük bir kaynak satırı (`video.credit_overlay_clip`) eklenir; NASA görseli kullanıldıysa görev klasörüne `credits.txt` malzeme aşamasının sonunda yazılır (her satırda `[NASA]` / `[Pixabay]` kaynak etiketi).
7. İsteğe bağlı paylaşım: `upload_post.py`, yalnızca `auto_upload` açıksa.

**Servisler** (`app/services/`)
- LLM: `llm.py`, `_generate_response` sağlayıcıya göre dallanır (43 sağlayıcı). OAuth kullananlar: `chatgpt_*`, `codex_*`, `kimi_code_oauth.py`. `claude_code` sağlayıcısı `claude` CLI'ını çağırır.
- TTS: `voice.py`. Sağlayıcılar: edge, azure v2, siliconflow, gemini, mimo, minimax, elevenlabs, chatterbox, kokoro, fish_audio, voxcpm ve sessiz kip.
- Materyal: `material.py` stok aramasını ve AI görsellerini yapar. Her ücretli sağlayıcının ayrı modülü var: `muapi.py`, `ofox.py`, `metaso_minimax.py`, `volcengine_seedance.py`, `infistar_video.py`, `jalapeno_video.py`, `loomloom.py`, `codex_image.py`, `twelvelabs.py`. Anahtarsız NASA görsel kaynağı `nasa.py`'de; isteğe bağlı yüz kontrolünün YuNet modeli (MIT lisanslı) `app/services/face_detection/` altında. Kullanıcı yüklemeleri `material_upload.py`'de.
- Altyazı: `subtitle.py`, `app/utils/subtitle_reader.py` ve `subtitle_writer.py`, `app/services/utils/subtitle_shaping.py` (Arapça gibi yazılar için şekillendirme).
- Video: `video.py` (birleştirme, altyazı çizimi, efektler için `utils/video_effects.py`). `video_project.py` ayrı ve isteğe bağlı bir sahne revizyonu CLI'ıdır (`docs/video-projects.md`); PATH'te ffmpeg ve ffprobe ister.
- Müzik: `bgm.py` (`resource/songs` içindeki yerel dosyalar), `elevenlabs_music.py`, `sonilo.py`.

**Config yükleme** (`app/config/config.py`)
- Kök dizini `app/paths.py::runtime_root()` belirler. Kaynak klonunda bu proje dizinidir; pip kurulumunda `%LOCALAPPDATA%` olur ve `MPT_HOME` ile değiştirilebilir. `config.toml` yoksa `config.example.toml`'dan kopyalanır.
- Dosya import sırasında bir kez okunur ve modül düzeyindeki nesnelere dağıtılır: `config.app`, `config.ui`, `config.azure`, `config.whisper`, `config.proxy`, …, ayrıca `listen_host` ve `listen_port`. WebUI ayarları `config.toml`'a geri yazar.
- Ortam değişkeniyle geçersiz kılınabilen değerler yalnızca şunlar: `REDIS_HOST`, `IMAGEIO_FFMPEG_EXE`, `CORS_ALLOWED_ORIGINS`, `MPT_HOME`.

**Docker**
- `Dockerfile` + `docker-compose.yml`: python:3.11-slim, apt ile ffmpeg, `requirements.txt`. Kaynaktan derlenir ve bütün proje dizinini bağlar.
- `docker-compose.release.yml`: hazır imaj `ghcr.io/harry0703/moneyprinterturbo:latest`; yalnızca `config.toml` ve `storage/` bağlanır.
- `Dockerfile.gpu` + `docker-compose.gpu.yml`: NVIDIA GPU sürümü.
- `Dockerfile.claude` + `docker-compose.claude.yml`: release imajına Node 22 ve `@anthropic-ai/claude-code` CLI'ını ekler. Böylece `claude_code` LLM sağlayıcısı API anahtarı yerine Claude aboneliğini kullanabilir. Proje dizininin tamamı bağlanır ve `CLAUDE_CODE_OAUTH_TOKEN` (`claude setup-token` ile alınır) zorunludur; değer `.env` dosyasında durur, o da gitignore'da.

## Komutlar (Windows, proje kökünde)

uv PATH'te değilse tam yolunu kullan: `$env:LOCALAPPDATA\Microsoft\WinGet\Links\uv.exe`. gh'ın yolu `C:\Program Files\GitHub CLI\gh.exe`.

```powershell
uv sync --frozen                                       # bağımlılıklar (.venv, dev grubu dahil)
uv sync --frozen --extra face                          # + NASA yüz kontrolü (opencv-python-headless)
uv run python main.py                                  # API: http://127.0.0.1:8080/docs
.\webui.bat                                            # WebUI: http://127.0.0.1:8501
uv run python cli.py --video-subject "Konu"            # CLI ile video
uv run python cli.py --help
uv run python cli.py --batch-file .\tasks.json --stop-at video
uv run python batch.py .\konular.csv --preset presets\uzay.toml   # toplu üretim + yükleme paketleri + rapor.html
uv run python batch.py durum <klasör>                              # iki aşamalı akış: onay bekleyenler, denetim
uv run python batch.py render <klasör>                             # iki aşamalı akış: yalnız onaylıların videosu
uv run python -X utf8 -m pytest -q test                # bütün testler
uv run python -X utf8 -m pytest -q test/services/test_video.py::TestVideoService
$env:MPT_RUN_SLOW_TESTS="1"; uv run python -X utf8 -m pytest -q test/services/test_ffmpeg_final_render.py   # ffmpeg son render'ın tam stil matrisi (~2,5 dk)
uv run ruff check app cli.py main.py webui test docs/skill   # CI'daki lint
```

- `uv sync` tam eşitleme yapar: `--extra face` verilmezse OpenCV kaldırılır ve NASA yüz kontrolü devre dışı kalır (bir kez uyarı loglanır, metin filtreleri çalışmaya devam eder). Yerel `.venv`'de `opencv-python-headless==4.14.0.94` kurulu.
- `webui.bat` önce `.venv\Scripts\python.exe -m streamlit` dener. Health ucu `/_stcore/health` ve `ok` döner. Tarayıcının açılmasını istemiyorsan `$env:STREAMLIT_SERVER_HEADLESS="true"` ver.
- Claude Code kabuğunda `NoDefaultCurrentDirectoryInExePath=1` tanımlı olduğu için `cmd /c webui.bat` dosyayı bulamaz. Tam yolla çalıştır: `cmd /c C:\Repom\MoneyPrinterTurbo\webui.bat`.

## Ortam notları

- Python 3.11'i uv yönetir (`.python-version`, `%APPDATA%\uv\python`). Sistemdeki Python 3.14 (`C:\Python314`) kullanılmaz. `python` ya da `pip`'i doğrudan çağırma, her şeyi `uv run` ile çalıştır.
- ffmpeg 09.11'den beri gyan.dev 9.0.2: kullanıcı düzeyindeki `IMAGEIO_FFMPEG_EXE` = `C:\Tools\ffmpeg\9.0.2\bin\ffmpeg.exe` (yanında ffprobe). Değişken yoksa `imageio-ffmpeg` paketindeki 7.1 kullanılır (`.venv\Lib\site-packages\imageio_ffmpeg\binaries\`); PATH'te ffmpeg ve ffprobe yok, bu yüzden `video_project.py` çalışmaz. Claude Code kabuğu değişkeni görmez: testleri ve CLI'ı 9.0 ile çalıştırmak için `IMAGEIO_FFMPEG_EXE='C:\Tools\ffmpeg\9.0.2\bin\ffmpeg.exe'` ver, 7.1 için `env -u IMAGEIO_FFMPEG_EXE`. Doğrulama, geri dönüş ve 9.0 farkları: bkz. **Upstream güncellemesi > ffmpeg**.
- `config.toml` gitignore'da ve API anahtarları orada durur. Yeni ya da değişen her ayar `config.example.toml`'a da işlenmeli.
- **Uçtan uca test kuralı:** `config.toml`'daki LLM sağlayıcısına güvenme; kullanıcı onu WebUI'dan değiştirir (04.11'den beri `ollama` / `gpt-oss:120b-cloud`). Sağlayıcıyı her CLI koşusunda `--llm-provider` (gerekirse `--llm-model`) ile ver, ör. `--llm-provider claude_code`. Bayraklar yalnız o süreçte geçerli, `config.toml`'a yazılmaz. Claude Code içinden `claude_code` sağlayıcısını kullanırken `CLAUDECODE` ve `CLAUDE_*` ortam değişkenlerini temizle.
- **Eski WebUI süreci config yazabilir.** Açık kalan WebUI süreci ayarları bellekte tutar ve ana sayfa her yeniden çalıştığında `config.toml`'a yazar. Kod güncellendikten sonra yeniden başlatılmazsa eski seçenek listeleriyle yazabilir. 05.11'de `config.toml` 19:40:47 ve 20:04:17'de değişti: render motoru `moviepy`, eşzamanlılık 1, ses `af-ZA-AdriNeural-Female`, yazı tipi `BeVietnamPro-Bold.ttf`; ayrıca `subtitle_enabled = false`, `voice_rate = 0.8`, `subtitle_position = "top"`, `font_size = 30`. O sırada çalışan tek süreç, 04.11 15:42'den beri açık WebUI'ydı. **`config.toml`'u elle ya da betikle düzeltmeden önce WebUI'yı durdur, sonra yeniden başlat:** açık bir WebUI belleğindeki eski değerleri ilk yeniden çalıştırmada geri yazar.
- **config.toml düzeltmesi (05.11 21:36, kullanıcı onaylı):**
  - Yedek: `storage/config-yedek/config.toml.20261105-2136.bak`.
  - Uzay reçetesine göre yazılan anahtarlar:
    - `[app]`: `video_source_fallback = "pixabay"`, `video_source_fallback_min_relevance = 0.5`, `video_source_fallback_min_candidates = 1`, `crop_materials_to_aspect = true`, `material_tag_relevance = "rank"`, `match_materials_to_script = true`, `video_terms_style = "visual"`, `script_accuracy_rules = true`, `nasa_credit_overlay = true`, `nasa_face_filter = true`, `nasa_text_filter = true`, `final_render_engine = "ffmpeg"`, `material_concurrency = 3`, `video_clip_concurrency = 3`.
    - `[ui]`: `voice_name = "tr-TR-EmelNeural-Female"`, `font_name = "STHeitiMedium.ttc"`, `stroke_width = 3.0`, `video_clip_duration = 5`, `video_language = "tr-TR"`.
    - `llm_provider`, `video_source` ve `exclude_ai_generated_materials` zaten doğruydu.
  - Yazım biçimi: WebUI'nin kodlayıcısıyla (`_ConfigTomlEncoder`, metin kipinde CRLF) yazıldı; WebUI'nin `save_config`'i aynı içeriği üretir ve dosyayı değiştirmez. Değişmedi: `listen_host`, API anahtarları ve geri kalan her anahtar (anlamsal fark doğrulandı).
  - Onaylı listede olmayan `subtitle_enabled`, `voice_rate`, `subtitle_position` ve `font_size` değerlerine dokunulmadı. Reçete bunları kendisi sabitliyor (bkz. Toplu üretim > Reçete); tek video için WebUI'dan düzeltilmeleri gerekebilir.
- `listen_host` yerelde `127.0.0.1`. Docker'daki API container'ı (`main.py`) aynı `config.toml`'u okur ve container içinde `0.0.0.0` dinlemesi gerekir; değer `127.0.0.1` kalırsa host'tan erişilemez. WebUI container'ı etkilenmez, çünkü adresini komut satırından (`--server.address=0.0.0.0`) alır. Bu değer için henüz ortam değişkeni yok; ileride eklenecek.

## Portlar

Yerel çalıştırma ve Docker aynı portları kullanır: WebUI 8501, API 8080 (Docker'da 127.0.0.1'e eşlenir). İkisini aynı anda çalıştırma. Yerel API 8080'i bağlayamaz; `webui.bat` ise 8501 doluysa hiçbir şey demeden 8502–8599 arasında boş bir porta geçer. Önce `docker compose -f docker-compose.release.yml down` çalıştır.

## Test baz durumu

04.11.2026, Windows 11, Python 3.11.18. Redis yok, API anahtarı yok, PATH'te ffmpeg yok. `pytest -q test` 12–23 dakika sürer.
**4242 geçti, 1 kaldı, 244 atlandı** (09.11, iki aşamalı toplu üretimden ve taslak düzeltmesinden sonra, ffmpeg 9.0.2 ile — `IMAGEIO_FFMPEG_EXE` verilerek —, `main` 410b96b, 15 dk 18 sn; 17 667 alt test; 63 yeni test: `test_batch_review.py` 47, `test_batch_review_webui.py` 14, taslağı aynı anda yazan iki oturum 1, yeniden başlatılan WebUI'nın günlük satırı 1). Önceki: 4179 geçti, 1 kaldı, 244 atlandı (09.11, ffmpeg 9.0.2 ile, `main` cdff882, 14 dk 36 sn; 15 915 alt test; 34 yeni test: ffmpeg sürüm günlüğü 9, protocol whitelist 13, main komutları 10, toplu üretim 2). Paketteki 7.1 ile iki DASH testi ve gerçek ffprobe testi atlanır. `MPT_RUN_SLOW_TESTS=1` matrisi 9.0 ile 122 geçti. Önceki: 4145 geçti, 1 kaldı, 244 atlandı (09.11, v1.3.10 yükseltmesinden sonra, `main` 8f45aab, 19 dk; ayrıca 15 915 alt test geçti; upstream'in 17 yeni testi ve `-nostdin` ek düzeltmesinin 2 testi dahil; upstream bf33010 bilinen 3 hatanın 2'sini düzeltti). Daha önceki baz: 4124 geçti, 3 kaldı, 244 atlandı (05.11, toplu üretim altyazılarından sonra, `main` 35895be, 13 dk 46 sn; ayrıca 15 908 alt test geçti; düzeltmelerin, kırpma, etiket uygunluğu, görsel arama terimi, doğruluk kuralı, eşit aralıklı terim seçimi, NASA kaynağı, noktalama normalleştirmesi, CLI sağlayıcı bayraklarının, yedek kaynağın, yüz kenarı kuralının, metin filtresinin, üretim süresi hızlandırmalarının, ffmpeg son render'ının, toplu üretimin, WebUI toplu üretiminin ve toplu üretim altyazılarının 1025 yeni testi dahil; atlananlara `MPT_RUN_SLOW_TESTS=1` ile açılan 96 matris testi eklendi). NASA testleri ağa çıkmaz; `test/resources/nasa/` altındaki kayıtlı API yanıtlarını kullanır. Yüz ve metin testleri çizilmiş bir yüz, çizilmiş yazı ve yapay bir yıldız alanı üretir (gerçek kişi fotoğrafı yok); `face` extra'sı kurulu değilse OpenCV isteyen 7'si atlanır. CI tam takımı yalnız Linux'ta çalıştırır; Windows'ta yalnız 4 dosyalık bir duman testi var, o yüzden aşağıdaki hatalar CI'da görünmez.

Aşağıdaki testlerin hiçbiri bir özelliğin bozulduğunu göstermiyor; nedenleri Windows ya da ortam. İkinci ve üçüncüsü v1.3.10'daki upstream bf33010 ile düzeldi; v1.3.10'dan beri her turda kalan yalnız ilki:
- `test_image_sanitize_identity.py::test_long_source_name_renders_without_exceeding_filesystem_name_limit`: tam yol 260 karakteri aşıyor ve `LongPathsEnabled=0` (MAX_PATH sınırı).
- `test_material.py::TestMaterialTlsVerification::test_metadata_probe_failure_preserves_existing_cache`: test yolun `\` ile kurulmasını bekliyordu, kod `/` kullanıyor (v1.3.10'da düzeldi).
- `test_utils_ffmpeg_check.py::TestGetFfmpegBinaryEnvExpansion::test_normalizes_home_path_in_os_environ`: test `HOME`'u değiştiriyordu, oysa Windows'ta `expanduser` `USERPROFILE`'ı okur (v1.3.10'da düzeldi).
- `test_material_cache_duration.py::test_legacy_finite_float_duration_remains_usable`: aralıklı kalıyor, tek başına çalıştırılınca 300 denemenin 24'ünde. Windows'ta yeni yazılan bir dosyanın `mtime` değeri `time.time()`'dan ileride olabiliyor (ölçümde 2000 yazmanın 227'sinde). O zaman `cache_age < 0` çıkar ve önbellek geçersiz sayılır. Bu yüzden toplam sonuç v1.3.10'dan beri 4144–4145 geçti, 1–2 kaldı arasında değişir (önce 4123–4124 / 3–4; 04.11'deki iki tam turun birinde kaldı, tek başına geçti).
- `test_webui_startup.py::TestWebuiStartup::test_external_directory_prefers_project_app_package` tam turun yükü altında bazen alt süreç zaman aşımına (60 sn) takılıyor; tek başına 9–14 sn'de geçiyor.

Atlananların dağılımı: 96 ffmpeg son render'ın tam stil matrisi (`MPT_RUN_SLOW_TESTS=1` ile çalışır; varsayılan turda her boyutu kapsayan 12 çekirdek durum çalışıyor), 91 native ffmpeg/ffprobe ister, 38 Redis ister (`MPT_TEST_REDIS_HOST`), 8 Kokoro test sunucusu ister, 7 canlı sağlayıcı ister (`MPT_RUN_INTEGRATION_TESTS=1`), 3 `twelvelabs` extra'sı kurulu olmadığı için, 1 Windows'ta geçersiz bir yol yüzünden atlanıyor.

Testler `test/conftest.py` fixture'ı sayesinde izole çalışır; gerçek `config.toml`'a dokunmaz. `pytest_configure`, toplama aşamasından önce `MPT_HOME`'u pytest'in geçici dizinine yönlendirir (`%TEMP%\pytest-of-<kullanıcı>\pytest-N\mpt-home`) ve oraya `config.example.toml`'un bir kopyasını koyar. `storage/` ve `resource/` da bu dizine gider; kaynaklar ilk kullanımda kopyalanır, oturum başına yaklaşık 198 MB. Alt süreçler `MPT_HOME`'u ortamdan devralır. `isolated_runtime_home` adlı session fixture'ı, `app.config` yönlendirmeden önce yüklendiyse çalıştırmayı durdurur. `no_nasa_preview_downloads` fixture'ı her testte `nasa_text_filter`'ı kapatır, çünkü açıkken NASA önizlemeleri indirilir; filtreyi deneyen testler (`test_nasa_text_filter.py`) ayarı kendileri açar.

## Git akışı

- `origin` = https://github.com/mc846/MoneyPrinterTurbo (fork), `upstream` = https://github.com/harry0703/MoneyPrinterTurbo. `main` dalı `origin/main`'i izler.
- Her özellik ayrı bir dalda geliştirilir: `git switch -c feat/<ad> main`, ardından `git push -u origin feat/<ad>`.
- **upstream'e asla push edilmez.** upstream'in push adresi `DISABLE` yapıldı (`git remote set-url --push upstream DISABLE`), bu yüzden `git push upstream` hata verir.
- upstream güncellemeleri ayrı bir `upgrade/<sürüm>` dalında, yayın etiketinden merge ile alınır; yöntem, çakışma ilkesi ve doğrulama listesi için bkz. **Upstream güncellemesi**.
- Commit mesajları upstream'in biçimini izler: `fix(webui): …`, `feat(tts): …`, `docs: …`, `test: …`.
- upstream `.gitignore`'u `CLAUDE.md`'yi yok sayar; fork bu dosyayı `git add -f` ile izliyor. upstream'e PR açılacaksa dalı `upstream/main`'den aç ki bu commit PR'a girmesin.
- **PR yığını** (upstream'de bu sırayla birleşmeli): `upstream/main` → `fix/test-config-isolation` → `feat/cross-orientation-crop` → `feat/material-relevance` → `feat/visual-search-terms`. Yığının bütün dalları `test/conftest.py`'yi içerir. `feat/script-accuracy-rules` de `fix/test-config-isolation`'ın üzerinde ama yığından bağımsız: upstream'de isolation'dan sonra herhangi bir sırada birleşebilir. `fix/script-order-dedup` ve `fix/cli-video-source-default` `upstream/main` tabanlı ve bağımsızdır; bu ikisinde `test/conftest.py` yoktur.
  - `fix/normalize-unicode-punctuation` ve `feat/cli-llm-override` `fix/test-config-isolation`'ın üzerinde ve yığından bağımsız; isolation'dan sonra herhangi bir sırada birleşebilir.
  - `feat/source-fallback`, `feat/nasa-source`'un üzerinde (dolayısıyla onun bütün bağımlılıklarına bağlı); upstream'de nasa-source'tan sonra birleşmeli. `credits.txt` biçimini (`nasa.credits_text`) ve `download_videos`'un kaynak seçimini değiştirir.
  - `fix/nasa-face-edge` `feat/source-fallback`'in, `feat/nasa-text-filter` de `fix/nasa-face-edge`'in üzerinde; upstream'de bu sırayla, source-fallback'ten sonra birleşmeli. Metin filtresi dalı 2,4 MB'lık ikili bir model (`app/services/text_detection/`, Apache-2.0) ve `test/conftest.py`'ye bir fixture ekler; `FILTER_VERSION` 4 olur.
  - Hızlandırma dalları (05.11). Genel olan üçü `fix/test-config-isolation`'ın üzerinde, birbirinden ve yığından bağımsız; isolation'dan sonra herhangi bir sırada birleşebilir: `perf/image-clip-reuse` (yakınlaştırma klibi yeniden kullanımı ve `output_size`; yerel görselleri ve OpenAI görsellerini de etkiler), `perf/ffmpeg-auto-threads` (`n_threads` varsayılanı), `perf/cached-video-probe` (önbellekteki stok videonun ölçümü). NASA'ya özgü ikisi `feat/nasa-text-filter`'ın üzerinde: `perf/nasa-text-cache` bağımsız; `perf/nasa-clip-size`'a `output_size` parametresi için `perf/image-clip-reuse` merge edildi, upstream'de ikisinden sonra birleşmeli. Çakışmayı önlemek için ölçüm fonksiyonu `material.py` yerine (import bloğunu main'deki dallar değiştirdi) `material_upload.py`'ye kondu.
  - Son render ve temizlik dalları (05.11). `fix/test-config-isolation`'ın üzerinde, bağımsız: `fix/webui-concurrency-options` (eşzamanlılık seçeneklerine 3; main'de NASA'nın kaynak listesiyle bitişik satırda küçük bir çakışma çözüldü, upstream'de çakışmaz), `fix/cache-cleanup-image-clips` (önbellek temizliğine yakınlaştırma klipleri ve temizlenmiş kopyalar), `perf/ffmpeg-final-render` (ffmpeg son render; NASA'ya bağlı değil). `perf/ffmpeg-concat-normalize` `feat/cross-orientation-crop`'un üzerinde (ffmpeg normalleştirmesi `_clip_fit_mode`'u ve `crop_cross_orientation_clips`'i kullanır), upstream'de crop dalından sonra. NASA'ya özgü: `fix/nasa-cache-cleanup` `perf/nasa-text-cache`'in üzerinde ve içine `fix/cache-cleanup-image-clips` merge edildi; `perf/nasa-ffmpeg-final-render` `feat/nasa-text-filter`'ın üzerinde ve içine `perf/ffmpeg-final-render` merge edildi (yalnız atıf katmanının eşdeğerlik testi; ffmpeg yolu atıfı bileşimden okuduğu için ayrı kod gerekmedi). İki ffmpeg dalı aynı `compute_position` import satırını ekliyor; birleştirmede aynı değişiklik olarak çakışmadan giriyor.
  - Son adım (05.11) mevcut iki dalın devamı olarak, yeni dal açılmadan: `perf/image-clip-reuse`'a 1bd0f29 (yakınlaştırma klipleri B karesiz, `_IMAGE_CLIP_RENDER_VERSION = 3`), `perf/ffmpeg-concat-normalize`'a 37a002c (video boyutundaki bütün klibi olduğu gibi kullanma). İkisi birbirinden bağımsız; atlama kuralı kazancını B karesiz yakınlaştırma klipleriyle verir.
  - `fix/script-order-even-spread`, `feat/visual-search-terms`'in üzerinde ve içine `fix/script-order-dedup` merge edilmiş. Gerekçe: `_download_videos_by_script_order`'ın gövdesini yalnız dedup dalı değiştirdi; korunması gereken yerel yön ve uygunluk sıralaması ise yığından geliyor. Upstream'de hem yığından hem `fix/script-order-dedup`'tan sonra birleşmeli.
  - `feat/nasa-source`, `fix/script-order-even-spread`'in üzerinde (dolayısıyla bütün yığına ve dedup dalına bağlı). Üçüncü turda içine `feat/script-accuracy-rules` merge edildi (bdf6412): NASA'nın metin kuralı doğruluk kurallarıyla aynı listede numaralanıyor, bu yüzden upstream'de doğruluk kuralları dalından da sonra birleşmeli. Kırpma kuralını, `MATERIAL_TAG_SOURCES` uygunluk sıralamasını ve sıra koruma kipindeki eşit aralığı kullanır. Upstream'de even-spread'den sonra birleşmeli. İkinci commit'i (filtreler ve yüz kontrolü) `pyproject.toml`'a isteğe bağlı `face` extra'sını, `uv.lock`'a yalnız `opencv-python-headless` girdisini (25 satır, elle) ve 227 KB'lık ikili bir model dosyası ekler. Yerel uv 0.13 `uv add`/`uv lock` ile kilidin tamamını yeni biçimde (revision 5, `upload-time`) yeniden yazıyor; bu yüzden kilit elle düzenlendi ve `uv lock --check` ile doğrulandı. Upstream merge'de `uv.lock` çakışırsa çakışmayı çözüp `uv lock --check` çalıştır.
- `feat/batch-production` (05.11, `main` 03afbb9'dan): toplu üretim. Kişisel iş akışı; upstream'e PR hedeflenmiyor. Commit'ler 4382fb0 (özellik), d85818b (Ctrl+Break), 9cd46d0 (e2e sonrası: süre/kelime uyarı, reçete talimatı); main merge'leri 59821d1, f512d22, a9620a2.
- `feat/batch-subtitles` (05.11, `feat/batch-webui`'nin üzerinden): pakete `.srt` altyazılar ve çeviriler. Commit 8070a72; main merge 35895be.
- `feat/batch-webui` (05.11, `feat/batch-production`'ın üzerinden): WebUI'dan toplu üretim. Commit'ler b144039 (özellik), 9d6b80a (test yalıtımı), 1d657c7 (venv başlatıcısı kilidi, erken çıkış mesajı), 2e4185f (`state.json` değiştirme yeniden denemesi); main merge'leri e9d6ff5, 5739466, 469bf67, 04cd0fe.
- ffmpeg dalları (09.11), ikisi de `fix/test-config-isolation`'ın üzerinde ve birbirinden bağımsız; upstream'de isolation'dan sonra herhangi bir sırada birleşebilir:
  - `fix/ffmpeg-version-log` (ef5e497, 8dc9f22): WebUI, API ve CLI açılışta kullanılan ffmpeg'in yolunu, kaynağını ve sürüm satırını süreç başına bir kez yazar. main'e merge'te (dffc7a1) `cli.py`'de `apply_llm_overrides` ile çakıştı, ikisi de korundu; toplu işteki çağrı main'de (cdff882).
    - 8dc9f22: yeniden başlatılan WebUI satırı bir süreçte 3 kez yazdı. Streamlit'in kaynak izleyicisi app modüllerini yeniden import ediyor (Windows'taki sahte dosya olayları dahil, bkz. `Main.py`'deki not), bu da modül değişkenindeki bayrağı sıfırlıyordu. Bayrak ve kilidi artık `sys` modülünde (`_moneyprinterturbo_ffmpeg_version_log`).
    - WebUI satırı ilk tarayıcı oturumunda yazar (`Main.py` o zaman çalışır); açık bir sekme yeniden bağlanınca da olur. WebUI dosyaya günlük yazmadığı için doğrulamada konsol çıktısı `Tee-Object` ile dosyaya da alındı (scratchpad `webui-restart\webui_tee.ps1`; PowerShell 5.1 dosyayı UTF-16 yazar).
  - `fix/ffmpeg-protocol-whitelist` (83b307c, 388c0ff): bkz. **Upstream güncellemesi > ffmpeg girdi güvenliği**. main'e çakışmasız girdi.
  - main'e özgü takip commit'i cdff882: bizim komutlarımıza whitelist, ffmpeg 8+ için `-/filter_complex`, `batch.log`'a ffmpeg satırı.
- `fix/batch-draft-empty-voice` (09.11, `main`'den; kişisel): Toplu Üretim tablosunda boş ses hücresi boş görünür (65f9e37).
- `feat/batch-review` (09.11, `main`'den; kişisel, upstream PR'ı yok): iki aşamalı toplu üretim (d160392), e2e'de bulunan sağlayıcı ve denetim kaybı düzeltmesi (eab2a1b), taslak yarışı düzeltmesi (3624928: WebUI yeniden başlatılınca açık sekme ve yeni sekme taslağı aynı anda yazıyordu, ikisi de `.taslak.json.tmp`'yi kullandığı için biri FileNotFoundError alıyordu; artık her yazımın kendi geçici dosyası var); main merge'leri a52df58, b572fb7, 410b96b. Bkz. **Toplu üretim > İki aşamalı akış**.
- **Test kuralı:** `test/conftest.py` içermeyen bir dalda test çalıştırma; önce `ls test/conftest.py` ile bak. Böyle bir dalın değişiklikleri `main`'de olduğu için testleri orada çalıştır. conftest yoksa testler gerçek `config.toml`'u okur ya da bozar ve `storage/tasks`'a yazar.

## Upstream güncellemesi

Kalıcı yöntem (ilk kez v1.3.10'da, 09.11 uygulandı). upstream'e push ve PR yok.

1. **Hazırlık:** Çalışan WebUI'yı ve toplu işleri durdur; açık WebUI kod değişince `config.toml`'a eski değerleri yazabilir (bkz. Ortam notları). `config.toml`'un özetini al (`sha256sum config.toml`). Bundan sonraki adımların hiçbiri onu değiştirmemeli.
2. **Bilgi:** `git fetch upstream --tags`. Yerel sürüm dizesi (`app/__init__.py`) main'in gerçek tabanını göstermeyebilir: `git describe --tags $(git merge-base main upstream/main)`. Ardından `git rev-list --count <son alınan upstream commit'i>..<etiket>`, sürüm notu (`docs/release-notes-<sürüm>.md`) ve `git diff --stat`.
3. **Yedek etiket:** `git tag -a pre-upgrade-<sürüm> main -m "..."`, `git push origin pre-upgrade-<sürüm>`. Geri dönüş: `git switch main; git reset --hard pre-upgrade-<sürüm>; git push --force-with-lease origin main`.
4. **Örtüşme tablosu:** upstream'in her commit'i özelliklerimizle karşılaştırılır ve her birine karar verilir: upstream'i al / bizimkini koru / birleştir. `git merge-tree --write-tree --name-only main <etiket>`, main'e dokunmadan çakışacak dosyaları gösterir. Çözülemeyecek kadar büyük bir çakışmada dur ve sor.
5. **Dal:** `git switch -c upgrade/<sürüm> main; git merge <etiket>`. Merge edilen, `upstream/main` değil etikettir: etiketten sonraki commit'ler sürüme girmemiştir. Her çakışma kararı merge commit mesajına yazılır. Kaynak dosyalar Edit ile değiştirilir, CRLF korunur.
6. **Çakışma ilkesi:**
   - Bizim dalımız upstream'in aynı düzeltmesini kapsıyorsa (üst küme) bizimki korunur, upstream'in testleri alınır.
   - Upstream'in düzeltmesi bizim dokunmadığımız bir yolu iyileştiriyorsa alınır, sonra bizim eklediğimiz eşdeğer yollara da uygulanır (ör. `-nostdin`).
   - Önbelleğe alınan çıktılarda (yakınlaştırma klipleri) upstream'in değiştirdiği yolun önbellek kimliği değiştirilir ki eski çıktı kullanılmasın. Değişmeyen yolların önbelleği geçerli kalır.
   - `uv.lock`: upstream'in kilidinden başlanır, `face` extra'sı (`opencv-python-headless`) en az değişiklikle eklenir. Yerel uv kilidi baştan yeniden yazdığı için `uv lock` çalıştırılmaz. `uv lock --check` temiz çıkmalı, `uv sync --frozen --extra face` başarılı olmalı (`--extra face` verilmezse OpenCV kaldırılır).
   - i18n: 14 dilde ve upstream'in yeni dillerinde eksik anahtar olmamalı. upstream sağlayıcı etiketlerini ve ipuçlarını yalnız İngilizce tutar; `test_webui_i18n` (`ENGLISH_FALLBACK_KEYS`, `PROVIDER_TIPS_PREFIXES`) bunların ikincil dillere kopyalanmasını yasaklar.
   - `config.example.toml`: iki tarafın yeni anahtarları açıklamalarıyla birlikte.
7. **Doğrulama listesi** (upgrade dalında; her adımdan sonra `config.toml` özeti aynı olmalı):
   - Tam takım, `config.toml` özeti ve `storage/tasks` önce/sonra karşılaştırılarak. Yeni her hata açıklanır ve düzeltilir.
   - `MPT_RUN_SLOW_TESTS=1` ile `test/services/test_ffmpeg_final_render.py`, ruff, `uv lock --check`, `uv sync --frozen --extra face`.
   - E2E (a): "Zombi yıldızlar" tek video, sıcak önbellek, profil betiğiyle (kaynak görev bb6c6d3d'nin metni ve terimleri). `config.toml`'da kayma varsa ffmpeg son render ve eşzamanlılık 3 yalnız o süreçte verilir (`PROFILE_OVERRIDES`). Karşılaştırılanlar: aşama süreleri, son render'ın ffmpeg komutu (aynıysa süre farkı yükten gelir), `credits.txt` ve `subtitle.srt` (bayt bayt), bütün videoda SSIM. NASA arama önbelleğinin süresi dolduysa ilk koşu ağa çıkar; sıcak süre ikinci koşudan okunur.
   - E2E (b): iki konulu toplu iş, `presets/uzay.toml` (İngilizce çeviri açık), `--yeni` ile. Aynı girdi ve reçeteyle eski bir klasör varsa batch.py `--yeni` olmadan onu sürdürür. Kontrol edilenler: .srt doğrulayıcı, denetçi, `kalite.json` (kredi sorunları, filtre elemeleri), süreler.
   - E2E (c): WebUI Playwright ile: Toplu Üretim görünümü, ana sayfadaki "Son video oluşturma" ve Ayarlar > Materyal kaynağı ayarları > NASA (filtreler, atıf, Pixabay yedeği ve eşikleri). Değerler okunur, değiştirilmez. Streamlit 1.66 notları:
     - Seçim kutusu react-aria ComboBox'tır: değer `input[role=combobox]`'ta, liste `.react-aria-ComboBox button` ile açılır (`stSelectbox`'taki ilk düğme yardım "?" düğmesidir).
     - Çerçeveli kap `stLayoutWrapper`'dır.
     - Yeni tarayıcı profilinde ana sayfada driver.js tanıtım turu (`.driver-popover`) açılır ve tıklamaları örter; `.driver-popover-close-btn` ile kapatılır.
     - "Ayarlar" düğmesi için `exact: true` gerekir ("Altyazı Ayarlarını Sıfırla" ile çakışır).
8. **main:** Her şey geçerse `git switch main; git merge --ff-only upgrade/<sürüm>; git push origin main`. Geçmezse main'e dokunulmaz. Sonra WebUI yeniden başlatılır.
9. **PR dalları:** v1.3.10'a taşınabilirlikleri geçici bir worktree'de deneme rebase'iyle ölçülür (`git worktree add --detach`, geçici dallar silinir, push yok).

### v1.3.10 (09.11.2026)

- **Önce:** `main` a989c2c, v1.3.9 + upstream ab6004e'ye kadar 52 commit (sürüm dizesi 1.3.9). v1.3.10 = 8b0206b (07.11); ab6004e..v1.3.10 arası 11 commit, 24 dosya, +578/−61. upstream/main etiketin 2 commit ilerisinde (66db0e5, c235354); bunlar alınmadı.
- **Yedek etiket:** `pre-upgrade-v1.3.10` (a989c2c), origin'de. Merge 8f7eee1, ek düzeltme 8f45aab. main a989c2c'den 8f45aab'ye ileri sarıldı.
- **Kararlar:**

  | upstream | alan | karar |
  |---|---|---|
  | d72bb58 (#1772) | senaryo sırasında tekrar eden konular | Bizimki korundu: `fix/script-order-dedup` + even-spread aynı işi yapıyor ve ek olarak `screen_material` kancası ile eşit aralık yedek terimlerini taşıyor. |
  | 76614ae | yavaş yakınlaştırmada titreme | Birleştirildi. `_smooth_zoom_clip` hedef boyutsuz yola alındı; hedef boyutlu (NASA) yol zaten alt piksel kırpıyordu. upstream'in dosya adı etiketi yerine o yolun ayar kimliğine `zoom = "smooth"` eklendi, NASA klip önbelleği geçerli kaldı. |
  | c8dbf96 | ffmpeg konsoldan tuş okuyor | Alındı. `-nostdin` kopya concat'e ve paketin kare çıkarmasına da eklendi (8f45aab, 2 test). |
  | b42ecbc, 842525d | `subtitle.py` | Alındı. Bu dosyaya dokunmamıştık; .srt modülümüz görevin `subtitle.srt`'ini okur. Zombi'nin `subtitle.srt`'i bayt bayt aynı çıktı. |
  | ace6c8f (#1773) | ses adında yazı alt etiketi | Alındı. |
  | 22c4671 | `cheapaimodel` sağlayıcısı ve config anahtarları | Alındı; upstream ilkesi gereği ikincil dillere eklenmedi. |
  | bf33010 | Windows'ta yol testleri | Alındı; bilinen 3 hatanın 2'si düzeldi. |
  | 0501fac, e308df6, 8b0206b | README, sürüm notu | Alındı. |
  | `uv.lock`, `pyproject.toml` | — | Otomatik birleşti: upstream'in kilidi (1.3.10) + `face` extra'sı. |

- **Doğrulama:**
  - Tam takım 4145 geçti, 1 kaldı (MAX_PATH), 244 atlandı. Yavaş matris 122 geçti. ruff ve kilit temiz.
  - E2E (a): ilk koşu 73,4 sn (NASA arama önbelleğinin süresi dolmuştu, 8 arama ağa çıktı). İkinci koşu 56,7 sn (05.11: 52,2): malzeme 12,5 / 11,0, birleştirme 10,8 / 10,4, son render 31,3 / 28,7. Son render komutu aynı, fark sistem yükünden. `credits.txt` ve `subtitle.srt` aynı, bütün videoda SSIM 0,969.
  - E2E (b): 405 sn (05.11: 423). Konu başına 179 / 226 sn. .srt doğrulayıcı temiz (tr ve en altyazı sayıları eşit, konuşma örtüşmesi ort. 0,94–0,96). Kara delikler denetçide 2 Y ile kontrol gerekli çıktı (LLM içeriği: "Sagittarius A" yerine A*); Mars temiz.
  - E2E (c): bütün değerler `config.toml` ile aynı, hata yok. `config.toml` özeti baştan sona 7e00d054f184.
- **Yan etki:** İlk (b) denemesi `--yeni` olmadan 05.11 klasörünü (`20261105-220755_altyazi-iki`) sürdürdü. Yalnız `finished` damgası, `rapor.html` ve `batch.log` güncellendi.
- **PR dalları** (deneme rebase'i):
  - 28 dalın hepsi ab6004e tabanlı ve main'de.
  - 14'ü v1.3.10'a temiz taşınıyor.
  - Çakışan 11'inin hepsi acfe361 (`fix/script-order-dedup`) yüzünden; upstream d72bb58 aynı işi yapıyor. acfe361 atlanınca sıradaki çakışma 2e7a788'de (even-spread, aynı fonksiyon). Dalın tarafı kazanırsa (`-X theirs`) hepsi geçiyor, ama `perf/image-clip-reuse` ve `perf/nasa-clip-size`'da bu `_smooth_zoom_clip`'i siliyor. Bu ikisinde 8f7eee1'deki elle çözüm uygulanmalı.
  - `perf/ffmpeg-concat-normalize` metin olarak temiz, ama 8f45aab'deki `-nostdin` onda yok.
  - Üç `feat/batch-*` dalı kişisel ve main'de; rebase gerekmez (doğrusallaştırılınca 857c68c'de `schema.py`'de duruyor).
- **PR kararları** (09.11, kullanıcı):
  - `fix/script-order-dedup` bırakıldı: upstream d72bb58 (#1772) aynı işi yapıyor. Dal ve main'deki merge'ü yerinde kalır, upstream'e PR'ı izlenmez.
  - PR dalları v1.3.10'a (ya da sonrasına) taşınırken acfe361 atlanır (`git rebase --onto <hedef> acfe361 <dal>`); sıradaki çakışmada (2e7a788, even-spread) dalın tarafı korunur.
  - `perf/image-clip-reuse` ve `perf/nasa-clip-size`'da merge'deki (8f7eee1) elle çözüm uygulanır: `_smooth_zoom_clip` hedef boyutsuz yolda kalır, o yolun ayar kimliğine `zoom = "smooth"` eklenir. `-X theirs` bunu siler.
  - `perf/ffmpeg-concat-normalize`'a `-nostdin` eklenir (8f45aab'deki kopya concat değişikliği).
  - Ek (09.11, ffmpeg 9.0): `perf/ffmpeg-final-render`'a `_filter_script_option` (cdff882) eklenmeli, yoksa ffmpeg 8+'da son render hep MoviePy'ye düşer. Bizim komutlarımızı içeren dallar `fix/ffmpeg-protocol-whitelist`'in üzerine taşınırsa cdff882'deki `ffmpeg_input()` değişiklikleri de o dallara gider (aşağıdaki girdi güvenliği bölümü).

### ffmpeg (09.11.2026'dan beri 9.0.2)

- **Kullanılan:** gyan.dev release "essentials" 9.0.2, `C:\Tools\ffmpeg\9.0.2\bin\ffmpeg.exe` (depo dışında, sürüm adlı klasör; yanında `ffprobe.exe` ve `ffplay.exe`). Kullanıcı düzeyinde (sistem değil) `IMAGEIO_FFMPEG_EXE` bu dosyayı gösterir; `config.toml`'da `ffmpeg_path` yok. Uygulama, MoviePy ve imageio_ffmpeg aynı ikiliyi kullanır.
- **Doğrulama (09.11):**
  - Zip: `ffmpeg-9.0.2-essentials_build.zip`, 114 768 076 bayt, SHA-256 `60f467265b1e312373dbcd92200c2618a74850f98d3d078e94296bb3fa2047ba`. gyan.dev'deki `packages/…zip.sha256` ve `ffmpeg-release-essentials.zip.sha256`, GitHub'daki GyanD/codexffmpeg 9.0.2 sürüm varlığının özeti (`gh api repos/GyanD/codexffmpeg/releases/tags/9.0.2`) ve imageio-ffmpeg v0.8.1'in `tools/ffmpeg-sources.json`'u aynı değeri verir. İmza yok.
  - `ffmpeg.exe` SHA-256 `3256173f3f8bffd7df12227c68adf68025edb1832273a9530688a7bb1ed8edec` (imageio-ffmpeg v0.8.1'in kaydıyla aynı), `ffprobe.exe` `f0d36ecbbdd3bcfac3efa078c96c7271c2e68b3810595552ac3b7f17e9a65c52`.
  - Başlık "ffmpeg version 9.0.2-essentials_build-www.gyan.dev", gcc 16.2.0 (MSYS2). `-encoders`'ta libx264 ve h264_qsv var, h264_qsv ile 1 sn kodlama çalışıyor; `-nostdin` destekli; hls ve dash demuxer'ları var.
  - Yeni açılan bir süreçte (Explorer'dan başlatılan .bat) `IMAGEIO_FFMPEG_EXE`, `imageio_ffmpeg.get_ffmpeg_exe()`, `moviepy.config.FFMPEG_BINARY` ve `utils.get_ffmpeg_binary()` hepsi 9.0.2'yi gösterdi. Claude Code / VS Code kabukları eski ortamı taşır; testlerde değişkeni elle ver.
- **Güncelleme adımları** (sonraki sürüm için de): WebUI'yı ve toplu işleri durdur. gyan.dev'den `packages/ffmpeg-<sürüm>-essentials_build.zip`'i yeni, boş bir klasöre indir (bağlantı yavaş: 115 MB ~15 dk; `curl -C -` ile sürer). SHA-256'yı yukarıdaki üç kaynakla karşılaştır, uyuşmazsa dur. `C:\Tools\ffmpeg\<sürüm>\`'e aç. `-version`, `-encoders` (libx264, h264_qsv) ve `-nostdin`'i dene. `[Environment]::SetEnvironmentVariable('IMAGEIO_FFMPEG_EXE', '<yol>\bin\ffmpeg.exe', 'User')`. Sonra tam takım, yavaş matris, Zombi karşılaştırması (eski/yeni) ve toplu iş; WebUI yeniden başlatılınca günlükte `using ffmpeg …` satırı yeni sürümü göstermeli.
- **Geri dönüş:** `[Environment]::SetEnvironmentVariable('IMAGEIO_FFMPEG_EXE', $null, 'User')`; yeni açılan süreçler imageio-ffmpeg'in 7.1'ine döner. Açık WebUI'yı yeniden başlat. Klasör silinmeden kalabilir.
- **upstream imageio-ffmpeg'i yükseltirse:** `IMAGEIO_FFMPEG_EXE` öncelikli olduğu için paketteki yeni ikili kullanılmaz. Paketteki sürüm bizimkinden yeniyse ya da aynıysa (ör. 0.8.1 de 9.0.2 paketler) değişkeni kaldırıp paketteki ikiliyle aynı doğrulama turunu yap. Eskiyse değişkeni koru. Hangisinin kullanıldığını açılış günlüğündeki `using ffmpeg <yol> (<kaynak>): <sürüm>` satırı söyler.
- **9.0 farkları** (7.1'e göre, 09.11 ölçümü):
  - `-filter_complex_script` 8.0'da kaldırıldı; 9.0 "Unrecognized option" der ve ffmpeg son render'ı sessizce MoviePy'ye düşerdi. `video._filter_script_option` ffmpeg 7+'da `-/filter_complex graph.txt`, daha eskide (Docker'daki Debian 5.1) eski seçeneği verir. `test_ffmpeg_final_render` ffmpeg yolunun kullanıldığını (`rendered is True`) doğruladığı için bunu yakalar.
  - `-i -` artık ffmpeg'in `fd` protokolüyle açılır (7.1'de de); boru girdilerinin listesi `fd,pipe`.
  - MoviePy'nin `ffmpeg_parse_infos`'u 27 örnekte (12 stok video, 3 yakınlaştırma klibi, görev dosyaları, webm/VP9+Opus, mov 29,97, mkv 25, mp4 60 fps, wav, mp3, png, jpg) aynı sonucu verir; çözülen kareler (3 noktada SHA-1) ve klip süre/fps/kare sayısı aynı. Farklar: mp3 kap süresi yuvarlaması (180,00 → 179,99 sn, çözülen süre aynı), webm `start` None → 0,0. PNG'yi iki sürümde de ayrıştıramıyor (`Duration: N/A`; kullanılmıyor).
  - Zip'te `ffprobe.exe` olduğu için `material_upload._probe_video_metadata` artık MoviePy ayrıştırması yerine ffprobe kullanır: süre akışın tam süresi (10,69 → 10,676667 sn gibi, kap süresinin 2 basamaklı yuvarlaması değil). Zombi'de seçilen malzeme ve `credits.txt` değişmedi.
  - Zombi (sıcak, aynı metin, terim ve sabit BGM `output000.mp3`): 7.1 60,3 sn, 9.0 60,2 sn. Video süresi, 1325 kare/paket, ses 44,17 sn ve 1 947 897 örnek aynı; bütün videoda SSIM 1,0 (PSNR sonsuz), 5 karede 1,000. Akış baytları yalnız kodlayıcı etiketi (Lavc61 → Lavc63) ve x264 ayar SEI'si yüzünden farklı. Anlatımın ses örnekleri iki sürümde %99,9 korelasyonlu; aynı 7.1'le iki koşu da aynı ölçüde farklı, çünkü Edge TTS her çağrıda biraz farklı ses döndürüyor.
  - Toplu iş (uzay reçetesi, İngilizce çeviri, aynı iki konu, `--yeni`): 7.1 405 sn, 9.0 420 sn (konu başına 199 / 218 sn; LLM her koşuda yeni metin yazar). İkisinde de .srt doğrulayıcı temiz (tr ve en sayıları eşit, ffmpeg'in srt okuyucusu aynı sayıyı buluyor, mux hatası yok), kredi sorunu, glifsiz karakter ve uyarı yok, yüz filtresi 0 eledi. `batch.log`'da `using ffmpeg … 9.0.2` satırı var. 9.0'daki Mars videosunda başlangıç sapması en çok 2,19 sn çıktı: virgülden sonra 0,16 sn arayla başlayan altyazı doğrulayıcının "duraklamadan sonra" eşiğini (0,15 sn) geçiyor, ama anlatımda orada duraklama yok, o yüzden bir önceki gerçek duraklamaya göre ölçülüyor. Altyazı doğru yerde; doğrulayıcı iki ffmpeg sürümüyle de aynı sonucu veriyor.
- **Önceki paket ikilisi:** imageio-ffmpeg 0.6.0 → `ffmpeg-win-x86_64-v7.1.exe` ("7.1-essentials_build-www.gyan.dev", gcc 14.2.0, SHA-256 `2ce797a0f88d7f067180338fb227f7b1928ea727bd9a4d7a1d022f7c52af71a3`). v1.3.10 bunu değiştirmiyor.
- **Seçim sırası** (`utils.get_ffmpeg_binary`): `IMAGEIO_FFMPEG_EXE` (`[app] ffmpeg_path` dosya varsa buna yazılır) → PATH'teki ffmpeg → imageio-ffmpeg. Açılışta WebUI, API, CLI ve toplu iş (`batch.log`'a da) bir kez `using ffmpeg <yol> (<kaynak>): <sürüm satırı>` yazar (`utils.log_ffmpeg_version`).
- **ffprobe:** `material_upload._ffprobe_binary` önce ffmpeg'in yanındaki ffprobe'u, sonra PATH'i dener. imageio-ffmpeg ffprobe paketlemediği için 7.1'de MoviePy'nin `ffmpeg_parse_infos`'una düşülür. `video_project.py` ve pydub yalnız PATH'teki ffprobe'a bakar (PATH'te yok).
- **Hangi dosyayı kim çözüyor:** NASA görselleri Pillow ile açılıyor (`Image.open`, dosya yolundan `ImageClip`). ffmpeg çoğunlukla stok videoları (Pixabay/Pexels/Coverr mp4) ve ara klipleri çözüyor.
- **Açıklar:** 7.1 (Eylül 2024) sonrası düzeltilmiş, güvenilmeyen dosyayı çözerken erişilebilen açıklar var (kaynak: ffmpeg.org/security.html ve NVD):
  - 7.1.2: CVE-2025-9951 (JPEG 2000 çözücüsünde yığın taşması, yüksek); CVE-2025-59731/59732/59733 (OpenEXR DWAA/DWAB sınır dışı yazma); CVE-2025-59728 (DASH demuxer).
  - 7.1.1: CVE-2023-6605 (DASH üzerinden SSRF), CVE-2023-6602, CVE-2025-22919 (AAC çözücüsünde DoS).
  - security.html'de 8.0.3/8.1.2/9.0 altında: CVE-2026-8461 (MagicYUV sınır dışı yazma, yüksek; MOV/MKV/AVI içinde gelebilir).
  - 9.0 altında: CVE-2026-12706 (RASC use-after-free) ve CVE-2026-70631/66038/70629 (TIFF/LCL/RSCC bellek sızıntısı). 7.1.x'e taşındıkları doğrulanmadı.
  - ffmpeg dosya uzantısına değil içeriğe bakar: ".mp4" diye inen bir HLS/DASH listesi ağa çıkabilir.
  - Kaynaklarımız (NASA, Pixabay, Pexels, Coverr) yüklemeleri yeniden kodladığı için risk düşük, ama sıfır değil.
- **Karar (09.11):** 9.0.2'ye geçildi (yukarıda). Son sürümler o gün: 7.1 dalında 7.1.5 (20.06.2026), en yeni ana sürüm 9.0.2 (18.09.2026). imageio-ffmpeg 0.8.1 (03.11.2026) de Windows x64 için aynı 9.0.2 ikilisini paketliyor. BtbN derlemeleri dal ucundan (ör. `n9.0.2-48-g…`) ve günlük üretiliyor; tam sürüm etiketi için gyan.dev'in release derlemesi seçildi.

### ffmpeg girdi güvenliği (protocol whitelist)

- `app/utils/ffmpeg_inputs.py` (`fix/ffmpeg-protocol-whitelist`, 83b307c + 388c0ff): protokol listeleri yalnız burada. Diskten okunan girdi `file`, boru girdisi (`-`, `pipe:N`) `fd,pipe`, pydub'un `cache:pipe:0`'ı `cache,pipe`.
  - Kendi komutlarımız `*ffmpeg_inputs.ffmpeg_input(path)` (= `-protocol_whitelist file -i path`) kullanır; ffprobe'un konumsal girdisi `*ffmpeg_inputs.input_options(path), path`.
  - `VideoProject._run` her komutu `restrict_ffmpeg_inputs` ile yeniden yazar (upstream'in kendi kopyasının yerine).
  - MoviePy (okuyucular, yazıcılar, `ffmpeg_parse_infos`, `ffmpeg_tools`) ve pydub komutlarını kendileri kurar, girdi seçeneği almaz. `guard_libraries()` bu modüllerin `sp`/`subprocess` adını, ffmpeg komutlarına listeyi ekleyen bir `subprocess` vekiliyle değiştirir. MoviePy okuyucu/yazıcısı ya da pydub kullanan her modül bunu import sırasında çağırır.
  - Testler: `test_ffmpeg_protocol_whitelist.py` (yerel .m3u8, .mpd ve .mp4 kılığında DASH; concat listesinde `http://` ve `data:`; MoviePy ve pydub; mp4/mp3/png/concat ve MoviePy'nin borudan yazıcısı etkilenmiyor; iki durağan test: uygulamada `"-i"` geçmesi ve `guard_libraries()` çağırmayan kütüphane kullanıcısı yasak). Sahte adresler `127.0.0.1:9`, hiçbir ad çözülmez. `test_ffmpeg_main_commands.py` main'e özgü komutları sınar.
- **Neden gerekliydi:** ffmpeg demuxer'ı içerikten seçer. 7.1 `.mp4` kılığındaki bir DASH listesini tanıyıp içindeki adrese `-protocol_whitelist file` verilse bile bağlanıyor (CVE-2023-6605, 7.1.1'de düzeltildi); 9.0 bunu reddediyor. HLS'yi iki sürüm de standart dışı uzantıda tanımıyor ve varsayılan listeleri (`file,crypto,data`) http'yi zaten engelliyor. Bizim listemiz ayrıca `data:` ve `crypto` adreslerini kapatıyor. DASH testleri 7.1.1'den eski ffmpeg'de atlanır.
- **Dallarla ilişki:** dal `fix/test-config-isolation` tabanlı; yalnız upstream'in komutlarını kapsar. Bizim komutlarımız (son render, normalleştirme, kopya concat, SPS/PPS ölçümü, paket karesi, ffprobe ölçümü) main'de cdff882 ile kapsandı. Bunlar `perf/ffmpeg-final-render`, `perf/ffmpeg-concat-normalize`, `perf/cached-video-probe`, `feat/batch-production` ve `feat/batch-subtitles` dallarında; o dallar whitelist dalının üzerine taşınırsa aynı değişiklikler oraya da yapılmalı.
- Yeni bir ffmpeg komutu yazarken: girdi `ffmpeg_input()` ile; MoviePy okuyucu/yazıcısı ya da pydub kullanan yeni modül `ffmpeg_inputs.guard_libraries()` çağırır. Durağan testler unutulanı yakalar.

## Toplu üretim (batch.py)

Kişisel iş akışı (upstream'e PR hedeflenmiyor). Yayınlama yok: her video için elle yüklemeye hazır bir paket ve toplu bir kalite raporu üretir. Kod `app/services/batch_production.py` (girdi, reçete, durum, çalıştırıcı) ve `app/services/batch_package.py` (paket, kalite, rapor); testler `test/services/test_batch_production.py` ve `test_batch_package.py` (sahte görev ve sahte LLM, video üretmez).

```powershell
uv run python batch.py .\konular.csv --preset presets\uzay.toml              # storage\batches\<tarih-saat>_<girdi>\
uv run python batch.py .\konular.txt --preset presets\uzay.toml --output D:\paket\uzay
uv run python batch.py .\konular.csv --preset presets\uzay.toml --yeni       # önceki koşu varken baştan, yeni klasörde
mpt batch .\konular.csv --preset presets\uzay.toml                            # pip kurulumunda
```

Çıkış kodu: 0 hepsi bitti, 1 başarısız konu kaldı, 2 girdi ya da reçete hatası, 130 Ctrl+C / Ctrl+Break ile durdu.

**Girdi**
- `.csv`: sütunlar `konu`, `talimat` (isteğe bağlı; reçetenin talimatının sonuna eklenir), `ses` (isteğe bağlı; reçetedeki sesin yerine geçer). UTF-8 ya da UTF-8-BOM. Ayırıcı (`,` ya da `;`) ilk satırdan anlaşılır. Başlık yoksa sütunlar bu sırayla okunur; bilinmeyen başlık hata verir (`subject`, `instruction`, `voice` da kabul edilir). Boş satırlar, konusu boş satırlar ve `#` ile başlayan satırlar atlanır.
- `.txt`: satır başına bir konu; boş satırlar ve `#` ile başlayanlar atlanır.

**Reçete** (`presets/*.toml`, örnek `presets/uzay.toml`). Dört bölümü var. Bilinmeyen bölüm, anahtar ya da geçersiz değer üretim başlamadan hata verir; yakın bir ad varsa önerilir.
- `[video]`: CLI bayraklarının alt çizgili adları (`--video-source-fallback` → `video_source_fallback`); değerler `cli.parse_args`'tan geçer, yani seçenek listeleri ve tipler CLI'daki gibi denetlenir. `true`/`false` → `--x` / `--no-x`; liste virgülle birleşir. Konu başına değişen anahtarlar yasak: `video_subject`, `video_script`, `video_terms`, `video_anchors`, `video_script_prompt`, `task_id`, `batch_file`, `stop_at`, `video_count`, `video_materials`, `custom_audio_file`. Reçetede olmayan ayarlar `config.toml`'dan gelir.
- `[app]`: yalnız bu süreçte `config.app`'e yazılır, `config.toml` değişmez: `material_concurrency`, `video_clip_concurrency`, `nasa_face_filter`, `video_source_fallback_min_relevance`, `video_source_fallback_min_candidates`, `video_codec`.
- `[script]`: `prompt` (metin talimatı; yer tutucular `{kelime_min}`, `{kelime_max}`, `{konu}`) ve `target_words = [90, 100]`. Talimat göreve `--video-script-prompt` olarak gider.
- `[quality]`: `target_duration = [35, 60]` (saniye), `audit = true` (doğruluk denetimi) ve `review_before_render` (iki aşamalı akış; `uzay.toml`'da `true`, bkz. **İki aşamalı akış**).
- `[package]`: `subtitle_translations = ["en"]` (BCP-47 kodları; boş liste = çeviri yok). `batch.py --altyazi-cevirileri en,de` (boş değer = yok) yalnız o toplu iş için reçetedekinin yerine geçer ve `state.json`'a yazılır; devam ettirilen çalıştırma aynı dilleri kullanır.
- **Altyazı görünümü reçetede sabit** (`subtitle_enabled = true`, `subtitle_position = "bottom"`, `font_size = 60`, beyaz yazı + siyah kontur, `voice_rate = 1.0`). CLI reçetede olmayan bu değerleri `config.toml`'un `[ui]` bölümünden alır; 05.11'de eski bir WebUI orada altyazıyı kapatıp sesi 0,8'e, yazı boyutunu 30'a çektiği için o günkü WebUI e2e videoları (`20261105-204332_webui`, `20261105-210031_webui`) altyazısız ve yavaş seslendirmeyle üretildi. Altyazı kapalıysa görevde `subtitle.srt` da olmaz.
- `uzay.toml` uzay reçetesidir: `claude_code`, tr-TR, doğruluk kuralları, NASA + Pixabay yedeği (eşik 0,5 / 1), 9:16 kırpma, `rank`, sıra koruma, `visual` terimler, yapay zeka dışlaması kapalı, atıf katmanı, metin ve yüz filtresi, EmelNeural, STHeitiMedium, kontur 3, klip 5 sn, ffmpeg son render, eşzamanlılık 3. Talimat: en az 90, en çok 100 kelime ("90'ın altına inme"); merak uyandıran açılış; tek bir somut ve gerçek örnek; yalnız konunun adı bir takma adsa (ör. "zombi yıldız") bunu söyle, başka hiçbir şeye takma ad deme. İlk sürüm ("Yaklaşık 90-100 kelime"; "konu bir takma ad ya da benzetme içeriyorsa…") e2e'de 86 ve 88 kelime verdi ve Satürn metnine "halkaların kaybolması aslında bir takma ad" cümlesini soktu. Yeni sürüm 4 konuda 90, 94, 98 ve 103 kelime verdi; takma ad yalnız Zombi'de geçti.

**Çalışma**
- Konular sırayla, CLI'ın tek görev yoluyla üretilir (`batch_production.run_video_task`: `parse_args` → NASA/render/LLM override'ları → `build_video_params` → `prepare_cli_files` → `task.start`). Her deneme ayrı bir görevdir (`storage/tasks/<uuid>/`).
- Durum klasördeki `state.json`'da: konu başına `pending`/`running`/`done`/`failed`, deneme ve başarısızlık sayısı, hata ve aşama, süre, kalite özeti.
- Aynı komut, aynı girdi (içeriğin sha256'sı) ve aynı reçeteyle başlatılmış en son klasörü bulur ve kaldığı yerden sürer: biten konular atlanır, yarıda kalan (`running`) baştan üretilir. Girdi ya da reçete değiştiyse yeni klasör açılır; `--output` başka bir girdinin klasörünü gösterirse hata verir.
- Başarısız konu günlüğe yazılıp atlanır ve turun sonunda bir kez daha denenir. Aynı komut yeniden verilirse yine denenir.
- Ctrl+C (Windows'ta Ctrl+Break de) temiz durdurur: o konu `pending`'e döner, `state.json` ve rapor yazılır.
- Klasördeki `batch.log` bütün çalıştırmaların günlüğüdür. Her konunun görev günlüğü ayrıca belleğe alınır ve teknik kontrollerde kullanılır.

**Çıktı:** `storage/batches/<YYYYMMDD-HHMMSS>_<girdi-slug>/` içinde `state.json`, `batch.log`, `rapor.html` ve her konu için `<sıra>-<konu-slug>/`. Slug'da Türkçe harfler ASCII'ye çevrilir (`02-saturn-un-halkalari-neden-kayboluyor`). Konu klasöründekiler:
- `video.mp4`: görevin `final-1.mp4`'üne sabit bağlantı (başka sürücüdeyse kopya).
- `paylasim.md`:
  - İçerik: YouTube Shorts (başlık ≤100 karakter, açıklama, 3 etiket), TikTok (açıklama + 5 hashtag) ve Instagram Reels (açıklama + 8 hashtag). Açıklamalar, hashtag ve kredi satırları dahil 2200 karakteri aşmaz.
  - Yeni bir Türkçe istemle, üç platform tek çağrıda üretilir (`batch_package.SOCIAL_PROMPT`, aynı LLM yolu: `llm._generate_response`; temizlik `llm._clamp_text` ve `llm._normalize_hashtags` ile, sınırlar `llm.SOCIAL_PLATFORMS`'taki değerlerle aynı). `auto_upload`'un kullandığı `llm.generate_social_metadata` (`build_social_metadata_prompt`) yeniden kullanılmadı. Platform başına ayrı çağrı istiyor; `auto_upload` onu tek platform için çağırıp aynı metni bütün platformlara gönderiyor. İstemi "catchy hook" ve "call to action" istiyor; bilgiyi kuruma atfetme ve onay ima etme yasağı içinde yok.
  - Kredi satırlarını kod ekler: NASA görseli kullanıldıysa "Görüntüler: NASA ve ortakları (images.nasa.gov)" (`nasa.social_attribution`), Pixabay klibi kullanıldıysa "Ek görüntüler: Pixabay (pixabay.com)".
  - İstem bilgiyi kuruma atfetmeyi ve onay imasını yasaklar. Yanıtta "NASA'ya göre", "NASA açıkladı", "NASA sunar" gibi bir ifade varsa metin bir kez yeniden istenir. LLM yine sonuç vermezse konudan ve metnin ilk cümlesinden yedek metin yazılır ve "elle düzenleyin" notu eklenir.
- `kapak.jpg` (1080×1920, altyazısız):
  - Kullanılan kliplerin kaynak dosyalarından seçilir. Asıl kaynağın (NASA) klipleri önce gelir; yedek kaynak yalnız asıl kaynaktan aday yoksa seçilir, çünkü yedek NASA'nın karşılayamadığı terimlerde devreye girer ve çoğunlukla CGI getirir.
  - NASA klibinde ilk kare (hazırlanmış 9:16 kırpımın tamamı), video klibinde orta kare 9:16'ya kırpılır.
  - Puan = log(1 + Laplace varyansı) × parlaklığın standart sapması; ortalama parlaklık 25'in altında ya da 235'in üstündeyse ×0,3.
  - Kaynak dosya bulunamazsa son videonun orta karesi kullanılır (altyazılı).
  - Sınır: kural keskin ve kontrastlı kareyi sever, bu yüzden kompozit/grafik NASA görsellerini seçebilir. E2e'de Nötron kapağı PIA05736 oldu (iki panelli Spitzer kompoziti, köşede "IRAC" yazısı). Metin filtresi ~small önizlemede bu yazıyı kaçırdı; `nasa.crop_text_lines` 1080×1920 kapakta da bulmuyor (kısa sözcük), bu yüzden kapağa yazı denetimi eklenmedi. Kapak her kartta göründüğü için yüklemeden önce bakılmalı.
- `credits.txt` (görevde varsa), `script.txt`, `kareler.jpg` (son videodan 5 kare: süre beşe bölünür, her parçanın ortası; üstte saniye) ve `kalite.json`.
- `altyazi.tr.srt` ve her çeviri dili için `altyazi.<dil>.srt` (`app/services/batch_subtitles.py`). `paylasim.md`'nin YouTube bölümünde "Altyazı dosyaları: altyazi.tr.srt, altyazi.en.srt (YouTube Studio > Altyazılar > Dosya yükle)" notu.

**Altyazı dosyaları** (`batch_subtitles.write_subtitles`)
- Kaynak: görevin `subtitle.srt`'si. Pipeline cümleleri bölerken sınırdaki noktalamayı atıyor ("olabilir?" → "olabilir"). Altyazılar seslendirilen metne (`utils.normalize_script_for_subtitle_matching(script)`) kelime kelime hizalanır ve kelimeler metindeki halleriyle (noktalama dahil) yazılır. Hizalanamazsa (ör. whisper altyazısı) metin olduğu gibi kalır ve uyarı yazılır.
- YouTube uyumluluğu: UTF-8 (BOM'suz), CRLF, sıra numaraları 1'den ardışık, `SS:DD:ss,mmm`, bitiş > başlangıç. Çakışan bitiş sonraki başlangıca çekilir, son bitiş video süresini aşmaz, boş altyazı atılır, süresi kalmayanın metni öncekine eklenir.
- Okunabilirlik: satır ≤ 42 karakter, altyazı ≤ 2 satır. 42'yi aşan altyazı en dengeli iki satıra bölünür; noktalamadan sonra bölmek yeğlenir. İki satıra sığmayan altyazı kelime sınırından parçalara bölünür. Parça sayısı açgözlü bölmenin gerektirdiği kadardır, uzunluklar dinamik programlamayla dengelenir ve noktalamayla biten parça ödüllenir. Süre karakter sayısıyla orantılı paylaştırılır. 42 karakteri aşan tek kelime tireyle bölünür. Videoya gömülü altyazı değişmez.
- `validate_srt` bütün kuralları denetler; bulduğu her sorun `kalite.json`'da ve uyarılarda görünür.
- Çeviri:
  - Kaynak altyazıların metni tek LLM çağrısında, sırayla JSON dizisi olarak çevrilir (`TRANSLATION_PROMPT`).
  - İstemin kuralları: doğal ve akıcı ama anlamı koruyan; bilimsel terimlerin yerleşik karşılıkları; özel adlar korunur; kuruma bilgi atfetme ve onay iması yasak; altyazı başına en çok 84 karakter; yalnız JSON. Bağlam olarak metnin tamamı da verilir.
  - Dizi uzunluğu tutmazsa bir kez yeniden istenir. Yine tutmazsa 6'lık parçalar çevrilir; bir parça da tutmazsa onun satırları tek tek çevrilir.
  - Zamanlamalar kaynak dosyayla birebir aynıdır. İki satıra sığmayan bir çeviri kendi zaman dilimi içinde bölünür ve uyarı yazılır.
  - Hafif kalite kontrolü (uyarı): boş çeviri; çevrilmemiş görünen satır (Türkçeye özgü ğ ı İ ş harfleri ya da kaynakla aynı metin; Almanca ö/ü uyarı vermez); en az 20 karakterlik kaynakta uzunluk oranı 0,5–2,0 dışı.
- `kalite.json` → `altyazi`: dosyalar, kaynak dil için `kaynak_altyazi`/`altyazi`/`bolunen`/`noktalama_geri_geldi`/`sorunlar`, her çeviri için `yontem` (tek çağrı / yeniden deneme / parça parça), `cagri`, `uyarilar`, `sorunlar`. Rapor kartında `.srt` bağlantıları ve "Altyazı dosyaları" satırı var.
- Uçtan uca (05.11, `main` 35895be, CLI, uzay reçetesi, İngilizce açık; `storage/batches/20261105-220755_altyazi-iki`; config.toml özeti baştan sona aynı):
  - Konular: "Kara deliklerin içinde zaman durur mu?" (208 sn) ve "Mars'ta neden su yok?" (214 sn); toplam 424 sn.
  - Altyazılar: 13 → 16 ve 16 → 17 (3 ve 1 bölme); noktalama ikisinde de geri geldi. İki çeviri de tek çağrıda geldi; doğrulayıcı sorunu ve çeviri uyarısı yok.
  - Bağımsız ayrıştırıcı: ffmpeg'in srt okuyucusu dört dosyayı da hatasız ASS'ye çevirdi, Dialogue sayıları aynı.
  - Yumuşak altyazı: geçici mp4'e mov_text olarak eklendi (akışlar `tur`, `eng`). Gömülü akışlardan iki dilde çıkarılan karelerde yumuşak altyazı, videoya gömülü Türkçe altyazıyla aynı anda aynı cümleyi gösteriyor.
  - Ses uyumu, görevin müziksiz `audio.mp3`'ünde silencedetect ile ölçüldü:
    - her altyazının süresinin en az %87'si (ortalama %93–96) konuşmayla örtüşüyor;
    - konuşmanın %97–98'i altyazıyla kapsanıyor;
    - duraklamadan sonra başlayan altyazılar konuşma başlangıcına ortalama 0,11–0,12 sn, en fazla 0,18 sn uzakta.
  - Çeviri değerlendirmesi:
    - Anlam kaybı yok; terimler yerinde (event horizon, redshifted, relativity, solar wind, rover); sayılar İngilizce biçimde ("7,700 kilometers per second").
    - Satır satır çeviri zorunluluğu birkaç yerde sözdizimini zorluyor. "was circled in sixteen years by the star S2" edilgen ve ağır; "froze into soil and poles" sıkıştırılmış.
    - Bir altyazının içeriği komşusuna kayabiliyor ("buharlaştı" İngilizcede sonraki satırda). Zamanlama aynı kaldığı için okunuşu bozmuyor.
    - "hidrojenin ağır biçimi" "hydrogen's heavy form" olarak kaldı; yerleşik terim "deuterium" olurdu. İstem yerleşik terimi istiyor ama kaynak da terimi kullanmamıştı.
    - Bir önceki denemede (Satürn) çevirmen yanıltıcı Türkçe "bize dönük"ü "edge-on" yaparak anlamı düzeltti. Doğru ama kaynağa sadık değil; denetçi o cümleyi zaten Y işaretliyor.

**Kalite** (`kalite.json`, `batch_package.build_package`)
- Doğruluk denetimi: metni yazan istemden bağımsız, şüpheci bir denetçi (`AUDIT_PROMPT`; talimat İngilizce, gerekçeler Türkçe). İddia başına D (doğru ve kesin), B (basitleştirilmiş, kesin değil ya da doğrulanamıyor; emin değilse B), Y (yanıltıcı), X (yanlış) ve kısa bir gerekçe, JSON olarak. Yanıt okunamazsa bir kez yeniden istenir; yine olmazsa "yapılamadı".
- Teknik kontroller:
  - video süresi ve kelime sayısı, reçetenin hedefine göre;
  - altyazı yazı tipinde glifi olmayan karakterler (`video._subtitle_font_supports_sample`);
  - NASA ya da izinli ortak dışı kredi (`nasa.free_use_credit`);
  - görev günlüğünden boş ve yedeğe geçen terimler;
  - filtre elemeleri: metaveride kişi, yazı, telif, kredi, çözünürlük; önizlemede yazı; yüz.
- Önbellekten gelen NASA aramaları günlüğe yazılmaz. Bu yüzden elemeler yalnız yeni aramalarda sayılır; önbellekten gelen arama sayısı `onbellekten_gelen_aramalar`'da.
- **Kontrol gerekli** sayılan durumlar: en az bir Y ya da X; denetim yapılamadı; glifsiz karakter; kredi sorunu; paket hazırlanamadı.
- **Uyarı** (`uyarilar`; kartta turuncu satır, tek başına kontrol gerektirmez): süre ya da kelime sayısı hedef dışı; altyazı uyarıları (hizalanamayan metin, doğrulayıcı sorunu, çeviri uyarıları). LLM kelime sayısını birkaç kelime şaşırıyor; ilk e2e'de bunlar kontrol gerekli sayılınca 3 videonun 2'si yalnız 86 ve 88 kelime yüzünden işaretlendi.

**rapor.html**
- Tek dosya, çevrimdışı açılır; görseller ve bağlantılar göreli yolla. Her konudan sonra yeniden yazılır.
- Üstte özet: toplam, başarılı, başarısız, kontrol gerekli, bekleyen, toplam süre (bütün çalıştırmalar).
- Kartların sırası: başarısız, kontrol gerekli, hazır, bekleyen.
- Videolu kartta: konu, üretim süresi, deneme, görev kimliği, klasör ve video bağlantısı, kontrol nedenleri, uyarılar, metin, işaretli iddialar ve gerekçeleri, D/B/Y/X sayımı, teknik tablo, kapak, kareler; açılır bölümlerde bütün iddialar ve paylaşım metni.
- Başarısız kartta hata ve aşama.

**Uçtan uca (05.11, `main` f512d22, `claude_code`, uzay reçetesinin ilk sürümü)**
- Girdi: 4 satırlık CSV (UTF-8-BOM, `;`): Nötron yıldızları; Satürn'ün halkaları neden kayboluyor? (talimatlı); Zombi yıldızlar; Kara delikler (ses `tr-TR-YokNeural-Female`, bilerek geçersiz).
- Kesinti ve devam: 2. konu bitip 3. başladıktan 10 sn sonra Ctrl+Break gönderildi. Süreç 2 sn içinde 130 ile çıktı, Zombi `pending`'e döndü. Aynı komut 2 biteni atlayıp Zombi'den sürdü.
- Hatalı satır: ses aşamasında düştü ("failed to synthesize audio…"), turun sonunda bir kez daha denendi, yine düştü (2 deneme, 44 sn). Raporda en üstte, kırmızı "başarısız" rozeti, hata ve aşamayla göründü. Çıkış kodu 1.
- Süreler: toplam 741 sn (12 dk 21 sn). Nötron 288 sn, Satürn 238 sn, Zombi 160 sn (kesilen ilk deneme hariç), hatalı satır 44 sn. Paket (paylaşım metni + kapak + kareler + denetim) 55 / 47 / 40 sn; bunun çoğu iki LLM çağrısı.
- Videolar: 46,4 / 47,2 / 46,3 sn; 91 / 88 / 86 kelime; glifsiz karakter, kredi sorunu ve boş terim yok. Yedeğe geçen terim 4 / 6 / 1.
- Denetçi ve kendi değerlendirmem:
  - Nötron 5 D / 2 B. Kendi değerlendirmem: 6 D / 1 B, Y ve X yok. Denetçi çay kaşığı (milyar ton) ve 20 km çap için B dedi; ikisi de yaygın ve büyüklük olarak doğru yuvarlamalar.
  - Satürn 4 D / 3 B / 1 Y. Y: "halka düzlemi tam olarak bize dönük hale gelir"; kayboluş anında düzlemin kenarı bize döner, bu yüzden ben de Y diyorum. Ayrıştığımız yerler:
    - "Mart 2025'te yine böyle bir geçiş yaşandı" cümlesine denetçi B dedi, ben D diyorum (geçiş 23 Mart 2025'te oldu).
    - "Halkaların kaybolması aslında bir takma ad" cümlesine denetçi D dedi, ben B diyorum: kayboluş bir takma ad değil. Bu cümleye reçete talimatı yol açtı; talimat bu yüzden düzeltildi.
  - Zombi 1 D / 5 B, Y ve X yok. Kendi değerlendirmem: 3 D / 3 B, Y ve X yok. Denetçi yorum cümlelerinde ("astronomlar … olarak yorumluyor", "zombi yıldız diyoruz") da B verdi.
  - Özet: denetçi benden daha temkinli (daha çok B), ama Y/X'te ayrışmadık. Tek Y'yi ikimiz de aynı cümlede bulduk.
- İlk kuralla 2 video (Satürn Y + 88 kelime, Zombi 86 kelime) kontrol gerekli oldu; yeni kuralla yalnız Satürn (Y), Zombi uyarılı. `rapor-ilk.html` ilk raporu saklıyor.
- Görsel notları:
  - Satürn'de 6 terim Pixabay'e geçti. "thin line" terimi bir kadının çizgi çizimini, "orbit animation" CGI yörünge getirdi.
  - Nötron'da "lighthouse beam" Pixabay cam küre CGI'ı getirdi.
  - Kapak notu yukarıda.

**Nasıl yapılır**
1. `konular.csv` yaz (ilk satır `konu;talimat;ses`) ya da satır başına bir konuyla `konular.txt`.
2. `uv run python batch.py .\konular.csv --preset presets\uzay.toml` çalıştır. Claude Code içinden çalıştırıyorsan önce `CLAUDECODE` ve `CLAUDE_*` ortam değişkenlerini temizle (`claude_code` sağlayıcısı).
3. Durdurmak için Ctrl+C; devam etmek için aynı komut. Başarısız konuları yeniden denemek için de aynı komut.
4. `storage\batches\<klasör>\rapor.html`'i aç. Önce başarısız ve kontrol gerekli kartlara bak: işaretli iddiaları gerekçeleriyle oku. Metin düzeltilecekse konuyu `talimat` sütunuyla yeni bir girdide yeniden üret.
5. Yüklemek için her konu klasöründen `video.mp4`'ü al, başlık, açıklama ve hashtag'leri `paylasim.md`'den kopyala, kapak olarak `kapak.jpg`'yi kullan.
6. Yeni reçete için `presets\uzay.toml`'u kopyala; anahtar adlarını `uv run python cli.py --help`'teki bayraklardan al.

### WebUI'dan toplu üretim

Dal `feat/batch-webui` (`feat/batch-production` üzerinden). Kod `webui/batch_page.py` (bölüm), `app/services/batch_runner.py` (arka plan çalıştırıcı), `webui/Main.py`'de üç küçük ekleme; testler `test/services/test_batch_runner.py` ve `test_batch_webui.py` (AppTest, sahte çalıştırıcı).

**Yerleşim:** WebUI tek sayfa (`pages/` yok); görev yöneticisi ve ayarlar üst çubuktan açılan diyaloglar. Toplu üretim üst çubuktaki "Toplu Üretim" düğmesiyle açılan ayrı bir görünüm (`?view=batch`; düğme orada "Tek Video" olur), ana 4 sütunlu formun yerine çizilir. Gerekçeler:
- Diyalog uzun süren izleme için uygun değil: uygulama yeniden çalışınca kapanıyor, alan dar.
- `st.navigation` giriş betiğini baştan kurmayı gerektirirdi.
- Adres parametresi sayfa yenilense ya da sekme yeniden açılsa da görünümü korur.
- Bölüm, `codex_login.py` gibi ayrı bir modülde durur ve `AppTest.from_string` ile test edilir.

**config.toml:** toplu görünüm ana formu ve sayfa sonundaki `_save_runtime_config()`'i hiç çalıştırmaz, bölüm de config'e yazmaz (test: `try_save_config`/`save_config` çağrılmaz, dosya baytları aynı). Ana formun içerik alanları (konu, metin, terimler, çapalar) toplu görünümdeyken oturumda korunur. Konu listesinin taslağı `storage/batches/.taslak.json`'da.

**Bölüm**
- Konu listesi: `st.data_editor` (konu, talimat, ses; satır ekleme/silme). Ses sütunu reçetenin diline uygun seslerden seçilir (`voice.get_all_azure_voices(filter_locals=[video_language])`), boş = reçetedeki ses.
- İçe aktarma `.csv`/`.txt` (`batch_production.read_inputs`, aynı kurallar); dışa aktarma `.csv` (UTF-8-BOM, `;`, `konu;talimat;ses`). "Listeyi temizle".
- Reçete: `presets/*.toml` seçimi ve salt okunur özet (kaynak → yedek, ses, en-boy, hedef kelime, LLM).
- Başlat öncesi doğrulama: boş konu (talimatı/sesi olan konusuz satır), tekrar eden konu (büyük/küçük harf duyarsız), listede olmayan ses; hatalar satır numarasıyla. Liste `storage/batches/<tarih-saat>_webui/konular.csv` olarak yazılır ve o klasörle başlatılır.
- Çalışırken her 3 sn'de kendini yenileyen bir `st.fragment(run_every=3)`: konu başına durum tablosu (bekliyor / çalışıyor: aşama / bitti / kontrol gerekli / başarısız, süre, deneme, not), ilerleme çubuğu (geçen süre ve kalan tahmini), son 12 günlük satırı ve Durdur. Parçacık yalnız iş çalışırken çizilir; bitince sayfayı bir kez yeniden çalıştırıp durur.
- Süreç bitince sonuç klasörden okunur: bitti, yarım kaldı ("Geçmiş'ten devam ettirin") ya da hiç başlayamadı (batch.log'un son hata satırıyla).
- Geçmiş: `storage/batches` altındaki son 10 toplu iş (tarih, konu sayısı, bitti/başarısız/kontrol gerekli, tamamlandı/yarım/çalışıyor). "Raporu aç" (varsayılan tarayıcıda `rapor.html`), "Klasörü aç" ve yarım kalanlarda "Devam ettir". Raporu ve klasörü yalnız yerel erişimde açar: masaüstü olan sistem, Streamlit `server.address` (yoksa `listen_host`) döngü adresi ve istemci döngü adresinden. Değilse yolu gösterir.
- Tek video sayfası, bir toplu iş çalışırken Video Oluştur düğmesinin üstünde uyarı gösterir (CPU çakışması); düğme çalışmaya devam eder.
- Çeviri dilleri için çoklu seçim (`batch_subtitles.LANGUAGES`, videonun dili hariç; reçetedeki değerle başlar). Seçim `start_batch(..., translations=…)` ile `--altyazi-cevirileri` olarak gider; reçete dosyasına ve `config.toml`'a yazılmaz. Reçete özetinde "Çeviri altyazıları" satırı var.
- Tablonun boş hücreleri "None" yerine boş görünür: metin sütunlarının varsayılanı `""`. Seste `""` kullanılamaz: Streamlit 1.66'nın `SelectboxColumn`'u `""` değerini (seçeneklerde olsa da, `format_func` etiketi verilse de) boş değer sayıp gri "None" çizer, `TextColumn` çizmez. Bu yüzden tabloda "ses seçilmedi" `EMPTY_VOICE = " "` (seçenek ve varsayılan; taslaktan yüklenen satırlarda da). `normalize_rows` kırptığı için taslak, CSV ve doğrulama `""` görür (`fix/batch-draft-empty-voice`, 65f9e37; önceki `""` varsayılanı yeni satırlarda da "None" gösteriyordu). "Boş = reçetedeki ses" ipucu sütun başlığında ve tablonun altında (`batch.col.voice_hint`).
- Geçmiş kartında video başına `.srt` dosyaları ("Altyazı dosyaları" açılır bölümü).
- 159 metin (`batch.*`; 73'ü iki aşamalı akışın) 14 dilde.

**Çalıştırıcı** (`batch_runner`)
- `start_batch`: `[python, -X, utf8, batch.py, <girdi>, --preset, <reçete>, --output, <klasör>, --gunluk-stderr]`; kabuk yok, `stdin` boş, stdout/stderr klasördeki `batch.log`'a (ekleme kipinde), Windows'ta `CREATE_NEW_PROCESS_GROUP` (POSIX'te yeni oturum). pip kurulumunda `-m app.services.batch_production`. `--gunluk-stderr` batch.py'nin aynı dosyaya ikinci kez yazmasını önler.
- Kilit `storage/batches/.lock` (JSON: pid, süreç başlangıç zamanı, klasör, komut). `O_EXCL` ile oluşturulur. Süreç yoksa ya da PID'in başlangıç zamanı farklıysa (PID başka sürece geçmiş) kilit bayattır ve silinir. batch.py de aynı kilidi alır (`run_batch(hold_lock=True)`), komut satırından başlatılan toplu iş de korunur; meşgulse çıkış kodu 3.
- **Windows tuzağı:** `.venv\Scripts\python.exe` bir başlatıcıdır ve asıl yorumlayıcıyı alt süreç olarak açar. WebUI kilide başlatıcının PID'ini yazar (Ctrl+Break o süreç grubuna gider), batch.py ise yorumlayıcıda çalışır. Bu yüzden batch.py üst sürecinin PID'ini de kendi kilidi sayar. İlk WebUI e2e'si bunu yakaladı: batch.py "başka bir toplu üretim çalışıyor" deyip 3 ile çıkıyordu; gerçek alt süreçli test eklendi.
- **Windows tuzağı 2:** hedef dosya başka bir süreçte okunmak üzere açıkken `os.replace` "Erişim engellendi" verir. WebUI `state.json`'ı her 3 sn'de okuduğu için ikinci e2e'de batch.py durumu kaydederken düştü. `batch_production.replace_file` değiştirmeyi 5 sn boyunca 0,1 sn arayla yeniden dener; `state.json` ve taslak onu kullanır. Süreç bittiğinde iş yarım kalmışsa ve günlükte o çalıştırmadan bir Traceback varsa, arayüz "durdu" yerine istisnanın son satırını gösterir.
- `stop_batch`: Ctrl+Break (POSIX'te süreç grubuna SIGINT), 30 sn içinde kapanmazsa `taskkill /T /F` ile süreç ağacı. Ardından `state.json`'da `running` kalan konu `pending` olur, kilit silinir. Ctrl+Break yalnız aynı konsolu paylaşan süreçlere ulaşır: WebUI `webui.bat` ile bir konsolda çalışıyorsa alt süreç o konsolu paylaşır. WebUI yeniden başlatıldıysa ya da konsolsuzsa sonlandırmaya düşülür; durum yine tutarlı kalır.
- `resume_batch`: `state.json`'daki girdi ve reçeteyle aynı klasöre aynı komut. Girdi ya da reçete yoksa veya özeti değiştiyse `ResumeError` (arayüzde nedeniyle).
- `batch_status`: `state.json` + batch.log'un son 256 KB'ı. Çalışan konunun aşaması günlükteki `toplu üretim [i/N] başladı`, `generation activity: phase=…` ve `toplu üretim [i] paket hazırlanıyor` satırlarından. Çalışırken geçen süre `ItemState.started`'dan, kalan tahmin biten konuların ortalamasından (hiç yoksa 240 sn). Süreç ölmüşse yarıda kalan konu "bekliyor" görünür.

**WebUI'dan nasıl yapılır**
1. WebUI'yı `webui.bat` ile bir konsolda başlat (Ctrl+Break'in toplu işe ulaşması için), üst çubukta "Toplu Üretim".
2. Konuları tabloya yaz (araç çubuğundaki + ile satır ekle, hücreye çift tıkla) ya da `.csv`/`.txt` içe aktar. Reçeteyi seç, özetine bak.
3. "Toplu üretimi başlat". Durum tablosu 3 sn'de bir yenilenir; sekmeyi kapatıp açmak durumu etkilemez (durum diskten okunur).
4. "Durdur" o konuyu bekliyor'a çevirir; Geçmiş'ten "Devam ettir" aynı klasörde kaldığı yerden sürer.
5. Bitince Geçmiş'ten "Raporu aç" / "Klasörü aç"; sonra paketleri yukarıdaki gibi yükle.

**Uçtan uca (05.11, `main` 04cd0fe, WebUI yeniden başlatıldı, Playwright ile)**
- Liste arayüzde oluşturuldu (iki konu: "Ay neden hep aynı yüzünü gösterir?", "Güneş bir gün sönecek mi?"), uzay reçetesiyle başlatıldı.
- Başlatıldıktan sonra sekme kapatılıp açıldı: durum tablosu, aşama ("Çalışıyor: seslendirme"), ilerleme ve günlük doğru göründü.
- İlk konu bitince (226 sn) Durdur: Ctrl+Break'ten ~1 sn sonra süreç temiz kapandı ("durdu", zorla sonlandırma gerekmedi), kilit kalktı, ikinci konu `pending`.
- Geçmiş'te "yarım" + "Devam ettir": ikinci konu 201 sn'de bitti; toplam 427 sn. "Raporu aç" varsayılan tarayıcıyı (Firefox) çağırdı.
- config.toml özeti baştan sona aynı. Ekran görüntüleri `scratchpad\e2e_webui\goruntu\` altında.
- Önceki üç deneme iki gerçek hata ve bir betik hatası buldu:
  1. venv başlatıcısının PID'i yüzünden kilit reddedildi.
  2. Okunan `state.json` değiştirilemedi.
  3. Betik eski bir klasörü izledi.
  İki gerçek hata düzeltildi ve testlendi; üçüncü denemenin toplu işi (`20261105-204332_webui`) WebUI 3 sn'de bir okurken baştan sona çöküşsüz bitti.

**Sıradaki**
- (a) Altyazı görünümü düzenleyicisi: yazı tipi, boyut, renk, kontur ve konum için canlı önizleme (örnek karede); Türkçe glif kontrolü (ğ ı İ ş ç ö ü ve noktalama, `video._subtitle_font_supports_sample`); sonucu yeni bir reçete olarak kaydetme (`presets/*.toml`). Bugün altyazı görünümü `uzay.toml`'da elle sabitleniyor.
- (b) ~~İki aşamalı toplu iş~~: yapıldı (09.11), bkz. **İki aşamalı akış**.
- (c) Arayüzde sonuç inceleme: Geçmiş'teki bir toplu işin videolarını oynatma, kapağı ve kareleri görme, işaretli iddiaları gerekçeleriyle okuma, paylaşım metnini ve `.srt`'leri platform başına kopyalanabilir alanlarda gösterme. Bugün bunlar için `rapor.html` açılıyor.

### İki aşamalı akış (önce metin, onaydan sonra video)

Dal `feat/batch-review` (09.11). Kod `app/services/batch_review.py` (dosyalar, onay kuralları, komutlar), `batch_production.py` (hazırlık ve render aşamaları), `batch_runner.py` (başlatma, durum), `batch_package.py` (onaylanan metnin denetimi), `webui/batch_page.py` (inceleme paneli). Testler `test_batch_review.py` (47) ve `test_batch_review_webui.py` (14, AppTest).

**Ne zaman:** reçetede `[quality] review_before_render = true` (`uzay.toml`'da açık). Yalnız o toplu iş için `batch.py --inceleme` / `--inceleme-yok` ya da WebUI'daki "İki aşamalı akış" kutusu (reçeteye ve config.toml'a yazılmaz). Akış toplu iş açılırken `state.json`'a yazılır, devam ettirilen çalıştırmalar aynı akışı sürer. Kapalıyken davranış birebir eskisi gibi (eski testler `review=False` / `--inceleme-yok` ile koşuyor).

**Durumlar** (`state.json`, yalnız arka plan süreci yazar): `pending` → `preparing` → `awaiting_review` → `approved` → `rendering` → `done` / `failed`; ayrıca `skipped`. "Kontrol gerekli" `done` + `kalite.json`'daki işarettir. Onay ve atlama kararları `review.json`'dadır; görünen durumu `batch_review.effective_status` ikisinden birlikte çıkarır. Süreç ölmüşse yarıda kalan hazırlık "bekliyor", yarıda kalan render "onaylandı" görünür ve sonraki çalıştırma sürdürür. `ItemState`'te hazırlık ayrı sayılır: `prepared`, `prepare_seconds`, `prepare_attempts`, `prepare_task_id` (render süresi `seconds`'ta).

**Hazırlık** (`batch.py <girdi> --preset …`; aynı komut ve aynı kural: biten atlanır, başarısız turun sonunda bir kez daha denenir):
- Her konu için metin, arama terimleri ve çapalar, görevin "terms"te duran yolundan (`bp.run_text_task`; LLM çıktısının noktalama düzeltmeleri dahil). Ardından mevcut denetçi (`batch_package.audit_script`). Ses ve video yok.
- Kelime sayısı ve tahmini süre (`batch_review.estimate`). Türkçe Edge (EmelNeural, hız 1.0) 18 videoda ölçüldü: süre ≈ 0,067 sn × boşluksuz harf + 7,7 sn (en büyük sapma 3,1 sn; kelimeyle 5,2 sn).
- Sonuç konu klasörünün `hazirlik.json`'una yazılır, konu `awaiting_review` olur. Hepsi hazırlanınca süreç 0 ile çıkar (hazırlanamayan kaldıysa 1).

**Dosyalar** (konu klasöründe):
- `hazirlik.json` (yalnız arka plan süreci): metin, özeti, terimler, çapalar, denetim, kelime/harf/tahmini süre, istem, görev kimliği, süre.
- `review.json` (yalnız WebUI ve onay komutları; atomik: benzersiz geçici dosya + `replace_file`; okuma `PermissionError`/yarım JSON'da 20 kez yeniden denenir): güncel metin, terimler, çapalar, `script_edited`, `terms_edited` (elle düzenlendi: metin değişince yeniden üretilmez), `terms_sha256` (terimlerin üretildiği metin), `audit` ve `audit_sha256` (son denetim ve ait olduğu metin), `approved`, `approved_at`, `approved_sha256` (onaylanan metin; metin sonra değişirse onay geçersiz), `approved_despite_flags`, `skipped`, `history` (yeniden üretimlerde talimat + önceki metin, terimler, denetim), `last_action` (son işlem; başarısızsa WebUI gösterir). İlk işleme kadar dosya yoktur, içerik `hazirlik.json`'dan gelir.
- `.islem.json`: süren LLM işleminin kilidi (pid + süreç başlangıcı; süreç ölünce bayat sayılıp silinir). `.duzenleme.txt`: WebUI'nın düzenlenmiş metni komuta verdiği dosya. `inceleme.log`: WebUI'nın başlattığı komutların çıktısı.

**Onay kuralları** (`batch_review`; CLI ve WebUI aynı fonksiyonları kullanır):
- Onayla: metin son denetimden sonra değiştiyse önce yeniden denetlenir; terimler elle düzenlenmediyse ve metin terimlerden sonra değiştiyse metinden yeniden üretilir. Denetimde Y/X varsa ya da denetim yapılamadıysa onay yalnız "yine de onayla" ile (`force`; `approved_despite_flags` ve kalite nedeni olarak raporda). Engellenen onayda düzenleme ve yeni denetim yine kaydedilir.
- Yeniden üret: isteğe bağlı ek talimat satırın talimatına eklenir; metin + terimler + çapalar üretilip denetlenir, önceki metin `history`'de.
- Yeniden denetle: (düzenlenmiş) metni onaylamadan denetler; eskimiş ve elle düzenlenmemiş terimleri de yeniler. Böylece "Temizleri onayla" hiç LLM gerektirmez.
- Atla / atlamayı geri al. Onaylı ya da atlanmış konu düzenlenemez.
- Temizleri onayla: onay bekleyen, son denetimi güncel metne ait, terimleri güncel (ya da elle düzenlenmiş) ve Y/X içermeyen (denetimi yapılmış) bütün konular. Kilitli konular ve WebUI'da kaydedilmemiş düzenlemesi olanlar (`exclude`) dışarıda.
- LLM işlemleri (`PipelineServices`) reçetenin `[app]` ayarlarını ve LLM sağlayıcısını/modelini ilk çağrıdan önce süreç içi `config.app`'e yazar. Bu yüzden yalnız batch.py süreçlerinde çalışır, Streamlit sürecinde hata verir. E2e'de bulunan hata: geçersiz kılma yalnız görev yolundaydı, denetimin önce geldiği yeniden denetim config.toml'daki sağlayıcıyla (moonshot) denetledi (eab2a1b). Terim üretimi başarısız olursa yeni denetim önce kaydedilir.
- `test/conftest.py`'deki `no_real_batch_review_llm` her testte `prepare_item` ve `PipelineServices`'i reddedene çevirir: `uzay.toml`'u `review=False` ya da sahte fonksiyon vermeden koşan bir test gerçek LLM'e (claude_code) gitmez, hemen düşer. İlk denemede eski testler böyle takılıp kalmıştı.

**Render** (`batch.py render <klasör>`; WebUI'da "Onaylıları render et"):
- Süreç başlarken `review.json`'lar bir kez okunur: geçerli onaylı ve videosu olmayan konular `approved`, atlananlar `skipped` olur. Render sürerken onaylananlar bu turda alınmaz, sonraki `render` çağrısını bekler.
- Onaylanan metin, terimler ve çapalar görev parametresi olur (`build_render_argv`: `--video-script=…`, `--video-terms=…`, `--video-anchors=…`; metin talimatı eklenmez). `task.generate_script` / `generate_terms` LLM'e sormaz.
- Paket yeniden denetlemez: `kalite.json` ve rapor onaylanan metnin son denetimini kullanır, `inceleme` alanı (onay zamanı, yine de onaylandı mı, metin/terim düzenlendi mi, yeniden üretim sayısı) eklenir. Paylaşım metni ve altyazı çevirisi için LLM yine çağrılır.
- Kesinti ve devam eski kurallarla: `rendering` → `approved`. Bütün konular `done`/`skipped`/(render'da) `failed` olunca toplu iş biter. Reçete hazırlıktan sonra değiştiyse `render` reddeder.

**Komutlar** (`batch.py <komut> <klasör> …`; klasör yol ya da `storage/batches` altındaki ad). Çıkış kodları: 0 tamam, 1 işlem başarısız (LLM; `review.json`'da `last_action`), 2 girdi hatası ya da işlem yapılamaz, 3 başka işlem sürüyor, 4 `--yine-de` gerekiyor.

```powershell
uv run python batch.py .\konular.csv --preset presets\uzay.toml        # hazırlık; konular onay bekler
uv run python batch.py durum 20261109-155927_webui                       # kelime, süre tahmini, denetim, Y/X iddiaları
uv run python batch.py onayla <klasör> 2 3                               # değişmemiş, temiz metinler (LLM yok)
uv run python batch.py onayla <klasör> 1 --metin-dosyasi .\duzeltilmis.txt   # düzenle + yeniden denetle + onayla
uv run python batch.py onayla <klasör> 1 --yine-de                       # Y/X'e rağmen
uv run python batch.py onayla <klasör> 1 --terimler "black hole, milky way" --capalar "space"
uv run python batch.py yeniden-uret <klasör> 2 --talimat "Kabuğun kalınlığından söz etme"
uv run python batch.py yeniden-denetle <klasör> 1 --metin-dosyasi .\duzeltilmis.txt
uv run python batch.py atla <klasör> 3            # --geri-al ile geri
uv run python batch.py temizleri-onayla <klasör>
uv run python batch.py render <klasör>            # yalnız onaylılar; sonradan onaylananlar için yeniden çağır
```

**WebUI:**
- Başlatırken "İki aşamalı akış" kutusu (reçeteden gelir). Hazırlık sürerken durum tablosunda "Hazırlanıyor: metin / arama terimleri / doğruluk denetimi". Bitince "Hazırlık bitti: N konu onay bekliyor" ve inceleme bölümü açılır.
- İnceleme bölümü (en yeni onay bekleyen toplu iş ya da Geçmiş'te "İncele"):
  - Üstte sayılar: onay bekliyor, onaylı (render bekliyor), Y/X olan, atlandı, videosu hazır. Düğmeler: "Temizleri onayla (n)" ve "Onaylıları render et (n)"; başka bir toplu iş çalışırken render kilitli.
  - Onay bekleyen her konunun paneli: düzenlenebilir metin, kelime sayısı ve tahmini süre (hedef dışında turuncu uyarı; metin kutudan çıkınca ya da Ctrl+Enter'la güncellenir), D/B/Y/X renkli iddialar ve gerekçeleri (metin değiştiyse "eski denetim"), açılır bölümde terimler (satır başına) ve çapalar, ek talimat kutusu.
  - Düğmeler: Onayla (güncel denetimde Y/X varsa "Yine de onayla"), Yeniden üret, Yeniden denetle, Atla. Onaylı/atlanmış/bitmiş konular tek satır; onaylı metin açılır bölümde.
- LLM gerektirmeyen işlemler (değişmemiş metnin onayı, atlama, temizleri onaylama) WebUI sürecinde doğrudan. Gerektirenler aynı onay komutuyla alt süreçte (`batch_runner.start_review_action`), konunun kilidi alt sürecin PID'iyle yazılır. Kilitliyken konunun öğeleri kapalı, "… sürüyor … sn" ve ilerleme çubuğu var; panel 2 sn'de bir yenilenir, işlem bitince sayfa yenilenir.
- Kaydedilmemiş düzenlemeler oturumda kalır: öğe anahtarları `review.json`'un sürümünü içerir, başka konuların değişmesi ve sayfanın kendini yenilemesi onları silmez. "Temizleri onayla" bu konuları atlar ve altta listeler.
- Geçmiş'te onay bekleyen toplu işler turuncu "onay bekliyor" rozeti, "N onay bekliyor · N onaylı · N atlandı" ve "İncele" düğmesiyle. "Devam ettir" yalnız yarıda kalmış hazırlık ya da render için.

**Uçtan uca (09.11, `main` b572fb7, WebUI + Playwright, ffmpeg 9.0.2, claude_code; `storage/batches/20261109-155927_webui`; ekran görüntüleri scratchpad `e2e_review\`; config.toml özeti baştan sona 7e00d054f184):**
- Hazırlık 194,8 sn (konu başına 56 / 55,6 / 82,9 sn). Denetim üç konuda da Y buldu:
  - Kara delikler D5 B2 Y2: "Sagittarius A" (doğrusu A*) ve "değişen tek şey, artık geri dönüş yolunun olmamasıdır".
  - Ay D7 B2 Y1: "kabuk daha kalın" Luna 3'e bağlanmış.
  - Nötron D8 B2 Y1: Yengeç pulsarının "saat gibi" kullanıldığı.
- Kara delikler elle düzeltildi (A*; "gelgit kuvvetleri onu sonunda parçalar"). Düzenleme kaydedilmeden Ay yeniden üretilirken sayfa kendini yeniledi ve düzenleme korundu. Yeniden denetim temiz (D7 B2, 44 sn), terimler yeni metinden yeniden üretildi, Onayla doğrudan (LLM yok).
- Ay ek talimatla yeniden üretildi (65 sn): kabuk sorunu gitti ama yeni metin X ("kimse arka yüzü görmedi"; Apollo 8 gördü) ve Y ("hiç görünmez") aldı. Talimat genişletilip ikinci kez üretildi (59 sn): temiz (D7 B2), onaylandı. Geçmişte iki eski metin var.
- Nötron "Temizleri onayla"da (0) kaldı, çünkü güncel denetimde Y vardı. Render sürerken metni düzeltildi ("milisaniye pulsarlarını saat gibi kullanır"), yeniden denetim temiz (D6 B6, 55 sn), "Temizleri onayla (1)" ile onaylandı. Çalışan render onu almadı.
- Render 1 (iki konu) 313 sn: 200,3 / 102,8 sn. Render 2 (Nötron) 186 sn: 174,2 sn.
- Doğrulama:
  - üç videoda `script.txt` = görevin metni = onaylanan metin, görevin terimleri ve çapaları = onaylananlar;
  - `kalite.json` denetimi = onaylanan metnin son denetimi, kontrol gerekli yok;
  - render günlüğünde konunun başlangıcı ile paket satırı arasında LLM çağrısı yok;
  - düzenlenen ifadeler (Sagittarius A*, "gelgit kuvvetleri onu sonunda parçalar", "milisaniye pulsarlarını saat gibi kullanır", Ay'ın yeni cümlesi) hem `altyazi.tr.srt`'de hem görevin TTS'ten gelen `subtitle.srt`'sinde, yani seslendirmede.
  - Tahmini/gerçek süre: 46,6/44,2, 47,2/47,6, 50,6/51,5 sn.
- İlk deneme iki sorun buldu:
  - claude_code oturum sınırına takıldı ("session limit · resets 4:20pm"); yeniden üretim ve yeniden denetim başarısız oldu, `last_action` hatası WebUI'da göründü.
  - Yeniden denetimin denetimi moonshot ile yapıldı (yukarıdaki düzeltme). Sınır kalkınca adımlar yeniden koşuldu.

## Planlanan iyileştirmeler

Sırası önceliğe göre. `[x]` ile işaretliler tamamlandı ve `main`'e alındı.

**Kullanım odağı (03.11):** kullanıcı ağırlıklı olarak genel bilim ve uzay konularında video üretecek: kuantum, uzay, evren, izafiyet, kara delikler, galaksiler. Yer konularına yönelik maddeler bu yüzden ertelendi.

- [x] Sıra koruma kipinde tekilleştirme (`fix/script-order-dedup`): `material._download_videos_by_script_order` artık yalnız seçilip indirmeye gönderilen URL'leri sonraki terimlerden çıkarıyor. Eskiden önceki terimin listelediği her aday siliniyordu.
- [x] CLI kaynak varsayılanı (`fix/cli-video-source-default`): `--video-source` verilmezse `[app].video_source` okunuyor. Ajan betiği (`docs/skill/mpt_agent.py::selected_video_source`) da aynı sırayı izliyor.
- [x] Yönden bağımsız kırpma (`feat/cross-orientation-crop`, ayar `crop_materials_to_aspect`, varsayılan kapalı): merkezden kırpıldığında hedef çözünürlüğü karşılayan Pexels ve Pixabay videoları yönden bağımsız kabul ediliyor. Örneğin 9:16 için 3840×2160 kabul edilir (1215×2160 kalır), 1920×1080 edilmez. Seçilen kopya, uygun olanların en küçüğü. Bu klipler `contain` kipinde de merkezden kırpılıyor. Önbellek anahtarı ayrı; kapalıyken davranış upstream ile aynı.
  - Sıralama kuralı **önce yerel yön** (`material._split_by_native_orientation`). Kırpılan adaylar yalnız yedek olarak kullanılır:
    - `match_materials_to_script` açıkken her terim önce kendi yerel adaylarını tüketir.
    - Karışık havuz kipinde terim başına dağıtım olmadığı için kural bütün havuza uygulanır: kırpılanlar ancak bütün yerel adaylar sesi karşılamazsa kullanılır. `random` kipte iki grup ayrı ayrı karıştırılır.
  - Gruplar içinde sağlayıcı sırası korunur. Gruplama önbellekten sonra yapılır.
  - Nesneye göre kırpma **ertelendi**: yeni bir bağımlılık (nesne ya da yüz tespiti modeli) ve işlem maliyeti getirir, kırpma da artık yalnız yedek olarak nadiren devreye girer.
- [x] Etiket uygunluğu ve yapay zeka dışlaması (`feat/material-relevance`; ayarlar `material_tag_relevance` = `off` | `rank` | `strict` ve `exclude_ai_generated_materials`, ikisi de varsayılan kapalı).
  - Terim ve Pixabay etiketleri normalleştirilip (küçük harf, iyelik eki ve noktalama temizliği, basit çoğul indirgeme, İngilizce durak kelimeler) karşılaştırılır. Puan = terimin içerik kelimelerinden etiketlerde geçenlerin oranı ve sayısı.
  - `rank` puana göre sıralar; `strict` ayrıca bütün kelimeleri karşılamayanları atar. Öncelik: uygunluk > yön (yerel önce) > sağlayıcı sırası. Karışık havuzda her aday, onu ilk getiren terime göre puanlanır.
  - Yapay zeka sinyali Pixabay'in yanıtındaki `isAiGenerated` alanı; "ai generated" etiketini kapsar ve etiketsiz yapay klipleri de yakalar.
  - Pexels video kaynağında etiket yok, sayfa slug'ı da her zaman bulunmuyor; Coverr'da da etiket yok. Bu yüzden iki ayar yalnız Pixabay'de etkili.
  - **PR sırası:** yığında `feat/cross-orientation-crop`'tan sonra gelir (bkz. Git akışı).
  - Uçtan uca gözlem (03.11): `rank` ve `strict` "Galata tower" için Belgrad Kulesi yerine Galata etiketli klibi seçti; 6 klipten 5'i İstanbul. Ama o klip kırpılmış ve merkez kırpma kuleyi kadrajın dışında bırakıyor. "autumn leaves" için seçilen Fuji klibi de etiket açısından tam eşleşiyor, yani terim seçimi hâlâ belirleyici.
- [x] `per_page` config'e taşındı (`feat/material-relevance`): `pixabay_per_page` (50, 3–200), `pexels_per_page` (20, 1–80). Sınır dışı değerler sınıra çekilir. Önbellek anahtarına yalnız varsayılandan farklı değer girer. Coverr'ın `page_size` değeri (20) sabit kaldı.
- [ ] Pixabay `category` **global ayar yapılmadı**: kategori bütün aramayı süzer, oysa terimler farklı kategorilere düşer (Sultanahmet "places/religion", vapur "transportation", yapraklar "nature"). Tek bir global değer başka terimlerin doğru adaylarını eler. Gerekirse terim başına ya da görev başına bir seçenek olarak düşünülmeli.
- [x] Görsel arama terimleri ve çapalar (`feat/visual-search-terms`; ayar `video_terms_style` = `legacy` | `visual`, varsayılan `legacy`; `VideoParams.video_terms_style = None` `[app]` değerini izler).
  - `visual` prompt'u stok sitelerinin etiketlediği 1-3 kelimelik somut isimler ister. Konuyu her terime ekleme kuralı kalktı; yer adı yalnız birkaç terimde (ünlü yapılar) geçebilir. Yer ve özne ayrı bir `anchors` listesi olarak döner. Sıra koruma kipindeki hedef, terim sayısı (8) ve sıra kuralı legacy ile aynı.
  - API uyumluluğu: `llm.generate_terms`'in imzası, prompt'u ve `List[str]` dönüşü değişmedi. Yeni `llm.generate_visual_terms` `(terimler, çapalar)` döndürür. Ayrıştırılamayan yanıtta legacy prompt'a düşer; sağlayıcı hatasında hemen boş döner. `/api/v1/terms` isteğe bağlı `video_terms_style` alır, yanıt yine yalnız `{"video_terms": [...]}`. Çapaları API'de almak için `/videos`'a `video_anchors` verilir ya da terimler boş bırakılır.
  - `video_anchors` (CLI `--video-anchors`, WebUI'da terimlerin altında): `material_tag_relevance` `rank`/`strict` iken sıralama puan > çapa eşleşmesi > yön > sağlayıcı sırası. Çapalar strict filtresine girmez. Üretilen çapalar yalnız terimler de üretildiyse kullanılır; kullanıcının çapaları önceliklidir.
  - Normalleştirmeye düzensiz çoğul tablosu eklendi (`leaves` → `leaf`, `people` → `person`, …).
  - **PR sırası:** yığının son dalı, `feat/material-relevance`'tan sonra gelir (bkz. Git akışı).
  - Değerlendirme (03.11, `claude_code`, 8 terim, kırpma + `rank` + yapay zeka dışlaması):
    - `visual` en üst adayın terimi tam karşılama oranını artırdı: İstanbul 5/8 (legacy 2/8), Kapadokya 4/8 (1/8), kahve 7/8 (2/8).
    - Konu uygunluğu artmadı. Kahvede eşit. Kapadokya'da daha kötü: "fairy chimneys" fabrika bacası, "cave houses" Mallorca getirdi; legacy'nin "cave houses goreme" terimi Kapadokya klibi buldu.
    - İstanbul videosunda İstanbul karesi legacy'de 3/5, visual'da 1/5.
    - Sebep: çapalar yalnız terimin kendi sonuçları içinde sıralar. "seagulls flying" gibi genel bir aramanın sonuçlarında İstanbul klibi yoksa çapa yardım edemez.
- [x] Uzay konularında stil ve yapay zeka dışlaması değerlendirmesi (03.11; 4 konu: kara delik, galaksi, görelilik, kuantum; 8 terim; kırpma + `rank`; seçim önizlemelere bakılarak E/K/H).
  - **Önerilen ayarlar:** `video_terms_style = "visual"`, `exclude_ai_generated_materials = false`, `crop_materials_to_aspect = true`, `material_tag_relevance = "rank"`. `config.toml` buna göre ayarlı.
  - **Uzay reçetesi (04.11'den beri):** yukarıdakilere ek olarak `video_source = "nasa"`, `video_source_fallback = "pixabay"` (eşik `video_source_fallback_min_relevance = 0.5`, `video_source_fallback_min_candidates = 1`), `match_materials_to_script`, `script_accuracy_rules`, `nasa_credit_overlay`, `nasa_face_filter` (kenar yüz kuralıyla) ve `nasa_text_filter` (ikisi de varsayılan açık, `face` extra'sı gerekir). CLI: `--video-source nasa --video-source-fallback pixabay` (metin filtresi için ayrıca `--nasa-text-filter/--no-nasa-text-filter`); WebUI: NASA ayarlarında "Sonuç yetersizse Pixabay'e geç". `config.toml`'daki `video_source` kullanıcının seçimi (şu an `pixabay`), değiştirilmedi. Hız ayarları (05.11'den beri, `config.toml`'da): `final_render_engine = "ffmpeg"`, `material_concurrency = 3`, `video_clip_concurrency = 3`; `video_codec` boş (libx264; QSV ölçüldü ve seçilmedi, bkz. hızlandırma maddesi).
  - Konuyla ilgili seçim oranı: visual + dışlama kapalı %53; visual + açık %44; legacy + kapalı %44; legacy + açık %41.
  - **Dışlama uzayda neden kapalı:** adayların ~%29'unu eliyor ve isabet 9 puan düşüyor, çünkü en uygun uzay klipleri (süpernova, bulutsu) çoğunlukla yapay zeka üretimi. Yerlerine gerçek gözlem değil yine CGI geliyor (dışlamayla 64 seçimin 47'si CGI). Fiziksel olarak imkansız tasvirler de yapay zeka bayrağı taşımayan CGI'larda çıktı: galaksi boyutunda gezegenler, okyanus üstünde Dünya.
  - Pixabay zayıf: 75 seçimin yalnız biri gerçek astronomik gözlem (Ay). Soyut terimlerde isabet %25, somutlarda %63. "star", "particles" gibi kelimeler Noel süslerine kayıyor.
  - Soyut konularda çapalar işe yaramıyor ("time dilation, relativity" hiçbir etiketle eşleşmedi).
- [x] Script'lerde bilimsel doğruluk kuralı (`feat/script-accuracy-rules`; ayar `script_accuracy_rules`, varsayılan kapalı, `config.toml`'da açık).
  - Açıkken `DEFAULT_SCRIPT_SYSTEM_PROMPT`'un kısıt listesine 9-11 numaralı üç kural eklenir: yerleşik bilgi ve belirtilmiş belirsizlik, yaygın yanılgılardan kaçınma (karşılaştırmada bütün etkiler), mecazı mecaz tutma. Kapalıyken prompt ve çağrılar upstream ile birebir aynı.
  - Özel sistem prompt'unda kurallar ayrı bir "## Scientific Accuracy" bölümü olarak eklenir; prompt'ta zaten geçen kural tekrarlanmaz. Gerekçe: `build_script_prompt` değiştirilebilir üretim kurallarını, her görevde seçilen ayarlardan (konu, dil, paragraf) ayırıyor; bu ayar da görev başına seçildiği için özel prompt'ta sessizce kaybolmamalı. Ek gereksinimler (`video_script_prompt`) kuralların ardından gelmeye devam eder.
  - Ölçüm (03.11, aynı 4 konu): iddiaların 9 D / 13 B / 4 Y'si kurallarla 17 D / 3 B / 1 Y oldu. Önceki 4 yanıltıcı iddianın hepsi gitti: "nokta" yerine olay ufku, yörünge saatinde iki etki, 2 trilyon galaksi ve "kırılamaz şifreleme" kaldırıldı. Yeni Y: Samanyolu-Andromeda birleşmesi "bekleniyor" deniyor, oysa 2025'teki bir çalışma olasılığı ~%50 buluyor.
  - İlk ifadede uzunluk korundu (58-68 kelime). Ama ton temkinli ve teknikleşti: 4 metnin 2'si belirsizlik cümlesiyle bitti, galaksi metnine "şehir benzetmesi yalnızca bir benzetme" gibi bir üst yorum girdi, çekici açılış 2/4'ten 1/4'e düştü; somut örnek 2/4'te kaldı.
  - Kurallar yeniden yazıldı (kapsam aynı, 3 kural):
    - belirsizlik yalnız anlamı değiştirdiği yerde, kısa ve cümle içinde; genel uyarıyla bitirme yok; çekici açılış ve somut örnekler korunur;
    - yanılgı kuralı ve örnekleri aynı;
    - benzetme ifadeyle kurulur ("like a city"), üstüne yorum yapılmaz.
  - İkinci ölçüm: 15 D / 3 B / 1 Y. Belirsizlik sonu 0/4, benzetme yorumu 0/4, çekici açılış 2/4, somut örnek 4/4 (Cygnus X-1, Andromeda'nın uzaklığı, GPS'te günde 38 µs, fotonlarla yüzlerce km). Kalan Y aynı: Andromeda birleşmesi "düşünülüyor" deniyor, ~%50 olasılık söylenmiyor.
- [x] Sıra koruma kipinde eşit aralıklı terim seçimi (`fix/script-order-even-spread`).
  - Sorun: `task.generate_terms` (ve WebUI'daki iki çağrı) sıra koruma açıkken sabit 8 terim ister; terimler sesten önce üretilir. İndirme toplam klip süresi sesi aşınca duruyordu, son terimler hep düşüyordu: 26,9 sn'lik K1'de "black hole" kullanılmadı.
  - Düzeltme: `material._download_videos_by_script_order`'da k = ⌈ses / max_clip_duration⌉, aday bulunan terim sayısı n'den küçükse ilk turda `_even_spread_indices` ile seçilen k terim kullanılır (indeks round(i·(n−1)/(k−1)); ilk ve son her zaman dahil, k = 1'de ilk terim). Kalan terimler sıralarıyla arkaya yedek olarak eklenir.
  - Yedekler kısa klipler daha fazla parça istediğinde ilk turda kullanılır; indirme başarısız olursa ikinci turda önceki terimlerin ikinci adayından önce gelir.
  - k ≥ n'de ve karışık havuz kipinde davranış aynı. Tekilleştirme, önce yerel yön ve uygunluk sıralaması değişmedi.
  - Tam katsayılı seste (ör. 25 sn / 5 sn) döngü toplamın sesi kesin aşmasını beklediği için bir yedek fazladan iner. Birleştirme sesin sonuna güvenlik payı eklediğinden bu yedek sonda en fazla bir kesir saniye görünebilir.
  - Uçtan uca (03.11, aynı K1 metni ve terimleri): kullanılan terimler starry sky, giant star, burning fire, collapsing building, swirling vortex, black hole. "black hole" 25,0 sn'den sona kadar (≈1,9 sn) görünüyor. Son terim, ses kalanı kadar ekranda kalıyor.
- [ ] **Ertelendi:** terim sayısını metin uzunluğundan tahmin etmek (kelime ÷ konuşma hızı ÷ klip süresi). Eşit aralık seçimi düşen terim sorununu çözdü; bu yalnız gereksiz terim üretimini ve aramayı azaltır, dile ve ses hızına bağlı tahmin hatası taşır.
- [ ] **Ertelendi:** terimleri cümlelerin ses içindeki zamanına göre dağıtmak. Altyazı zamanları malzeme adımında elde, ama terim-cümle eşleşmesi, LLM prompt'u, malzeme ayırma ve birleştirme değişir. Büyük ve riskli.
- [x] NASA görsel kaynağı, çekirdek (`feat/nasa-source`, 04.11): `video_source = "nasa"`, yalnız görseller, anahtar gerekmez. API: `https://images-api.nasa.gov` (`/search`, `/asset/{id}`, `/metadata/{id}`); hız sınırı başlığı yok.
  - Modül `app/services/nasa.py`. `search_images` önce arama verisiyle metin filtrelerini uygular, kalanların ilk 15'inin asset ve metadata'sını 4 iş parçacığıyla çeker. Her terim için `nasa image search: … kept=…, skipped={…}` satırı loglanır.
  - Filtre katmanları (listeler ve eşikler modülün başında sabit; 32 terimin ilk sayfasında 1055 öğe, 481'i gözle etiketlenerek seçildi):
    - `third_party_copyright`: metadata'daki XMP/IPTC/EXIF hak alanı NASA dışını gösteriyorsa.
    - `identifiable_person_risk`, sırayla:
      - `NHQ` kimliği doğrudan dışlanır (70'te 69 etkinlik fotoğrafı).
      - Başlıkta ya da anahtar kelimede kişi terimleri (astronot, mürettebat, brifing, portre…). ISS'in "photographed by the Expedition N crew" ve "Crew Earth Observations" kalıpları sayılmaz (10'da 10 Dünya/yıldız fotoğrafı).
      - Başlıkta, anahtar kelimede ya da açıklamada olay dili: speaks, panelist, guests, reporters, "during an … event", mannequin… (59'da 59 kişi). HTML ve bağlantılar önce ayıklanır.
      - Merkez kimlikleri (KSC, JSC, AFRC, GRC, ED/EC, `NNpd`…) yalnız açıklamada rol sözcüğü (astronaut, commander, pilot, physicist, principal investigator…) ya da "Dr. Ad" varsa. Kimlik tek başına kişi demek değil (181'de 87); eski `KSC-…-PH-/-VP-` kuralı tanınabilir kişi elemediği için kaldırıldı.
      - Anahtar kelimelerin en az 2'si ve %60'ı kişi adına benziyorsa.
    - `text_graphic_risk`: başlıkta ya da anahtar kelimede logo, amblem, diyagram, grafik, spektrum…; "This spectrum shows…" gibi açılışlar; açıklamada insignia, meatball, mission/crew patch, logo.
    - Yüz kontrolü (`nasa_face_filter`, varsayılan açık): `face` extra'sı (`opencv-python-headless>=4.8`) ve depodaki YuNet modeli (`cv2.FaceDetectorYN`) kırpılmış, küçültülmüş görselde 720 px genişlikte çalışır. Eşik: skor ≥ 0,75, yüz ≥ 24 px.
      - Ölçüm: kişi içeren 129 fotoğrafın kırpımında 93 tespit, 319 astronomi görselinde 0 yanlış pozitif. OpenCV'nin Haar sınıflandırıcısı aynı görsellerde %6–17 yanlış pozitif verdi; OpenCV 5'te Haar hiç yok.
      - Süre: görsel başına medyan ~130–250 ms.
      - Extra yoksa bir kez uyarı loglanır ve yalnız metin filtreleri çalışır. Hazırlanan görsel önbelleklenir; kontrol ve render aynı dosyayı kullanır.
  - Uygunluk yalnız anahtar kelimeler ve başlıktan: 4+ kelimelik anahtar kelimeler ve kurum adlarıyla biten anahtar kelimeler (Laboratory, Center, Field, University…) etiket sayılmaz, başlık temizlenip eklenir. İstisnalar: "deep field", "magnetic field", "star field"… Bu sayede "star field" artık "All-Star Target Field"a, "physics laboratory" "Johns Hopkins APL"ye puan vermez. Açıklama uygunluğa girmez.
  - Varyant seçimi: orig/large/medium/small içinden merkez kırpma sonrası hedefi (9:16'da 1080×1920) zoom payıyla (1 + 0,03·klip süresi) karşılayan en küçük varyant; payı karşılayan yoksa hedefi karşılayan. Hiçbiri yoksa aday `resolution` gerekçesiyle elenir; hiçbir zaman büyütülmez. Boyut metadata'da yoksa ilk 1 MB'tan okunur. Yatay arşiv yüzünden 9:16'da çoğu zaman `orig` iner.
  - Klip: `nasa.save_image_clip` görseli indirir, hedef oranda ortadan kırpar, zoom payıyla küçültür (LANCZOS) ve `video.render_image_zoom_video`'ya verir. `material.download_videos` bunu `save_material` kancasıyla çağırır; kırpma kuralı, `MATERIAL_TAG_SOURCES` uygunluk sıralaması, sıra koruma ve eşit aralık, tekilleştirme NASA'ya da uygulanır. Kredi ve `images.nasa.gov/details/{id}` `script.json`'daki `material_sources`'a yazılır.
  - Kullanım koşulları (https://www.nasa.gov/nasa-brand-center/images-and-media/): içerik ABD'de genellikle telif dışı; NASA amblemi ve logoları korumalı; tanınabilir kişiler ticari kullanımda kısıtlı; NASA'nın onayladığı izlenimi verilmemeli; üçüncü taraf içeriği sahibinin adıyla işaretli; kaynak olarak NASA belirtilmeli. Sayfadaki "Super Intelligence" bölümü (EO 14434: SI görsellerinde amblem yok, SI ürünlerinde "according to NASA" yasak) bu uygulamaya nasıl uygulanacağı açısından hukuki yorum ister.
  - Uçtan uca (04.11, uzay değerlendirmesinin K1/K2 metni ve visual terimleri, kırpma + rank + sıra koruma, 9:16): K1 472 sn / 109 MB yeni dosya, K2 672 sn / 123 MB (aynı K2 Pixabay'le 779 sn / 197 MB). K2'de "night sky timelapse" NASA'da 0 sonuç verdi. 5'er karede gerçek gözlem payı NASA'da K1 4/5, K2 4/5, Pixabay'de 0/5. Konuya uygunluk K1'de eşit (2/5 + 1 kısmi), K2'de NASA önde (4/5'e karşı 1/5 + 3 kısmi). En büyük yakınlaşmada pikselleşme yok; Voyager taramasında film kenarı ve işaret noktaları, Spitzer görüntüsünde doğal yumuşaklık var. `resolution` elemesi terim başına ayrıntısına bakılan 15 adaydan 0–11'ini (çoğunlukla 5–10) düşürüyor. Klipler durağan görsel üstünde yavaş yakınlaşma; Pixabay videolarındaki hareket yok.
- [x] NASA kişi ve amblem filtresi açıkları (`feat/nasa-source`, 04.11): yukarıdaki filtre katmanları ve yüz kontrolü.
  - Kredi satırı paragraf ya da cümle sonunda kesilir ("D. Coe" gibi baş harfler korunur), bütünü saran parantez atılır.
  - Ölçüm (32 terim, filtrelerden önce ve sonra seçilecek ilk 3 aday, hazırlanmış kırpımlar gözle):
    - Yakalanan: 8 tanınabilir kişi, 1 tanınmayan kişi, 1 manken.
    - Sonda kalan kaçaklar: tanınabilir kişi 0; tanınmayan kişi 4 (arkadan, çok küçük, temiz oda tulumu); görüntüye işlenmiş metin ya da grafik 8 (açıklama yazısı, etiket, diyagram).
    - Gereksiz eleme yok. Metin filtreleri terim başına ortalama 0,6 aday düşürüyor; en çok "physics laboratory" (5'ten 1'e).
    - İlk sayfalarda kişi elemesi 38'den 126'ya çıktı; "starry sky", "earth from space" gibi terimlerde eskiden elenen 14 öğe (ISS mürettebat fotoğrafları, KSC aurora) artık geçiyor.
  - `NHQ202606040047` NHQ kimliğiyle elenir (ad kuralı 6/9 ve "speaks" da yakalıyor); kırpımda yüz bulunmuyor, sahnedeki yüzler küçük.
  - `AFRC2019-0248-01` (manken) "mannequin" ile elenir; YuNet yan yatmış manken yüzünü bulmuyor.
  - Uçtan uca (04.11, K2 aynı metin ve terimlerle, `--video-source nasa`): "deep space telescope" ilk sayfasından 23 kişi fotoğrafı eleniyor ve artık `PIA09967` seçiliyor (Spitzer, "Water Early Journey in a Solar System Artist Concept": bir sanatçı çizimi, gözlem değil). 5 karede kişi ya da amblem yok; diğer 6 klip öncekiyle aynı. Süre 748 sn.
  - Kalan açık: görüntüdeki metin ve grafikleri yalnız metin verisi yakalıyor.
- [x] NASA kullanıcıya hazır (`feat/nasa-source`, 04.11):
  - Arama önbelleği: NASA kayıtlarının anahtarına her zaman `nasa.FILTER_VERSION` (filtreler değişince 1 artırılır), `nasa_face_filter` ve `nasa_media_types` girer (`material_cache`'te `variant`). Varsayılan ayarlarda da girer; yoksa sürüm eklenmeden önceki eski filtre sonuçları aynı anahtarla kullanılmaya devam ederdi. Diğer kaynakların anahtarı değişmedi.
  - Atıf: NASA görseli kullanıldıysa görev klasörüne `credits.txt` yazılır (Türkçe/İngilizce başlık, her görselin kredisi ve `images.nasa.gov/details/{id}` bağlantısı, "Görüntüler: NASA ve ortakları" ve "onaylanmamıştır" satırı). Uygulamanın tek sosyal metni çapraz paylaşımdaki başlık/açıklama (`auto_upload`); oraya ve YouTube açıklamasına bir atıf satırı eklenir. `script.json` ve `subtitle.srt`'ye dokunulmaz.
  - Ekran üstü atıf: `nasa_credit_overlay` (varsayılan kapalı; `VideoParams`, CLI `--nasa-credit-overlay`, WebUI'da NASA ayarlarında). Son 2 saniyede altyazının karşı tarafında (altyazı üst yarıdaysa altta, değilse üstte) yarı saydam zeminli küçük bir satır: "Görüntüler: NASA, STScI ve diğerleri" (tr-* videolar) ya da "Images: …". Ajanslar kredilerdeki kısaltmalardan, NASA önde, en fazla 2 ad.
  - Metin prompt'u: kaynak `nasa` iken `llm.SOURCE_ATTRIBUTION_RULES["nasa"]` kuralı eklenir ("according to NASA", "NASA says", "NASA presents" yok). Varsayılan prompt'ta doğruluk kurallarından sonra numaralanır (açıksa 12., kapalıysa 9.); özel prompt'ta ayrı bir "## Attribution:" bölümü olarak eklenir (zaten içeriyorsa eklenmez). Görev, API (`VideoScriptRequest.video_source`) ve WebUI (betik paneli kaynak seçiminden önce çalıştığı için kaynağı bekleyen yapılandırmadan okur) iletir.
  - WebUI: stok listesinde "NASA (görsel, anahtar gerekmez)"; ayarlar her zaman yapılandırılmış sayılır (`settings_providers`); NASA ayarlarında yüz filtresi (extra yoksa uyarı), ekran üstü atıf, kullanım koşulları notu ve bağlantısı; üretimden önce yüz tespiti eksikse bilgi mesajı. İlk en-boy Coverr gibi yatay; malzeme eşzamanlılığı seçeneği. 10 yeni anahtar 14 dilde.
  - Uçtan uca (04.11, K3 "Görelilik kuramına göre zaman neden yavaşlar?", metin baştan üretildi, doğruluk kuralları açık, visual terimler, ekran üstü atıf açık): metinde kuruma atıf yok. 88 sn, 18 klip, `credits.txt` 18 satır. Son 2 saniyede üstte "Görüntüler: NASA, JPL ve diğerleri" okunaklı ve altyazıyla çakışmıyor. Toplam 1941 sn. 5 karenin 2'sinde görüntüye işlenmiş yazı var (Egg Nebula başlığı, "Orbit 99,999"); biri altyazıyla üst üste biniyor (OCR maddesi).
- [x] NASA kredi temizliği (`feat/nasa-source`, 04.11; `FILTER_VERSION = 3`): kredi hashtag, @hesap, "Follow us"/"Read more" ve bağlantıdan önce kesilir. Kredi NASA, bir NASA merkezi (GSFC, JSC, KSC, MSFC, Ames…) ya da `FREE_USE_PARTNERS`'tan birini (ESA/Hubble ve ESA/Webb CC BY 4.0, JPL, STScI, Chandra CXC/SAO; kaynak bağlantıları sabitin yanında) içermiyorsa aday `third_party_credit` ile elenir. esa.int'in genel görselleri ("European Space Agency (ESA) - D. Ducros") ticari kullanımda ESA'nın yazılı iznini istediği için listede yok ve elenir. Fotoğrafçı alanındaki kişi adları (astronot, NASA fotoğrafçısı) "NASA/Ad" yazılır; "SpaceX" gibi kurum adları listeden geçmek zorunda. Ölçüm (32 visual terim + iki sağlayıcının Zombi terimleri, 46 terim): 12 görsel elendi (JAXA/ESA astronot fotoğrafları, ESA Herschel/Planck/XMM-Newton, SDSS, 2MASS, telifli UH IfA çizimi, Space Dynamics Laboratory). Not: ESA/Hubble görsellerinde CC BY 4.0, YouTube'da kredinin görüntünün üstünde görünmesini istiyor; ekran üstü atıf kısaltılmış ("NASA, JPL ve diğerleri"), tam kredi `credits.txt`'te.
- [x] Görsele işlenmiş yazı ve grafik tespiti: aşağıdaki "NASA metin filtresi" maddesi (`feat/nasa-text-filter`). 4. madde ölçümünde ilk 3 adaylarda kalan 8 kaçağın hepsi bu türdendi.
- [ ] **Ertelendi:** Docker imajına `face` extra'sı. `listen_host` ortam değişkeniyle birlikte Docker adımında yapılacak; şimdilik imajda yalnız metaveri metin filtreleri çalışır (yüz kontrolü ve önizleme metin filtresi OpenCV ister).
- [x] Desteklenmeyen noktalama (`fix/normalize-unicode-punctuation`, 04.11): `llm._normalize_text_response` U+2010/U+2011/U+2012'yi "-", U+00A0/U+202F'yi boşluğa çevirir (bütün sağlayıcılar). Ölçüm: STHeitiMedium'da U+2011, U+2012, U+00A0, U+202F; MicrosoftYaHeiBold'da U+2011, U+2012, U+202F glifi yok (altyazıda kutu). En/em tire ve U+2015 iki yazı tipinde de var, değişmez; tırnaklar ve harfler korunur.
- [x] CLI'dan görev başına sağlayıcı (`feat/cli-llm-override`, 04.11): `--llm-provider` ve `--llm-model` yalnız o süreçte `config.app`'i değiştirir; model, kayıt defterindeki `<sağlayıcı>_model_name` alanına gider (`ollama_model_name`, `claude_code_model_name`…). Geçersiz sağlayıcıda çıkış kodu 2 ve geçerli adların listesi. `mpt_agent.missing_config` bayrağı dikkate alır.
- [x] Ollama ölçümü (04.11, `gpt-oss:120b-cloud`, K1–K4 + "Zombi yıldızlar", doğruluk kuralları açık): Ollama `/v1` üzerinden çalışıyor; gpt-oss düşünmeyi ayrı `reasoning` alanında gönderiyor, `content` temiz. Terim JSON'u iki sağlayıcıda da 10/10 ilk denemede. gpt-oss hızlı (metin ort. 7 sn, `claude_code` 15 sn) ama doğrulukta zayıf (D/B/Y/X 10/6/5/4, `claude_code` 22/6/0/0); Türkçe dilbilgisi hataları, U+2011 tireleri. **Metin için `claude_code` önerilir.** Donanım (i5-9600K, 32 GB, GPU yok) için yerel adaylar (`gpt-oss:20b` 13,8 GB, `qwen3:30b` 18,6 GB, `gemma3:12b` 8,2 GB) ölçülmedi.
  - Uçtan uca "Zombi yıldızlar" (04.11, `main` 9253c09, NASA kaynağı, sağlayıcı bayrakla; `config.toml` iki üretimden sonra aynı): `--llm-provider ollama --llm-model gpt-oss:120b-cloud` 1221 sn, 94 kelime, 50,7 sn ses, D/B/Y/X 1/4/1/2 (PSR J0205+6449'u Cassiopeia A'ya koyuyor, oysa 3C 58'de; pulsarı zombi yıldız sayıyor; "yeniden parlayabilir" yanıltıcı). `--llm-provider claude_code` 847 sn, 85 kelime, 42,6 sn ses, 6/0/0/0 (kısmi patlama, beyaz cüce, SN 2012Z). İkisinde de altyazıda glifi olmayan karakter yok; `credits.txt` (11 ve 9 satır) hashtag ve NASA dışı kredi içermiyor. Karelerde görüntüye işlenmiş yazı altyazıyla çakışıyor (Hourglass Nebula başlığı; Spitzer'in Cas A toz spektrumu grafiği; OCR maddesi).
- [x] Harf arasındaki kesme işareti (`fix/normalize-unicode-punctuation`, 04.11, 743c500): U+2018, U+2019, U+201C ve U+201D iki varsayılan yazı tipinde (STHeitiMedium, MicrosoftYaHeiBold) CJK genişliğinde çiziliyor (60 punto, 60 px; ASCII `'` 12–18 px); gpt-oss Türkçe ek kesmesinde U+2019 kullanıyordu ("Milky Way’in" altyazıda "Way’ in" gibi görünüyordu). `llm._LETTER_APOSTROPHE_RE` iki Unicode harf (Türkçe harfler dahil) arasındaki U+2019 ve U+02BC'yi `'` yapar. Harfle çevrili olmayan U+2019 (tek tırnak kapanışı, "1990’lar" gibi rakamdan sonraki) ve diğer tırnaklar değişmez; JSON terimleri ayrıştırılmaya devam eder.
- [ ] **Ertelendi:** Ollama zaman aşımı ayarı (`ollama_request_timeout`). Şu an SDK varsayılanı 600 sn × 3 deneme, uygulama 5 deneme yapıyor; yalnız yerel CPU modeli kullanılırsa gerekir.
- [x] Terim başına yedek kaynak (`feat/source-fallback`, 04.11, 1aa1e33). Ayar biçimi: birincil kaynak `video_source` olarak kaldı; yeni `video_source_fallback` (görev başına, `VideoParams`/API, CLI `--video-source-fallback`, WebUI NASA ayarı; boş = yedek yok) ikinci stok kaynağını verir (`pexels`, `pixabay`, `coverr`, `nasa`; birincil de bunlardan biri olmalı). Liste tabanlı `video_source` değerlendirildi ve seçilmedi: task.py'deki 18 `video_source ==/in` kontrolü, API şeması, CLI seçenekleri, WebUI seçim kutusu ve kayıtlı görevler değişirdi.
  - Ölçüt (`material._search_with_fallback`): birincilin filtre ve sıralamadan sonraki adaylarından uygunluk oranı ≥ `video_source_fallback_min_relevance` olanlar sayılır (`material_tag_relevance = off` ya da etiketsiz kaynakta bütün adaylar). Sayı `video_source_fallback_min_candidates`'ten azsa terim yedekte aranır. Yedeğin uygun adayı daha çoksa (ya da birincilin hiç adayı yoksa) terimin bütün adayları yedekten gelir, yoksa birincilinkiler kalır. Bir terim iki kaynağı hiç karıştırmaz. Yüz kontrolü indirme anında olduğu için ölçüte girmez.
  - Yedek kendi önbelleğini ve kendi desteklediği kırpma, uygunluk, YZ dışlaması ve `per_page` ayarlarını kullanır. Karar yalnız iki filtrelenmiş aday listesine bağlı, önbelleğe yazılmaz; önbellekten okununca aynı karar çıkar. Anahtarı yoksa yedek kapanır ve görev başına bir kez uyarı loglanır.
  - Sıra koruma ve eşit aralık terim düzeyinde aynı. Karışık havuz kipinde terimlerin (farklı kaynaklardan gelen) adayları birleştirilip birlikte sıralanır: uygunluk > çapa > yön > terim sırası.
  - Atıf: `credits.txt` her satırda kaynağı yazar (`[NASA] NASA/JPL-Caltech`, `[Pixabay] <yükleyen>` + sayfa bağlantısı); son satır "NASA ve ortakları, Pixabay" olur. Pixabay lisansı (https://pixabay.com/service/terms/, 20.10.2026) ticari kullanıma izin veriyor ve atıf istemiyor ("You do not need to credit Pixabay or the contributor… but it is certainly appreciated"); bu yüzden NASA görseli olmayan görevde `credits.txt` yine yazılmaz (`script.json`'daki `material_sources` sayfaları zaten tutuyor). Ekran üstü atıf yalnız NASA görseli varsa görünür.
  - Eşik ölçümü (54 terim: 32 visual + 4 Zombi listesi; NASA kırpma + rank, en üst adaylar önizlemeden E/K/H): yalnız NASA puanı 61 (E=2, K=1, H/boş=0; 24 E, 13 K, 11 H, 6 boş). Eşik 0,5 ile en az 1 aday: 17 terim yedeğe geçiyor, 9'u iyileşiyor, 3'ü kötüleşiyor, puan 70. En az 2 aday: 24 geçiş, 9 kötüleşme, puan 62; NASA'da tek uygun adayı olan terimlerde o görsel çoğunlukla doğru, Pixabay ise terimin yarısını karşılıyor ("white dwarf" → beyaz kuş, "expanding shell" → kaplumbağa). Eşik 0,25/0,75 en az 1'de 16/20 geçiş. Kalan 3 kötüleşme (swirling vortex, expanding shell, astronomer workstation): NASA'nın etiketi terimi içermiyor ama görsel konuya uygun.
  - Uçtan uca (04.11, `main` 4769a29, `claude_code`, NASA reçetesi, kontur 3, klip 5 sn; yedekli koşu aynı metni, terimleri ve çapaları kullandı, yalnız malzeme değişti). Klipler her 5 saniyenin ortasından E/K/H:
    - K3 (69 sn, 8 terim): yalnız NASA 1511 sn, 1 boş terim ("spaceship flyby"), 14 klip 4/7/3. Kişi: hangarda küçük figürler, maskeli teknisyen; bir klipte kenarda yarım yüz ve "Rocket Lab" yazısı (YuNet kenardaki yarım yüzü bulmadı). Yedekli 1729 sn: "light beam mirror" (NASA 0/3 uygun), "spaceship flyby" (0/0) ve "ticking clock" (0/5) Pixabay'e geçti, boş terim 0, 9 NASA + 6 Pixabay klip, 5/7/2. Süre +218 sn: Pixabay'in 4K yatay kopyaları kırpılarak işleniyor.
    - Zombi yıldızlar (91 kelime, 44 sn): yalnız NASA 1036 sn, boş terim 0, 9 klip 6/3/0, kişi ve amblem yok. Yedekli 977 sn: yalnız "binary star system" geçti (NASA 0/7; çift yıldız çizimi E idi), yerine Pixabay CGI sarmal gökada (K) geldi, 5/4/0.
    - Dört görevde `credits.txt` satırları etiketli; NASA dışı kredi ve hashtag yok, Pixabay satırlarında sayfa var. Ekran üstü atıf yedekli videolarda da göründü. Kesme işareti: K3 altyazısında "Dünya'nın yüzeyinde" boşluksuz.
    - Sonuç: yedek boş terimleri dolduruyor ve terimi hiç karşılamayan NASA adaylarının yerine geçiyor. Ama NASA'nın etiketi terimi tutmayan doğru görseli de gidebiliyor ("binary star system"). Uzay reçetesinde açık tutmak yararlı; daha seçici bir ölçüt için terimin alan kelimesine göre ağırlık (ör. "star", "galaxy" eşleşmesi) sonraki adımda değerlendirilebilir.
- [x] Görsel içerik taraması fizibilitesi (05.11, ölçüm dosyaları scratchpad `vision/`): 195 öğelik doğru cevap seti (step4'ün 86 adayı, 54 terimin en üst NASA ve Pixabay adayları, uçtan uçtan bilinen 7 örnek). A: OpenCV metin tespiti (PP-OCRv3 DB seçildi; EAST 96,7 MB, GPL-3.0 kodlu ve astronomide çok yanlış pozitif). B: görsel LLM (claude CLI `--input-format stream-json` + base64 görsel, araçlar kapalı; Ollama `gemma4:31b-cloud`): yazı 12/12, kişi 16/16, yanlış alarm astronomide 0; uygunluk iki modelde de %68.
- [x] NASA yüz kenarı kuralı (`fix/nasa-face-edge`, 05.11, bdf62be): kırpmada yüz yoksa orijinalde kırpma ve iki yanındaki yarım kırpma genişliği aynı ölçekte taranır; kırpmaya en az %15'i giren ve skoru ≥0,85 olan yüz de adayı eler (`nasa.FACE_EDGE_*`). Ölçüm (138 tanınabilir kişi, 379 astronomi): şimdiki 100 → 106, astronomi yanlış pozitif 0. Skor 0,75'te bir kara delik çiziminin parıltısı, %20'de Rocket Lab'deki yarım yüz (%19,8) sorun çıkarıyordu. Ayar yok, `nasa_face_filter` altında.
- [x] NASA metin filtresi (`feat/nasa-text-filter`, 05.11, 9acf348; ayar `nasa_text_filter`, varsayılan açık): `app/services/text_detection/` PP-OCRv3 DB (OpenCV Zoo, 2,4 MB, Apache-2.0, tembel yükleme; OpenCV yoksa bir kez uyarı). İndirmeden önce `~small` önizlemede yazı satırları aranır (skor ≥0,7, en-boy ≥2,5); kırpma bölgesi önizlemede hesaplanır, kesişen satır adayı eler, tamamen dışındaki elemez. Terim başına sıralama sırasıyla 3 aday geçene kadar kontrol edilir (sıra koruma kipinde bir terim 1–3 klip kullanıyor; yalnız ilk geçeni bulmak K3'te terimin üçüncü klibi olan Egg Nebula'yı kaçırırdı). Önizleme alınamazsa aday kalır. `FILTER_VERSION` 4, önizleme adresi önbellek beyaz listesinde, ayar önbellek anahtarında. CLI `--nasa-text-filter`, WebUI NASA ayarı (14 dil).
  - Doğrulama (fizibilite setinden bağımsız; 9 yeni uzay konusu, 61 terimin ilk 5 adayı, 230 önizleme; claude ve gemma4 ön etiketi, birinin yazı dediği 69 aday ve 30 rastgele temiz aday gözle kontrol edildi, 30'un hepsi temizdi):
    | etiket | kural sonucu |
    |---|---|
    | kırpmada işlenmiş yazı (başlık, açıklama, zaman damgası, eksen) 28 | 26 yakalandı (%93); kaçan 2: kısa etiketler ("171 Å / 335 Å", "IRAC"; kutu en-boy oranı ~2,0) |
    | sahne yazısı dahil (tabela, logo, cihaz etiketi) 40 | 33 (%82) |
    | yazısız 190 | 6 yanlış alarm (%3,2), hepsi tesis/donanım fotoğrafı |
    | yazısız astronomi 129 | 0 (%0) |
    | terim başına | ilk 5 adaydan ortalama 0,64 elendi; 61 terimin 2'sinde bütün adaylar elendi (grafik ağırlıklı terimler, yedeğe geçer) |
  - Varsayılan ölçütü (duyarlılık ≥%85, astronomide yanlış alarm ≤%2) işlenmiş yazıda sağlandı; eşik yeniden ayarlanmadı (yarım setlerdeki ızgara `textval/rapor.md`'de). Görsel başına medyan 477 ms (önizleme, girdi uzun kenarı 1312).
  - Uçtan uca (05.11, `main` bfbcf9d, `claude_code`, uzay reçetesi + Pixabay yedeği, önceki yedekli koşularla aynı metin, terim ve çapalar): K3'te metin filtresi S69-19190'ı (Apollo üzerindeki "UNITED STATES" yazısı) ve KSC-20180412-PH_CSH01_0032'yi ("Rocket Lab" yazısı; önceki yalnız-NASA koşusundaki yarım yüzlü işçi fotoğrafı, yüz kenarı kuralı da yakalıyor) eledi. Yerine gelen PIA12843 (Titan'ın iç yapısı çizimi) yazısız ama "spacecraft interior" terimine uymuyor. Zombi'de PIA16697 elendi, kullanılan klipler değişmedi. Yüz filtresi ikisinde de öncekiyle aynı adayları eledi (6758008, PIA23644). İki videoda altyazıyla çakışan işlenmiş yazı ve tanınabilir kişi kalmadı. 5'er kare öncekilerle aynı. Süre K3 1415 sn (önceki 1729), Zombi 1077 sn (önceki 977); fark çoğunlukla önbellekten (`FILTER_VERSION` 4 NASA aramalarını yeniledi, Pixabay kopyaları önbellekteydi).
- [x] Üretim süresi profili (05.11, ölçüm dosyaları scratchpad `profile/`; uygulama koduna dokunulmadı): sıcak önbellekli "Zombi yıldızlar" (44 sn, 9 klip) 1017 sn sürüyordu. Payları: MoviePy son render'ı 545 sn (~1,1 çekirdek; Python'da kare kare birleştirme), her görevde yeniden üretilen yakınlaştırma klipleri 230 sn, birleştirme 215 sn (normalleştirme 161 + concat 54). Bir NASA klibi 4 kez kayıplı kodlanıyordu (1242×2208 yakınlaştırma → 1080×1920 normalleştirme → concat → son render), Pixabay klibi 3 kez. Deneyler: aynı katmanlarla ffmpeg overlay son render'ı 15 sn'de 154 → 16 sn (SSIM 0,984); libx264 `-threads 2` 10,3 sn, otomatik 6,1 sn; h264_qsv 2,1–2,4 sn (SSIM 0,945–0,962); 2 kodlamalı zincir (yakınlaştırma doğrudan 1080×1920, concat `-c copy`) 4 kodlamaya göre hem hızlı hem kaliteli (SSIM 0,951'e karşı 0,929); PIL draft zaten verimli.
- [x] Düşük riskli hızlandırmalar (05.11; dallar ve bağımlılıklar Git akışı'nda):
  - Yakınlaştırma klibi yeniden kullanımı (`perf/image-clip-reuse`): `render_image_zoom_video` çıktısının adı çözülmüş kaynak piksellerinden ve sonucu değiştiren her ayardan (süre, hareket, yakınlaştırma hızı, `output_size`, `fit_mode`, fps, codec, preset, ffmpeg parametreleri, `_IMAGE_CLIP_RENDER_VERSION`) türetilir; yayımlanmış, boş olmayan dosya varsa yeniden kodlanmaz. Klip geçici `.image-zoom-` adıyla yazılıp `os.replace` ile yayımlanır; Windows değiştirmeyi reddederse başka görevin yayımladığı aynı klip kullanılır. Klip görselin yanında durur (NASA için `storage/cache_videos`; 5 sn'lik 1080×1920 klip ~1,7 MB, Zombi'nin 9 klibi 15,4 MB). `cache_manager` yalnız `vid-*.mp4`'ü temizler; yakınlaştırma klipleri NASA görselleri gibi kendiliğinden silinmez. Sürüm öncesi adla kalan 69 klip (130 MB) artık okunmuyor, elle silinebilir.
  - Doğrudan hedef boyut (aynı dal ve `perf/nasa-clip-size`): `output_size` verilince klip doğrudan video boyutunda üretilir; her kare kaynaktaki o anki yakınlaştırma bölgesini tek adımda küçültür (PIL `resize(box=…)`), `fit_mode` `combine_videos`'un cover/contain sonucuyla aynı (küçük boyutlarda kare farkı 1,0–2,1/255). NASA'da yakınlaştırma payı hazırlanmış görselde kalır (1242×2208), çıkışta büyütme yok. Yerel görseller görevin oranını ve `video_fit_mode`'unu alır; OpenAI görselleri doğal boyutta kalır (sığdırma kipi o yola gelmiyor). Normalleştirme bu klipleri boyutlandırmaz, ama yine bir kez yeniden kodlar (#5'e kadar). Kırpım başına 23,7 + 13,7 sn → 10,0 sn, yeniden kullanımda 0,05 sn.
  - `n_threads` (`perf/ffmpeg-auto-threads`): `VideoParams` ve `TaskVideoRequest` varsayılanı `None`; concat ve son render'a `-threads` verilmez, FFmpeg seçer. Açık değer (API 1–16, CLI `--n-threads`) aynen geçer.
  - Önbellekteki stok videonun ölçümü (`perf/cached-video-probe`): VideoFileClip yerine `material_upload._probe_video_metadata` (ffprobe FFmpeg'in yanında ya da PATH'te varsa ffprobe, yoksa MoviePy `ffmpeg_parse_infos`); geçerlilik ölçütü aynı. Kazanç küçük: dosya başına 0,3–0,8 sn → 0,1 sn. Önbellek isabetinin asıl maliyeti tam akış çözümlemesi (4K'da 6–8 sn), ölçütün parçası olduğu için kaldı; değiştirilecekse doğrulanmış dosyanın boyut ve mtime damgasıyla atlanması düşünülebilir.
  - NASA metin sonucu önbelleği (`perf/nasa-text-cache`): `cache_videos/nasa-text-<sha256>.json`; anahtar önizleme adresi, `FILTER_VERSION`, oran, model dosyası ve bütün eşikler (DB parametreleri `TEXT_DB_*` sabitlerine taşındı). Yalnız tamamlanan kontroller saklanır; bozuk dosya yeniden kontrol edilir. Zombi'nin 24 sonucu toplam 346 bayt.
  - `config.toml`: `material_concurrency = 3`, `video_clip_concurrency = 3` (kullanıcı onayıyla, 05.11; başka değer değişmedi). WebUI artık 1, 2, 3, 4, 6, 8 sunuyor ve listede olmayan pozitif tam sayıyı olduğu gibi gösteriyor (`fix/webui-concurrency-options`); okunamayan değer yine 1 olur.
  - Ölçüm (05.11, `main` 47fcf5e, aynı metin, terim ve çapalar; arka planda başka süreçlerin yükü var, aynı ayarlı koşular arasında ±%20 oynama):

    | aşama (sn) | önceki sıcak | a: ilk koşu, eşz. 3 | b: sıcak, eşz. 3 | c: sıcak, eşz. 1 | d: ilk koşu, eşz. 1 |
    |---|---|---|---|---|---|
    | malzeme | 255 | 69 | 11 | 12 | 107 |
    | birleştirme | 215 | 94 | 92 | 103 | 129 |
    | son render | 545 | 433 | 448 | 445 | 469 |
    | toplam | 1017 | 597 | 552 | 561 | 708 |
    | tepe RAM (MB) | 2010 | 3291 | 3125 | 1965 | 2163 |

    "İlk koşu": yakınlaştırma klibi ve metin sonucu önbelleği boş, malzeme önbelleği dolu. Eşzamanlılık 3, ilk koşuda malzemeyi 107 → 69 sn, normalleştirmeyi 102 → 69 sn kısaltıyor. Sıcak koşuda yalnız normalleştirmeyi kısaltıyor (79 → 68 sn), çünkü 4K Pixabay klibi tek başına 40–52 sn. Bedeli +1,1–1,3 GB RAM. Son render'daki kazanç otomatik thread sayısından. Video öncekiyle aynı görünüyor: bütün videoda SSIM 0,968; NASA karelerinde yeni yol biraz daha keskin (tek yeniden örnekleme). Malzeme kaynakları ve `credits.txt` aynı.
- [x] ffmpeg son render (#2), kopya concat (#5), ffmpeg normalleştirmesi ve önbellek temizliği (05.11; dallar Git akışı'nda):
  - **Ayar** `final_render_engine` = `moviepy` | `ffmpeg` (varsayılan `moviepy`, upstream ile aynı davranış; `config.toml`'da kullanıcı isteğiyle `ffmpeg`). CLI `--final-render-engine` (yalnız o süreç), WebUI "Video Encoder"ın üstünde (14 dil), `config.example.toml`'da açıklamalı.
  - **Nasıl:** `video._render_final_video_with_ffmpeg`, `generate_video`'nun kaynak videonun üstüne koyduğu bütün katmanları (altyazılar, sonra atıf) bileşimden okur; her katman MoviePy'nin kendi kare/maske koduyla bir kez saydam PNG'ye çizilir (aynı katmanlar tek PNG), konum MoviePy `compute_position` ile, görünürlük MoviePy'nin `start <= i/fps < end` kuralıyla kare numarasına göre (`enable='between(n,a,b)'`). Kaynak MoviePy okuyucusu gibi bicubic ile RGB'ye çözülür, RGB'de `overlay`, sonra aynı BT.709 filtresi ve aynı kodlayıcı seçenekleri (yapılandırılmış codec + libx264'e dönüş, preset, threads); kare sayısı `trim=end_frame` ile (`-frames:v` videodan sonraki ses paketlerini atıyordu). Ses MoviePy'nin `write_videofile` içindeki çağrısıyla aynı biçimde yazılır ve `-c:a copy` ile eklenir.
  - **Desteklenenler:** altyazı konumları (bottom, top, center, two_thirds_bottom, custom + `custom_position`), yazı tipi, boyut, renk, kontur, düz ve yuvarlak arka plan kutusu, satır kaydırma ve çok satır, Arapça şekillendirme (aynı çizim kodu), kelime kelime altyazı (daha çok katman), altyazı kapalı, NASA atıf katmanı, video kaynağından uzun süren altyazı (siyah zemin, MoviePy gibi), anlatım ses düzeyi, BGM (rastgele/özel/sağlayıcı, düzey, döngü, 3 sn çıkış sönümü), 9:16/16:9/1:1, `video_codec` (QSV dahil) ve `n_threads`. Filigran/logo ve BGM giriş sönümü uygulamada yok.
  - **Desteklenmeyenler (doğrudan MoviePy, gerekçe loglanır):** `subtitle_animation = pop_spring` ve zamanla değişen başka katman (3 zamanda kare/konum karşılaştırması), 30 fps olmayan kaynak video. ffmpeg'in herhangi bir hatasında uyarı loglanıp MoviePy'ye dönülür.
  - **Normalleştirme ve concat** (`perf/ffmpeg-concat-normalize`): yalnız boyutlandırma/kırpma isteyen klipler (geçiş efekti ve hız değişimi yoksa) tek ffmpeg komutuyla: MoviePy'nin kare seçimi (yarım sn önce ara, zamanı başlangıca kaydır, `fps=30:round=up`, öncesini at, `-r 30`; 24/25/29,97/30/60 fps'te seçilen kareler aynı), bicubic RGB çözme, `_fit_clip_to_canvas`'la aynı boyut ve kaydırma (cover/contain, yön dışı klipte zorunlu cover), `setsar=0`. Geçiş ya da hız varsa veya ffmpeg hata verirse MoviePy yolu; video boyutundaki klipler için aşağıdaki son adıma bakın. Ara klipler B karesiz (`-bf 0`) kodlanır; bütün kliplerin akış bilgisi ve SPS/PPS'i aynıysa ve x264 `bframes=0` ise concat akışı kopyalar ve ses süresinde tam keser, değilse (ör. donanım kodlayıcı, 0 kareli kuyruk klibi) eskisi gibi yeniden kodlar. 4K → 1080×1920 5 sn: 42,4 → 7,4 sn.
  - **Önbellek temizliği:** `cache_manager` yaş kuralıyla yakınlaştırma kliplerini, `.sanitized.png` kopyalarını, NASA orijinal/önizleme/hazırlanmış görsellerini ve `nasa-text-*.json`'ları da kapsıyor; geçici dosyaları ancak 1 günden eskiyse siliyor. 0. adımda eski adlı 69 klip (130 MB) ve 4 ölçüm görevi silindi.
  - **Eşdeğerlik (otomatik):** 108 durum (konum × kutu × kontur 0/3 × STHeiti/YaHei × üç oran, artı altyazısız, müziksiz, altyazı videodan uzun, sesi videodan uzun) ve 6 atıf durumunda kare SSIM'i ≥ 0,98, kare sayıları, ses paketleri ve ses-görüntü başlangıcı aynı; değerleri raporlanan 106 stil ve 6 atıf durumunda en düşük kare SSIM'i 1,0. Normalleştirme cover/contain durumlarında en düşük kare SSIM'i 0,985–0,996.
  - **Ölçüm** (05.11, sıcak önbellek, aynı metin ve terimler, eşzamanlılık 3; Zombi 44 sn, K3 69 sn):

    | aşama (sn) | Zombi önceki | Zombi moviepy | Zombi ffmpeg | K3 moviepy | K3 ffmpeg |
    |---|---|---|---|---|---|
    | malzeme | 11 | 10 | 10 | 28 | 28 |
    | birleştirme | 92 | 38 | 37 | 66 | 66 |
    | ↳ concat | 24 | 0,8 (kopya) | 0,9 (kopya) | 1,1 (kopya) | 1,1 (kopya) |
    | son render | 448 | 415 | 29 | 624 | 42 |
    | toplam | 552 | 465 | 79 | 720 | 138 |
    | tepe RAM (MB) | 3125 | 2709 | 2804 | 2956 | 2939 |

    ffmpeg son render'ı ~4,1 çekirdek kullanıyor (MoviePy ~1,3). Video iki motorda aynı (bütün videoda ve 5'er karede SSIM 1,0). Sabit müzikle ses paketleri aynı, korelasyon 1,0; `bgm_type = random` her koşuda başka şarkı seçtiği için koşular arası korelasyon 0,993 çıkıyor. Birleştirmede kalan sürenin çoğu video boyutundaki yakınlaştırma kliplerinin MoviePy normalleştirmesi (eşzamanlılık 3 altında klip başına 6–13 sn).
- [x] Son hızlandırma adımı (05.11):
  - **Atlama kuralı:** geçiş ve hız değişimi yoksa, klibin tamamı kullanılıyorsa (başlangıç 0, son = klip süresi) ve dosya zaten video boyutunda, 30 fps, yuv420p, sessiz ve kopya concat'in beklediği x264 B karesiz kodlamadaysa klip yeniden kodlanmadan concat listesine girer (`SubClippedVideoClip.owns_file = False`; önbellek dosyası olduğu için silinmez). Liste sonunda yine kopyalanamıyorsa (ör. öteki klipler donanım kodlayıcıdan) bu klipler eskisi gibi yeniden yazılır, çünkü yeniden kodlayan concat da aynı parametreleri ister. Yakınlaştırma klipleri bunun için B karesiz kodlanıyor (`_IMAGE_CLIP_RENDER_VERSION = 3`; sürüm 2'nin 18 klibi, 29,7 MB silindi).
  - **Kesit:** video boyutundaki bir klibin yalnız bir parçası kullanılıyorsa (ya da klip olduğu gibi kullanılamıyorsa) ffmpeg normalleştirmesi boyutlandırmadan keser. Akış kopyalayarak kesit ölçüldü ve reddedildi: `-ss/-t -c copy` düzenleme listesine dayanıyor, MoviePy'nin aldığı kareden bir sonrakiyle başlıyor ve kopya concat içinde 10 kare fazla veriyor (460/450 paket); concat `inpoint/outpoint` anahtar kareden başlıyor (150 yerine 160 kare). ffmpeg kesiti MoviePy'yle aynı kareleri alıyor ve SPS'i koruyor (450/450).
  - **QSV ölçüldü, seçilmedi.** Son render bileşeninde (Zombi) libx264 medium 36,6 sn / 14,2 MB; QSV `global_quality` 23: 11,5 sn / 16,3 MB, yüksek kaliteli referansa göre SSIM 0,9855 (libx264 0,9889; gq 21: 0,9877 / 21 MB, varsayılan 0,9797). Bütün hatta (`video_codec = "h264_qsv"`, gq 23): Zombi 52,3 → 51,1 sn, K3 125,8 → 112,2 sn; libx264 çıktısına göre bütün videoda SSIM 0,979 / 0,985, 5 karede en düşük 0,949. Son render kısalıyor (29 → 11, 54 → 16 sn) ama QSV ara klipleri kopya concat'e giremiyor, olduğu gibi kullanılan klipler ve concat yeniden kodlanıyor (birleştirme 11 → 28, 41 → 65 sn). Zombi'de fark 10 sn'nin altında kaldığı için libx264'te kalındı. Geçersiz bir QSV parametresiyle (`-look_ahead_depth 1000`) gerçek görevde libx264'e dönüş doğrulandı: hem bütün QSV kodlamalarında (ilk hatada QSV devre dışı kalıyor) hem yalnız ffmpeg son render'ında.
  - **Ölçüm** (sıcak önbellek, `config.toml`'daki son ayarlar: ffmpeg motoru, libx264, eşzamanlılık 3; aynı metin ve terimler):

    | aşama (sn) | Zombi önceki | Zombi şimdi | Zombi soğuk | K3 önceki | K3 şimdi |
    |---|---|---|---|---|---|
    | malzeme | 10 | 11 | 81 | 28 | 28 |
    | birleştirme | 37 | 10 | 10 | 66 | 39 |
    | son render | 29 | 29 | 29 | 42 | 41 |
    | toplam | 79 | 52 | 123 | 138 | 110 |
    | tepe RAM (MB) | 2804 | 1557 | 2058 | 2939 | 2943 |

    "Soğuk": `cache_videos` ve `cache_material_search` geçici olarak taşındı (arama, önizleme, indirme, yüz ve metin kontrolü, yakınlaştırma render'ı yeniden), sonra geri kondu. Zombi'nin 8 yakınlaştırma klibi olduğu gibi kullanılıyor; K3'te kalan birleştirme süresi 6 Pixabay klibinin (4K/1080p) ffmpeg normalleştirmesi.
  - **Hızlandırma çalışmasının özeti** (Zombi yıldızlar, 44 sn, sıcak önbellek): 1017 sn (profil) → 552 (yakınlaştırma klibi yeniden kullanımı, doğrudan hedef boyut, otomatik thread, metin önbelleği, eşzamanlılık 3) → 79 (ffmpeg son render, kopya concat, ffmpeg normalleştirmesi) → 52 sn (video boyutundaki klipleri olduğu gibi kullanma); yaklaşık 19 kat. Tipik süreler: sıcak önbellekte videonun saniyesi başına ~1,2 sn (NASA ağırlıklı) ile ~1,6 sn (K3 gibi çok sayıda Pixabay klibi); soğuk Zombi 123 sn. Son render artık birkaç saniyelik ses ve MoviePy katman çizimi dışında ffmpeg'de; malzeme aşaması sıcakta ~10 sn.
- [ ] **Ertelendi:** zoompan (#3). Yakınlaştırma klibi önbellekte olduğu için yalnız soğuk koşuda kazanç verir (doğrudan boyutlu render 5 sn'lik klip başına ~10 sn, zoompan ölçümde 5,6–9,3 sn); kayma (jitter) ve görünüm eşdeğerliği ayrıca doğrulanmalı.
- [ ] **Olası:** yalnız son render için donanım kodlayıcı (ara klipler libx264'te kalır, kopya concat bozulmaz). Ölçülen bileşen kazancına göre Zombi'de ~18, K3'te ~38 sn; ayrı bir codec ayarı ve QSV kalite parametresi (`-global_quality 23`) gerektirir.
- [ ] **Ertelendi:** görsel LLM taraması (fizibilitedeki B). Güvenlik alanlarında güçlü (yazı ve kişi kaçırmadı, astronomide yanlış alarm yok), ama uygunlukta %68 ve claude ile Max kotasını, gemma4 ile Ollama bulut planını kullanıyor; görsel başına 3–5 sn. Yeniden değerlendirmede daha hafif bir claude modeli (`--model`) denensin; uygunluk kararı etiket eşiğiyle birleşirse (model E derse NASA'da kal) 54 terimde puan 70 → 73.
- [ ] Sıradaki aday: NASA videosu (`nasa_media_types`'a "video"; arşivde dikey video az, önce oran ve süre dağılımı ölçülmeli).
- [ ] Soyut kavramlar için somut terim yedeği: `visual` stil zaten soyut terim üretmiyor, ama alan dışına çıkan somut metaforlar üretiyor ("collapsing building" → Osaka Kalesi, "glowing particles" → Noel ağacı); en iyi kombinasyondaki 5 yanlış seçimin beşi bunlardan. Strateji: metafor terimlerine alan kelimesi eklensin ("glowing particles space"). En üst aday alan çapasıyla eşleşmezse terim + alan kelimesiyle yedek arama yapılsın. Yer adı olmayan konularda çapalar "space", "physics" gibi görsel alan kelimeleri olsun.
- [ ] **Ertelendi** (kullanım odağı bilim/uzay, bkz. yukarı): çapa destekli ek arama. Bir terimin sonuçlarında yer çapası taşıyan aday yoksa "terim + yer çapası" ile ek arama yapılıp sonuçlar birleştirilsin. Yer çapaları özne çapalarından ("autumn") ağır bassın; şu an ikisi eşit sayılıyor.
- [ ] **Ertelendi** (aynı gerekçeyle): ikiden çok kaynak. Terim başına tek yedek `feat/source-fallback` ile geldi; liste tabanlı `video_source` bu adımda maliyetli bulundu (bkz. yedek kaynak maddesi).

## Kod kuralları

- **Düzenleme kuralı:** kaynak dosyalar Edit aracıyla değiştirilir. sed, regex yamaları ve heredoc ile dosya yazma kullanılmaz. Mevcut satır sonları korunur: `core.autocrlf=true` olduğu için çalışma alanındaki dosyaların çoğu CRLF, `sed -i` ise CRLF'yi LF'ye çevirir.
- Mevcut dosya yapısına ve fonksiyon imzalarına uy. API, WebUI ve CLI aynı servis fonksiyonlarını çağırır, testler de bu imzalara bağlı.
- Yeni servisler `app/services/` altındaki mevcut desenle eklenir: her sağlayıcı kendi modülünde (örneğin `muapi.py`) durur ve var olan dağıtım noktasına bağlanır:
  - LLM: `llm_provider.py`'ye bir `LLMProviderSpec`, gerekiyorsa `llm._generate_response`'a bir dal.
  - TTS: `voice.py`'ye `is_<x>_voice` ve `<x>_tts` fonksiyonları, `_single_tts`'e bir dal.
  - Video kaynağı: ayrı modül, `material.download_videos`'ta bir dal, `task._SUPPORTED_VIDEO_SOURCES`'a yeni değer.
- Yeni ayarlar `config.example.toml`'a da eklenir; yorumlu ve varsayılan değeriyle.
- Yeni WebUI metni 14 dil dosyasının hepsine (`webui/i18n/*.json`) eklenir. `test_webui_i18n.py`, her dilin `en.json`'daki bütün anahtarları içerdiğini denetler.
- Testler `test/services/test_<alan>.py` dosyalarına yazılır (pytest ya da unittest). Ağ ya da anahtar isteyen testler `MPT_RUN_INTEGRATION_TESTS=1` olmadan atlanmalı.
- Çevredeki kodun yorum dilini ve yoğunluğunu izle; servis dosyalarındaki yorumların çoğu Çince.
