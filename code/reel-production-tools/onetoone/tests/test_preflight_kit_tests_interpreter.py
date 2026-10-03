"""L0051: kit_tests() must run pytest with an interpreter that actually has pytest installed.

Regression: kit_tests() used to shell out via a bare interpreter that had no pytest installed,
so every --finish gate printed a false 'kit tests FAILED: ?' even when the real suite was green.
"""
import subprocess
import sys
from pathlib import Path

from onetoone import preflight


def test_kit_tests_uses_an_interpreter_with_pytest_installed():
    py = preflight.kit_python()
    r = subprocess.run([py, "-c", "import pytest"], capture_output=True, text=True)
    assert r.returncode == 0, (
        f"kit_tests()'s chosen interpreter ({py}) has no pytest installed -- "
        f"this is exactly the L0051 false-negative bug. stderr: {r.stderr}"
    )


def test_kit_tests_does_not_hardcode_sys_executable():
    src = Path(preflight.__file__).read_text()
    assert '[sys.executable, "-m", "pytest"' not in src, (
        "kit_tests() reverted to hardcoding sys.executable (L0051 regression)"
    )
