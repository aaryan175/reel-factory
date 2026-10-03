#!/usr/bin/env python3
"""Read-only Resolve/MCP readiness checker for the reel-production hybrid workflow."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

# All locations are overridable; defaults are the usual macOS install paths.
ROOT = Path(os.environ.get("RESOLVE_MCP_ROOT") or (Path.home() / "Library/Application Support/davinci-resolve-mcp")).expanduser()
PY = Path(os.environ.get("RESOLVE_MCP_PYTHON") or sys.executable).expanduser()
SERVER = ROOT / "src/server.py"
RESOLVE_APP = Path("/Applications/DaVinci Resolve/DaVinci Resolve.app")
RESOLVE_SCRIPTING = Path("/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting")
USER_SCRIPTING = Path.home() / "Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting"


def run(cmd: list[str], timeout: int = 60) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=timeout)
        return p.returncode, p.stdout
    except Exception as e:
        return 999, repr(e)


def main() -> int:
    checks = []

    def check(name: str, ok: bool, detail: str = ""):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})
        print(("PASS" if ok else "FAIL") + f" {name}" + (f" — {detail}" if detail else ""))

    check("ffmpeg on PATH", shutil.which("ffmpeg") is not None, shutil.which("ffmpeg") or "missing")
    check("ffprobe on PATH", shutil.which("ffprobe") is not None, shutil.which("ffprobe") or "missing")
    check("reelctl on PATH", shutil.which("reelctl") is not None, shutil.which("reelctl") or "missing")
    check("Resolve MCP managed root", ROOT.exists(), str(ROOT))
    check("Resolve MCP server file", SERVER.exists(), str(SERVER))
    check("MCP Python executable", PY.exists(), str(PY))

    if PY.exists() and SERVER.exists():
        code, out = run([str(PY), "-c", f"import sys; sys.path.insert(0,{str(ROOT)!r}); import src.server; print('import-ok')"], 30)
        check("Resolve MCP Python import", code == 0 and "import-ok" in out, out.strip()[:500])

    check("DaVinci Resolve app installed", RESOLVE_APP.exists(), str(RESOLVE_APP))
    scripting_path = next((p for p in (RESOLVE_SCRIPTING, USER_SCRIPTING) if p.exists()), RESOLVE_SCRIPTING)
    check("Resolve scripting API path", scripting_path.exists(), str(scripting_path))

    if scripting_path.exists():
        env = os.environ.copy()
        env["RESOLVE_SCRIPT_API"] = str(scripting_path)
        modules_path = str(scripting_path / "Modules")
        env["PYTHONPATH"] = modules_path + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        lib_path = RESOLVE_APP / "Contents/Libraries/Fusion/fusionscript.so"
        if lib_path.exists():
            env["RESOLVE_SCRIPT_LIB"] = str(lib_path)

        # Official external scripting only (DaVinciResolveScript.scriptapp). It must be enabled
        # in Resolve's preferences and supported by your Resolve edition; this checker never
        # works around an edition's scripting restrictions.
        script_code = (
            "import DaVinciResolveScript as d\n"
            "r = d.scriptapp('Resolve')\n"
            "print('connected=' + str(r is not None))\n"
            "print(r.GetProductName() + ' ' + r.GetVersionString() if r else '')\n"
        )
        try:
            p = subprocess.run(
                [sys.executable, "-c", script_code],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=30,
                env=env,
            )
            code, out = p.returncode, p.stdout
        except Exception as e:
            code, out = 999, repr(e)
        check(
            "Live Resolve external scripting",
            code == 0 and "connected=True" in out,
            out.strip()[:500] or "Resolve is not running or external scripting is not enabled/supported",
        )

    blockers = [c for c in checks if not c["ok"]]
    print("\nJSON_SUMMARY")
    print(json.dumps({"ok": not blockers, "blockers": blockers, "checks": checks}, indent=2))
    return 0 if not blockers else 2


if __name__ == "__main__":
    raise SystemExit(main())
