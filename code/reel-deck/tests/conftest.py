"""Test isolation: every Deck/factory location points at a throwaway temp dir.

Set BEFORE any `server` module is imported (server.settings resolves at import time), so no
test can read or write a real Deck home, factory tree, work volume or agent transcript dir.
"""
import os
import tempfile
from pathlib import Path

_ROOT = Path(tempfile.mkdtemp(prefix="reel-deck-tests-"))
for _env, _sub in (
    ("REEL_DECK_HOME", "deck"),
    ("REEL_FACTORY_HOME", "reel-production"),
    ("REEL_FACTORY_WORKDRIVE", "workdrive"),
    ("REEL_FACTORY_WORKBENCH", "workdrive/workbench"),
    ("REEL_DECK_AUDIT_DIR", "workdrive/workbench/footage-intake/audit"),
):
    os.environ[_env] = str(_ROOT / _sub)
os.environ.pop("REEL_DECK_WORKFLOW_GLOB", None)
os.environ.pop("DECK_CONTROL_DIR", None)
os.environ.pop("REEL_FACTORY_BUS_DIR", None)
os.environ.pop("REEL_FACTORY_TOOLS", None)
os.environ["REEL_FACTORY_TZ"] = "UTC"
