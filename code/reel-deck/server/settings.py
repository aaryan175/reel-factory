"""Environment-driven locations and display settings for the Reel Deck.

Nothing here is tied to one machine. Every path can be overridden:

  REEL_DECK_HOME          the Deck's own state dir (config.json, board.json,
                          factory_status.json, uploads/, logs/, index cache)
                          default: ~/apps/reel-deck
  REEL_FACTORY_HOME       the factory's project root (REEL_REGISTRY.json,
                          _receipts/, _control/, tools/)
                          default: ~/reel-production
  REEL_FACTORY_WORKDRIVE  the large work volume (footage, renders)
                          default: /Volumes/WORKDRIVE
  REEL_FACTORY_WORKBENCH  scratch/render area on the work volume
                          default: $REEL_FACTORY_WORKDRIVE/workbench
  REEL_FACTORY_TZ         IANA timezone for human-facing times (default UTC)
  REEL_DECK_WORKFLOW_GLOB optional glob of agent workflow dirs to show as live
                          lanes (unset = feature off)
"""

from __future__ import annotations

import os
from datetime import timezone, tzinfo
from pathlib import Path


def _path(env: str, default: str) -> Path:
    return Path(os.environ.get(env) or default).expanduser()


DECK_HOME = _path("REEL_DECK_HOME", "~/apps/reel-deck")
REEL_HOME = _path("REEL_FACTORY_HOME", "~/reel-production")
WORKDRIVE = _path("REEL_FACTORY_WORKDRIVE", "/Volumes/WORKDRIVE")
WORKBENCH = _path("REEL_FACTORY_WORKBENCH", str(WORKDRIVE / "workbench"))
RECEIPTS = REEL_HOME / "_receipts"
BUS_DIR = _path("REEL_FACTORY_BUS_DIR", str(RECEIPTS / "bus"))
CONTROL_DIR = _path("DECK_CONTROL_DIR", str(REEL_HOME / "_control"))
TOOLS_DIR = _path("REEL_FACTORY_TOOLS", str(REEL_HOME / "tools"))
FOOTAGE_LIBRARY_ROOT = WORKBENCH / "footage-library"
WORKFLOW_GLOB = os.environ.get("REEL_DECK_WORKFLOW_GLOB") or ""
TZ_NAME = os.environ.get("REEL_FACTORY_TZ") or "UTC"
# Optional link template for a row's cloud delivery folder, e.g. "https://<host>/folders/{folder_id}".
# Unset = no cloud-folder links are shown.
DRIVE_FOLDER_URL_TEMPLATE = os.environ.get("REEL_DECK_DRIVE_FOLDER_URL") or ""


def local_tz() -> tzinfo:
    """The configured display timezone (REEL_FACTORY_TZ, default UTC)."""
    if TZ_NAME.upper() == "UTC":
        return timezone.utc
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(TZ_NAME)
    except Exception:  # noqa: BLE001 — unknown zone falls back to UTC
        return timezone.utc


# Optional: a home-relative alias for the work volume that older registry rows may carry
# (e.g. "~/work"). Unset by default; set REEL_FACTORY_WORKDRIVE_ALIAS to rewrite such paths.
WORKDRIVE_ALIAS = (os.environ.get("REEL_FACTORY_WORKDRIVE_ALIAS") or "").rstrip("/")


def to_workdrive(text: str) -> str:
    """Map a registry path written under WORKDRIVE_ALIAS onto the configured work volume."""
    if WORKDRIVE_ALIAS and (text == WORKDRIVE_ALIAS or text.startswith(WORKDRIVE_ALIAS + "/")):
        return str(WORKDRIVE) + text[len(WORKDRIVE_ALIAS):]
    return text
