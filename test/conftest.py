"""Keep test runs away from the developer's config.toml, storage/ and resource/.

app.config reads config.toml when it is imported, and test modules import app
while pytest collects them, before any fixture runs. pytest_configure therefore
points MPT_HOME, the runtime root of config.toml, storage/ and resource/, at a
pytest temporary directory first. Subprocesses started by tests inherit it.
"""

import os
import shutil
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
_RUNTIME_HOME = pytest.StashKey[Path]()


@pytest.hookimpl(trylast=True)
def pytest_configure(config):
    # trylast: the built-in tmp_path plugin creates _tmp_path_factory in its own
    # pytest_configure, which must run first.
    runtime_home = config._tmp_path_factory.mktemp("mpt-home", numbered=False)
    shutil.copyfile(PROJECT_ROOT / "config.example.toml", runtime_home / "config.toml")
    config.stash[_RUNTIME_HOME] = runtime_home

    previous = os.environ.get("MPT_HOME")
    os.environ["MPT_HOME"] = str(runtime_home)

    def restore_mpt_home():
        if previous is None:
            os.environ.pop("MPT_HOME", None)
        else:
            os.environ["MPT_HOME"] = previous

    config.add_cleanup(restore_mpt_home)


@pytest.fixture(scope="session", autouse=True)
def isolated_runtime_home(pytestconfig):
    """Stop the run if app.config was loaded before MPT_HOME was redirected."""
    from app.config import config as app_config

    runtime_home = pytestconfig.stash[_RUNTIME_HOME]
    if Path(app_config.config_file).resolve().parent != runtime_home.resolve():
        pytest.exit(
            f"tests would write the real config file {app_config.config_file}; "
            f"expected it under {runtime_home}",
            returncode=pytest.ExitCode.USAGE_ERROR,
        )
    return runtime_home


@pytest.fixture(autouse=True)
def no_nasa_preview_downloads(monkeypatch):
    """nasa_text_filter is on by default and downloads NASA previews before the
    images themselves. Tests must not reach the network, so the filter is off
    unless a test turns it on (test_nasa_text_filter.py sets it explicitly)."""
    from app.config import config as app_config

    monkeypatch.setitem(app_config.app, "nasa_text_filter", False)


@pytest.fixture(autouse=True)
def no_real_batch_review_llm(monkeypatch):
    """presets/uzay.toml is a two-step batch preset: a test that runs it without review=False or a fake
    preparer would prepare scripts with the real LLM (claude_code). Fail fast instead; tests that need
    the services replace them themselves (their monkeypatch runs after this one)."""
    from app.services import batch_review

    def refuse(*args, **kwargs):
        raise AssertionError("a test reached the real batch review LLM; pass review=False or fake services")

    monkeypatch.setattr(batch_review, "prepare_item", refuse)
    monkeypatch.setattr(batch_review, "PipelineServices", refuse)
