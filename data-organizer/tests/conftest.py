"""Test fixtures. Environment is redirected to a temp dir BEFORE the app is imported, so tests never touch real data."""
import os
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="organizer-tests-"))
os.environ.update(DATA_DIR=str(_TMP / "data"), LOG_DIR=str(_TMP / "logs"), SECRETS_DIR=str(_TMP / "secrets"),
                  OUTPUT_DIR=str(_TMP / "output"), LOCAL_INPUT_DIR="", GOOGLE_DRIVE_FOLDER_URL="", GOOGLE_DRIVE_FOLDER_ID="",
                  DASHBOARD_TOKEN="")

import pytest  # noqa: E402

from app.config import RuntimeConfig, load_settings  # noqa: E402
from app.database.repository import Repository  # noqa: E402
from app.pipeline import LocalSource, Pipeline  # noqa: E402
from scripts.generate_sample_data import generate  # noqa: E402


@pytest.fixture(scope="session")
def settings():
    s = load_settings()
    s.ensure_dirs()
    return s


@pytest.fixture(scope="session")
def demo_dir():
    d = _TMP / "demo"
    generate(d)
    return d


@pytest.fixture()
def cfg(demo_dir):
    return RuntimeConfig(local_input_dir=str(demo_dir), output_dir=str(_TMP / "out"))


@pytest.fixture()
def repo():
    r = Repository(":memory:")
    yield r
    r.close()


@pytest.fixture()
def done_pipeline(settings, cfg, repo, demo_dir):
    """A repository after a complete run over the demo files."""
    p = Pipeline(settings, cfg, repo, source=LocalSource(demo_dir))
    p.run()
    assert not p.progress.snapshot()["error"]
    return p
