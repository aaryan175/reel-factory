"""Shared locations for the day-in-the-life (DITL) example engine.

All paths are configurable:
  DITL_WORKDIR   working folder (specs, songs, look files, outputs)
                 default: $REEL_FACTORY_WORKBENCH/ditl  (see ../rf_paths.py)
  FFMPEG         ffmpeg binary (default: first `ffmpeg` on PATH)
"""
import os
import shutil
import sys

TOOLS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)

import rf_paths  # noqa: E402

WB = os.path.expanduser(os.environ.get('DITL_WORKDIR') or str(rf_paths.WORKBENCH / 'ditl'))
FF = os.environ.get('FFMPEG') or shutil.which('ffmpeg') or 'ffmpeg'
LIB_JSON = str(rf_paths.FOOTAGE_LIBRARY_JSON)
LIBROOT = str(rf_paths.FOOTAGE_LIBRARY_ROOT)
IDENTITY_POOL = os.path.join(TOOLS, 'onetoone', 'identity_pool.json')


def read_text(path: str, default: str = '') -> str:
    """Optional look/config file: empty default when it has not been created yet."""
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return default
