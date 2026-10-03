from __future__ import annotations

import pytest

from reelctl.identifiers import IdentifierError, validate_identifier


def test_project_and_revision_identifiers_cannot_escape_roots() -> None:
    for value in ("../escape", "a/b", "..", ".hidden", "", "name with spaces"):
        with pytest.raises(IdentifierError):
            validate_identifier(value, kind="project")
    assert validate_identifier("REEL-09", kind="project") == "REEL-09"
    assert validate_identifier("v001", kind="revision") == "v001"
