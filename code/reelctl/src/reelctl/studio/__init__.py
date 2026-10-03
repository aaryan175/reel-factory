"""Reel Studio v1: the local operator surface built on top of the reelctl engine.

Modules here read the engine's truth on disk (``REEL_REGISTRY.json``, each project's
``state.json`` and receipts) and project it for the operator. They never advance a
stage: stage advancement happens only inside reelctl.

Submodules are imported directly (``from reelctl.studio import status``); this file
deliberately declares no ``__all__``, so several agents adding modules to the package
never have to edit one shared list.
"""

from __future__ import annotations
