"""Central, environment-driven locations for the reel-production tools.

Every module in this directory resolves its working locations through here so
nothing is hardcoded to one machine.  Override with environment variables:

  REEL_FACTORY_HOME       project root (registry, receipts, control files)
                          default: ~/reel-production
  REEL_FACTORY_WORKDRIVE  large external work volume (footage, renders)
                          default: /Volumes/WORKDRIVE
  REEL_FACTORY_WORKBENCH  scratch/render area on the work volume
                          default: $REEL_FACTORY_WORKDRIVE/workbench
  REEL_FACTORY_TZ         IANA timezone used for human-facing timestamps
                          default: UTC
"""
import os
from datetime import timezone, tzinfo
from pathlib import Path


def _p(env: str, default: str) -> Path:
    return Path(os.environ.get(env) or default).expanduser()


REEL_HOME = _p("REEL_FACTORY_HOME", "~/reel-production")
WORKDRIVE = _p("REEL_FACTORY_WORKDRIVE", "/Volumes/WORKDRIVE")
WORKBENCH = _p("REEL_FACTORY_WORKBENCH", str(WORKDRIVE / "workbench"))
FOOTAGE_LIBRARY_ROOT = WORKBENCH / "footage-library"
FOOTAGE_LIBRARY_JSON = REEL_HOME / "FOOTAGE_LIBRARY.json"
RECEIPTS = REEL_HOME / "_receipts"
CONTROL = REEL_HOME / "_control"
TOOLS = Path(__file__).resolve().parent
TZ_NAME = os.environ.get("REEL_FACTORY_TZ") or "UTC"


def local_tz() -> tzinfo:
    """The configured display timezone (REEL_FACTORY_TZ, default UTC)."""
    if TZ_NAME.upper() == "UTC":
        return timezone.utc
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(TZ_NAME)
    except Exception:
        return timezone.utc
