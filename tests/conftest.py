"""Test fixtures.

`tmp_path` is redefined here on purpose. pytest's built-in implementation creates
its base directory with mode 0700 via `mkdtemp`, which on some locked-down
Windows setups (restricted tokens / mandatory integrity labels) produces a
directory the test process itself cannot write into. The replacement keeps the
same contract — a fresh, writable directory per test — but creates it with
`Path.mkdir()` and cleans it up afterwards, so the suite runs on those machines
as well as on a normal laptop.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest

BUILD_ROOT = Path(__file__).resolve().parents[1] / "build" / "pytest-tmp"


@pytest.fixture
def tmp_path(request: pytest.FixtureRequest) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", request.node.nodeid)
    target = BUILD_ROOT / safe
    target.mkdir(parents=True, exist_ok=True)
    yield target
    shutil.rmtree(target, ignore_errors=True)
