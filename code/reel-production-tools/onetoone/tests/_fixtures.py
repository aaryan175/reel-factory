"""Locations of optional real-project fixtures for the onetoone tests.

Tests that check a real project's brain files skip when the file is absent. Point
REEL_FACTORY_FIXTURES at a directory holding your own fixtures to run them; the
default is $REEL_FACTORY_WORKBENCH/fixtures (see rf_paths.py).
"""
import os
import sys
from pathlib import Path

_TOOLS = Path(__file__).resolve().parents[2]
if str(_TOOLS) not in sys.path:
    sys.path.insert(0, str(_TOOLS))

from rf_paths import WORKBENCH  # noqa: E402

FIXTURES = Path(os.environ.get("REEL_FACTORY_FIXTURES") or (WORKBENCH / "fixtures")).expanduser()


def fixture(*parts: str) -> Path:
    return FIXTURES.joinpath(*parts)
