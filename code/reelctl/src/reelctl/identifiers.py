from __future__ import annotations

import re

_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")


class IdentifierError(ValueError):
    pass


def validate_identifier(value: str, *, kind: str = "identifier") -> str:
    text = str(value)
    if not _PATTERN.fullmatch(text) or text in {".", ".."} or ".." in text:
        raise IdentifierError(
            f"invalid {kind} identifier {text!r}; use 1-80 letters, digits, dots, underscores, or hyphens with no path components"
        )
    return text
