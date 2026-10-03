"""Environment-driven locations for the caption-learning tools.

Nothing in this package is tied to one machine.  Override with:

  REEL_FACTORY_HOME       project root (reference projects, registry)
                          default: ~/reel-production
  REEL_FACTORY_WORKDRIVE  large work volume            default: /Volumes/WORKDRIVE
  REEL_FACTORY_WORKBENCH  scratch/render area          default: $REEL_FACTORY_WORKDRIVE/workbench
  CAPTION_SWEEP_ROOT      evidence root for a sweep    default: $REEL_FACTORY_WORKBENCH/caption-sweep
  FFMPEG / FFPROBE        binaries                     default: first on PATH
"""

import os
import shutil


def _p(env, default):
    return os.path.expanduser(os.environ.get(env) or default)


REEL_HOME = _p("REEL_FACTORY_HOME", "~/reel-production")
WORKDRIVE = _p("REEL_FACTORY_WORKDRIVE", "/Volumes/WORKDRIVE")
WB = _p("REEL_FACTORY_WORKBENCH", os.path.join(WORKDRIVE, "workbench"))
SWEEP_ROOT = _p("CAPTION_SWEEP_ROOT", os.path.join(WB, "caption-sweep"))

FFMPEG = os.environ.get("FFMPEG") or shutil.which("ffmpeg") or "ffmpeg"
FFPROBE = os.environ.get("FFPROBE") or shutil.which("ffprobe") or "ffprobe"
