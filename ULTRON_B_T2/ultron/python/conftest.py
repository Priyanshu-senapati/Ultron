"""Pytest configuration for the Python sidecars.

Three jobs:

1. Make ``python/`` importable so ``test_insight_pulse.py`` can write
   ``from insight_pulse import ...`` rather than fiddling with package
   layouts. The sidecars are deliberately flat-file modules — we don't
   want a `src/ultron/` package structure here because the daemon's
   token bootstrap reads files from the same dir.

2. Tell ``pytest-asyncio`` to default every ``async def`` test to async
   mode without requiring per-test markers. Newer ``pytest-asyncio``
   versions warn loudly when the mode isn't pinned, and CI hates warnings.

3. Tame chatty third-party loggers so a failed test prints a useful
   traceback rather than 40 lines of ``websockets`` handshake noise.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

# Make `insight_pulse` and `ultron_bridge` importable when pytest is
# invoked from the repo root (`pytest python/`) as well as from inside
# `python/` (`pytest`).
HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

# Pin pytest-asyncio to "auto" mode so every async test runs without
# needing @pytest.mark.asyncio decorators.
import pytest  # noqa: E402  — must come after sys.path edit


def pytest_collection_modifyitems(config, items):
    # No-op hook; kept for future test selection if needed.
    return


# Pytest-asyncio config via a fixture is fragile across versions; the
# canonical knob is `asyncio_mode = auto` in `pyproject.toml`/`pytest.ini`.
# We don't want to ship a pyproject just for this, so we set it via a
# config hook here. Works on pytest-asyncio >= 0.21.
def pytest_configure(config: pytest.Config) -> None:
    config.option.asyncio_mode = "auto"
    # Silence noisy library loggers during tests.
    for name in ("websockets.client", "websockets.server", "asyncio"):
        logging.getLogger(name).setLevel(logging.WARNING)
