from __future__ import annotations

from pathlib import Path

import pytest

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "mini_gtfs"


@pytest.fixture(scope="session")
def mini_gtfs_dir() -> Path:
    return FIXTURE_DIR
