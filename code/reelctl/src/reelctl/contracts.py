from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Dict

from jsonschema import Draft202012Validator

from .hashing import load_json, sha256_file

SCHEMAS_ROOT = Path(__file__).resolve().parent / "schemas"


class ContractValidationError(ValueError):
    pass


@lru_cache(maxsize=None)
def load_schema(name: str) -> Dict[str, Any]:
    if not name or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789-_" for character in name):
        raise ContractValidationError(f"invalid schema name: {name!r}")
    path = SCHEMAS_ROOT / f"{name}.schema.json"
    if not path.is_file():
        raise ContractValidationError(f"unknown runtime schema: {name}")
    schema = load_json(path)
    Draft202012Validator.check_schema(schema)
    return schema


def schema_sha256(name: str) -> str:
    load_schema(name)
    return sha256_file(SCHEMAS_ROOT / f"{name}.schema.json")


def validate_contract(name: str, payload: Any) -> Dict[str, Any]:
    schema = load_schema(name)
    errors = sorted(Draft202012Validator(schema).iter_errors(payload), key=lambda error: list(error.absolute_path))
    if errors:
        details = []
        for error in errors[:20]:
            path = ".".join(str(part) for part in error.absolute_path) or "$"
            details.append(f"{path}: {error.message}")
        suffix = "" if len(errors) <= 20 else f"; plus {len(errors) - 20} more errors"
        raise ContractValidationError(f"{name} schema validation failed: {'; '.join(details)}{suffix}")
    return {
        "status": "PASS",
        "schema": name,
        "schema_sha256": schema_sha256(name),
    }
