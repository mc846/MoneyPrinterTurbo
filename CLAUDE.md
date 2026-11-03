# CLAUDE.md

MoneyPrinterTurbo, bir konu ya da anahtar kelimeden kendiliğinden kısa video üretir: metni LLM yazar, görüntüleri stok sitelerden ya da AI'dan alır, üstüne TTS seslendirme, altyazı ve arka plan müziği ekler. Bu dizin `mc846` fork'u; geliştirme Windows'ta, uv ile yerelde yapılır.

## Mimari

**Giriş noktaları**
- `main.py`: FastAPI uygulamasını (`app.asgi:app`) uvicorn ile `listen_host:listen_port` adresinde başlatır.
- `webui/Main.py`: Streamlit WebUI (tek dosya, ~9,5 bin satır). API'yi kullanmaz, görevi aynı süreçte `app/services/webui_task.py` → `task.start` üzerinden çalıştırır. Metinler `tr("key")` ile `webui/i18n/*.json` dosyalarından gelir.
- `cli.py`: tarayıcısız üretim yolu, `run_cli` → `build_video_params` → `task.start`. `--stop-at` şu aşamalardan biri olabilir: script, terms, audio, subtitle, materials, video. `--batch-file` JSON ya da JSONL alır. `--video-source` verilmezse `[app].video_source` kullanılır, o da yoksa `pexels`.
- `app/launcher.py`: pip ile kurulan `mpt` komutunun girişi (`mpt webui`, `mpt cli`).

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
1. Metin: `llm.generate_script`
2. Arama kelimeleri: `llm.generate_terms` (`video_source=local` ise atlanır). İkisi birlikte `script.json`'a kaydedilir.
3. Ses: `voice.tts` → `audio.mp3`. Sağlayıcıyı ses adı belirler (`voice._single_tts` içindeki `is_<x>_voice` dalları). Edge (Azure V1) anahtarsız çalışır.
4. Altyazı: `task.generate_subtitle` → `subtitle.srt`. `app.subtitle_provider` iki değer alabilir: `edge` zaman damgalarını TTS'den alır (`voice.create_subtitle`); `whisper` faster-whisper ile üretir (`subtitle.create`), ardından metne göre düzeltir (`subtitle.correct`).
5. Materyal: `material.download_videos`. Kaynaklar `task._SUPPORTED_VIDEO_SOURCES`'ta listeli: pexels, pixabay, coverr, local, wavespeed, volcengine_seedance, ofox, metaso_minimax, muapi, loomloom, openai_image, codex_image, infistar, jalapeno. Önbellek `material_cache.py`'de. `crop_materials_to_aspect` açıkken Pexels ve Pixabay için yön filtresi yerine `material._center_crop_size` kuralı kullanılır; kırpılan adaylar yerel yöndekilerden sonra gelir. `material_tag_relevance` ve `exclude_ai_generated_materials` adayları `material._order_material_candidates` içinde sıralar ve süzer (yalnız Pixabay).
6. Video: `task.generate_final_videos`. `video.combine_videos` klipleri `combined-N.mp4` olarak birleştirir, `video.generate_video` ses, altyazı ve müziği (`bgm.py`) ekleyip `final-N.mp4` üretir.
7. İsteğe bağlı paylaşım: `upload_post.py`, yalnızca `auto_upload` açıksa.

**Servisler** (`app/services/`)
- LLM: `llm.py`, `_generate_response` sağlayıcıya göre dallanır (43 sağlayıcı). OAuth kullananlar: `chatgpt_*`, `codex_*`, `kimi_code_oauth.py`. `claude_code` sağlayıcısı `claude` CLI'ını çağırır.
- TTS: `voice.py`. Sağlayıcılar: edge, azure v2, siliconflow, gemini, mimo, minimax, elevenlabs, chatterbox, kokoro, fish_audio, voxcpm ve sessiz kip.
- Materyal: `material.py` stok aramasını ve AI görsellerini yapar. Her ücretli sağlayıcının ayrı modülü var: `muapi.py`, `ofox.py`, `metaso_minimax.py`, `volcengine_seedance.py`, `infistar_video.py`, `jalapeno_video.py`, `loomloom.py`, `codex_image.py`, `twelvelabs.py`. Kullanıcı yüklemeleri `material_upload.py`'de.
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
uv run python main.py                                  # API: http://127.0.0.1:8080/docs
.\webui.bat                                            # WebUI: http://127.0.0.1:8501
uv run python cli.py --video-subject "Konu"            # CLI ile video
uv run python cli.py --help
uv run python cli.py --batch-file .\tasks.json --stop-at video
uv run python -X utf8 -m pytest -q test                # bütün testler
uv run python -X utf8 -m pytest -q test/services/test_video.py::TestVideoService
uv run ruff check app cli.py main.py webui test docs/skill   # CI'daki lint
```

- `webui.bat` önce `.venv\Scripts\python.exe -m streamlit` dener. Health ucu `/_stcore/health` ve `ok` döner. Tarayıcının açılmasını istemiyorsan `$env:STREAMLIT_SERVER_HEADLESS="true"` ver.
- Claude Code kabuğunda `NoDefaultCurrentDirectoryInExePath=1` tanımlı olduğu için `cmd /c webui.bat` dosyayı bulamaz. Tam yolla çalıştır: `cmd /c C:\Repom\MoneyPrinterTurbo\webui.bat`.

## Ortam notları

- Python 3.11'i uv yönetir (`.python-version`, `%APPDATA%\uv\python`). Sistemdeki Python 3.14 (`C:\Python314`) kullanılmaz. `python` ya da `pip`'i doğrudan çağırma, her şeyi `uv run` ile çalıştır.
- ffmpeg, `imageio-ffmpeg` paketinden gelir (`.venv\Lib\site-packages\imageio_ffmpeg\binaries\`); PATH'te ffmpeg yok. Başka bir ffmpeg kullanmak için `[app] ffmpeg_path` ya da `IMAGEIO_FFMPEG_EXE` ver. ffprobe olmadığı için `video_project.py` çalışmaz.
- `config.toml` gitignore'da ve API anahtarları orada durur. Yeni ya da değişen her ayar `config.example.toml`'a da işlenmeli.
- `listen_host` yerelde `127.0.0.1`. Docker'daki API container'ı (`main.py`) aynı `config.toml`'u okur ve container içinde `0.0.0.0` dinlemesi gerekir; değer `127.0.0.1` kalırsa host'tan erişilemez. WebUI container'ı etkilenmez, çünkü adresini komut satırından (`--server.address=0.0.0.0`) alır. Bu değer için henüz ortam değişkeni yok; ileride eklenecek.

## Portlar

Yerel çalıştırma ve Docker aynı portları kullanır: WebUI 8501, API 8080 (Docker'da 127.0.0.1'e eşlenir). İkisini aynı anda çalıştırma. Yerel API 8080'i bağlayamaz; `webui.bat` ise 8501 doluysa hiçbir şey demeden 8502–8599 arasında boş bir porta geçer. Önce `docker compose -f docker-compose.release.yml down` çalıştır.

## Test baz durumu

03.11.2026, Windows 11, Python 3.11.18. Redis yok, API anahtarı yok, PATH'te ffmpeg yok. `pytest -q test` 12–18 dakika sürer.
**3221 geçti, 3 kaldı, 148 atlandı** (ayrıca 13 100 alt test geçti; düzeltmelerin, kırpma ve etiket uygunluğu özelliklerinin 122 yeni testi dahil). CI tam takımı yalnız Linux'ta çalıştırır; Windows'ta yalnız 4 dosyalık bir duman testi var, o yüzden aşağıdaki hatalar CI'da görünmez.

Kalan 4 testin hiçbiri bir özelliğin bozulduğunu göstermiyor; nedenleri Windows ya da ortam:
- `test_image_sanitize_identity.py::test_long_source_name_renders_without_exceeding_filesystem_name_limit`: tam yol 260 karakteri aşıyor ve `LongPathsEnabled=0` (MAX_PATH sınırı).
- `test_material.py::TestMaterialTlsVerification::test_metadata_probe_failure_preserves_existing_cache`: test yolun `\` ile kurulmasını bekliyor, kod `/` kullanıyor.
- `test_utils_ffmpeg_check.py::TestGetFfmpegBinaryEnvExpansion::test_normalizes_home_path_in_os_environ`: test `HOME`'u değiştiriyor, oysa Windows'ta `expanduser` `USERPROFILE`'ı okur.
- `test_material_cache_duration.py::test_legacy_finite_float_duration_remains_usable`: aralıklı kalıyor, tek başına çalıştırılınca 300 denemenin 24'ünde. Windows'ta yeni yazılan bir dosyanın `mtime` değeri `time.time()`'dan ileride olabiliyor (ölçümde 2000 yazmanın 227'sinde). O zaman `cache_age < 0` çıkar ve önbellek geçersiz sayılır. Bu yüzden toplam sonuç 3220–3221 geçti, 3–4 kaldı arasında değişir.
- `test_webui_startup.py::TestWebuiStartup::test_external_directory_prefers_project_app_package` tam turun yükü altında bazen alt süreç zaman aşımına (60 sn) takılıyor; tek başına 9–14 sn'de geçiyor.

Atlananların dağılımı: 91 native ffmpeg/ffprobe ister, 38 Redis ister (`MPT_TEST_REDIS_HOST`), 8 Kokoro test sunucusu ister, 7 canlı sağlayıcı ister (`MPT_RUN_INTEGRATION_TESTS=1`), 3 `twelvelabs` extra'sı kurulu olmadığı için, 1 Windows'ta geçersiz bir yol yüzünden atlanıyor.

Testler `test/conftest.py` fixture'ı sayesinde izole çalışır; gerçek `config.toml`'a dokunmaz. `pytest_configure`, toplama aşamasından önce `MPT_HOME`'u pytest'in geçici dizinine yönlendirir (`%TEMP%\pytest-of-<kullanıcı>\pytest-N\mpt-home`) ve oraya `config.example.toml`'un bir kopyasını koyar. `storage/` ve `resource/` da bu dizine gider; kaynaklar ilk kullanımda kopyalanır, oturum başına yaklaşık 198 MB. Alt süreçler `MPT_HOME`'u ortamdan devralır. `isolated_runtime_home` adlı session fixture'ı, `app.config` yönlendirmeden önce yüklendiyse çalıştırmayı durdurur.

## Git akışı

- `origin` = https://github.com/mc846/MoneyPrinterTurbo (fork), `upstream` = https://github.com/harry0703/MoneyPrinterTurbo. `main` dalı `origin/main`'i izler.
- Her özellik ayrı bir dalda geliştirilir: `git switch -c feat/<ad> main`, ardından `git push -u origin feat/<ad>`.
- **upstream'e asla push edilmez.** upstream'in push adresi `DISABLE` yapıldı (`git remote set-url --push upstream DISABLE`), bu yüzden `git push upstream` hata verir.
- upstream güncellemeleri merge ile alınır: `git switch main; git fetch upstream; git merge upstream/main; git push origin main`.
- Commit mesajları upstream'in biçimini izler: `fix(webui): …`, `feat(tts): …`, `docs: …`, `test: …`.
- upstream `.gitignore`'u `CLAUDE.md`'yi yok sayar; fork bu dosyayı `git add -f` ile izliyor. upstream'e PR açılacaksa dalı `upstream/main`'den aç ki bu commit PR'a girmesin.
- `upstream/main` tabanlı dallarda `test/conftest.py` yoktur ve testler gerçek `config.toml`'u okur ya da bozar. Bu dallarda testleri geçici bir `MPT_HOME` ile çalıştır: dizine `config.example.toml`'u `config.toml` olarak kopyala, sonra `$env:MPT_HOME="<dizin>"; uv run python -X utf8 -m pytest -q <testler>`.

## Planlanan iyileştirmeler

Sırası önceliğe göre. `[x]` ile işaretliler tamamlandı ve `main`'e alındı.

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
  - **PR sırası:** `feat/material-relevance`, `feat/cross-orientation-crop`'un üzerine kurulu. Upstream'de önce o birleşmeli.
  - Uçtan uca gözlem (03.11): `rank` ve `strict` "Galata tower" için Belgrad Kulesi yerine Galata etiketli klibi seçti; 6 klipten 5'i İstanbul. Ama o klip kırpılmış ve merkez kırpma kuleyi kadrajın dışında bırakıyor. "autumn leaves" için seçilen Fuji klibi de etiket açısından tam eşleşiyor, yani terim seçimi hâlâ belirleyici.
- [x] `per_page` config'e taşındı (`feat/material-relevance`): `pixabay_per_page` (50, 3–200), `pexels_per_page` (20, 1–80). Sınır dışı değerler sınıra çekilir. Önbellek anahtarına yalnız varsayılandan farklı değer girer. Coverr'ın `page_size` değeri (20) sabit kaldı.
- [ ] Pixabay `category` **global ayar yapılmadı**: kategori bütün aramayı süzer, oysa terimler farklı kategorilere düşer (Sultanahmet "places/religion", vapur "transportation", yapraklar "nature"). Tek bir global değer başka terimlerin doğru adaylarını eler. Gerekirse terim başına ya da görev başına bir seçenek olarak düşünülmeli.
- [ ] `llm.generate_terms` prompt'u: "always add the main subject" kuralı yumuşatılsın, stok sitelerinin etiketlediği somut görsel isimler istensin.
- [ ] Çoklu kaynak desteği: `video_source` bir liste ya da sırayla denenen kaynaklar olsun (`material.download_videos`, `task._SUPPORTED_VIDEO_SOURCES`, `VideoParams`, `cli.py`).

## Kod kuralları

- Mevcut dosya yapısına ve fonksiyon imzalarına uy. API, WebUI ve CLI aynı servis fonksiyonlarını çağırır, testler de bu imzalara bağlı.
- Yeni servisler `app/services/` altındaki mevcut desenle eklenir: her sağlayıcı kendi modülünde (örneğin `muapi.py`) durur ve var olan dağıtım noktasına bağlanır:
  - LLM: `llm_provider.py`'ye bir `LLMProviderSpec`, gerekiyorsa `llm._generate_response`'a bir dal.
  - TTS: `voice.py`'ye `is_<x>_voice` ve `<x>_tts` fonksiyonları, `_single_tts`'e bir dal.
  - Video kaynağı: ayrı modül, `material.download_videos`'ta bir dal, `task._SUPPORTED_VIDEO_SOURCES`'a yeni değer.
- Yeni ayarlar `config.example.toml`'a da eklenir; yorumlu ve varsayılan değeriyle.
- Yeni WebUI metni 14 dil dosyasının hepsine (`webui/i18n/*.json`) eklenir. `test_webui_i18n.py`, her dilin `en.json`'daki bütün anahtarları içerdiğini denetler.
- Testler `test/services/test_<alan>.py` dosyalarına yazılır (pytest ya da unittest). Ağ ya da anahtar isteyen testler `MPT_RUN_INTEGRATION_TESTS=1` olmadan atlanmalı.
- Çevredeki kodun yorum dilini ve yoğunluğunu izle; servis dosyalarındaki yorumların çoğu Çince.
