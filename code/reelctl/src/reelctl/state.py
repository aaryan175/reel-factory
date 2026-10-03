from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from .hashing import atomic_write_json, load_json, recipe_hash, sha256_file, sha256_file_memoized
from .paths import PathSafetyError, confined_path, secure_mkdirs
from .signing import SignatureError, sign_payload, verify_payload

STAGES = [
    "REFERENCE_LOCKED",
    "BLUEPRINT_LOCKED",
    "FOOTAGE_INDEXED",
    "FEASIBILITY_REPORTED",
    "SELECTION_LOCKED",
    "ASSETS_LOCKED",
    "RENDERED",
    "TECHNICAL_QC",
    "STRUCTURE_QC",
    "VISUAL_QC",
    "LOCAL_REVIEW_READY",
    "HUMAN_APPROVED",
    "DELIVERY_APPROVED",
    "DELIVERED",
]

# QC reports can legitimately be reevaluated after a hash-bound visual review
# receipt is recorded. The candidate input is unchanged, but the immutable
# report artifact (and therefore its stage receipt) is intentionally new.
REFRESHABLE_SAME_INPUT_STAGES = frozenset({"TECHNICAL_QC", "STRUCTURE_QC", "VISUAL_QC"})


class StateError(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class ProjectState:
    def __init__(self, path: Path, data: Dict[str, Any]):
        self.path = Path(path).absolute()
        self.root = self.path.parent
        self.data = data

    @classmethod
    def create(cls, path: Path, project_id: str) -> "ProjectState":
        path = Path(path).absolute()
        secure_mkdirs(path.parent)
        data = {
            "schema_version": 3,
            "project_id": project_id,
            "created_at_utc": _now(),
            "updated_at_utc": _now(),
            "stages": {
                name: {
                    "status": "PENDING",
                    "input_hash": None,
                    "outputs": [],
                    "output_receipts": [],
                    "receipt": None,
                    "receipt_id": None,
                    "receipt_sha256": None,
                    "reason": None,
                    "updated_at_utc": None,
                }
                for name in STAGES
            },
        }
        instance = cls(path, data)
        instance.save()
        return instance

    @classmethod
    def load(cls, path: Path) -> "ProjectState":
        path = Path(path).absolute()
        data = load_json(path, root=path.parent)
        if int(data.get("schema_version", 0)) != 3:
            raise StateError("state schema is obsolete; create a fresh project or run an explicit migration")
        if list(data.get("stages", {})) != STAGES:
            raise StateError("state stage order/schema does not match this reelctl build")
        return cls(path, data)

    def save(self) -> None:
        self.data["updated_at_utc"] = _now()
        atomic_write_json(self.path, self.data, root=self.root)

    def stage(self, name: str) -> Dict[str, Any]:
        if name not in STAGES:
            raise StateError(f"unknown stage: {name}")
        return self.data["stages"][name]

    def _output_receipts(self, outputs: List[str]) -> List[Dict[str, Any]]:
        receipts: List[Dict[str, Any]] = []
        seen = set()
        for output in outputs:
            raw = Path(str(output)).expanduser()
            path = raw if raw.is_absolute() else self.root / raw
            try:
                safe = confined_path(self.root, path, require="file", allow_missing=False)
            except PathSafetyError as exc:
                raise StateError(f"stage output is unsafe or missing: {output}: {exc}") from exc
            relative = str(safe.relative_to(self.root))
            if relative in seen:
                raise StateError(f"stage output is duplicated: {relative}")
            seen.add(relative)
            receipts.append(
                {
                    "path": relative,
                    "bytes": safe.stat().st_size,
                    "sha256": sha256_file(safe),
                }
            )
        return receipts

    def verify_stage(self, name: str, *, _verified: Optional[Set[str]] = None) -> Dict[str, Any]:
        """Prove a stage's receipt and artifacts still are what completion recorded.

        ``_verified`` carries the stages already proven during one logical walk, and exists
        only so that walking the chain stays linear. Every verification recurses into its
        predecessor, so ``next_stage`` over eleven passed stages performed sixty-six of them
        and re-proved the earliest stage eleven times — 110,761 path resolutions and 6.6 GB
        of digests per studio daemon tick, as measured on a large project.

        The set is scoped to the call that created it and never outlives it, so nothing is
        remembered between two questions and no observer outside the walk sees a staler
        answer than before. The receipt-linkage check between neighbours is deliberately
        outside the guard: it is about this stage's own receipt, and it runs at every link.
        """
        if _verified is None:
            _verified = set()
        stage = self.stage(name)
        if stage["status"] != "PASS":
            raise StateError(f"stage {name} is not PASS: {stage['status']}")
        receipt_value = stage.get("receipt")
        receipt_sha = stage.get("receipt_sha256")
        if not receipt_value or not receipt_sha:
            raise StateError(f"stage {name} lacks an immutable receipt")
        try:
            receipt_path = confined_path(self.root, receipt_value, require="file", allow_missing=False)
        except PathSafetyError as exc:
            raise StateError(f"stage {name} receipt is unsafe or missing: {exc}") from exc
        if sha256_file_memoized(receipt_path) != receipt_sha:
            raise StateError(f"stage {name} receipt changed after completion")
        receipt = load_json(receipt_path, root=self.root)
        try:
            verify_payload(receipt, purpose="stage-receipt-v1")
        except SignatureError as exc:
            raise StateError(f"stage {name} receipt signature is invalid: {exc}") from exc
        if receipt.get("stage") != name or receipt.get("input_hash") != stage.get("input_hash"):
            raise StateError(f"stage {name} receipt no longer matches state")
        if recipe_hash({key: value for key, value in receipt.items() if key not in {"receipt_id", "signature"}}) != receipt.get(
            "receipt_id"
        ):
            raise StateError(f"stage {name} receipt identity is invalid")
        if receipt.get("receipt_id") != stage.get("receipt_id"):
            raise StateError(f"stage {name} receipt ID no longer matches state")
        index = STAGES.index(name)
        if index:
            predecessor_name = STAGES[index - 1]
            predecessor = self.stage(predecessor_name)
            if predecessor.get("status") != "PASS":
                raise StateError(f"stage {name} predecessor {predecessor_name} is not PASS")
            if predecessor_name not in _verified:
                self.verify_stage(predecessor_name, _verified=_verified)
            if receipt.get("predecessor_receipt_id") != predecessor.get("receipt_id"):
                raise StateError(f"stage {name} predecessor receipt linkage is invalid")
        elif receipt.get("predecessor_receipt_id") is not None:
            raise StateError(f"stage {name} unexpectedly has a predecessor receipt")
        for expected in receipt.get("outputs", []):
            try:
                path = confined_path(self.root, expected["path"], require="file", allow_missing=False)
            except PathSafetyError as exc:
                raise StateError(f"stage {name} output is unsafe or missing: {expected.get('path')}: {exc}") from exc
            if path.stat().st_size != int(expected["bytes"]) or sha256_file_memoized(path) != expected["sha256"]:
                raise StateError(f"stage {name} artifact changed after completion: {expected['path']}")
        _verified.add(name)
        return {"status": "PASS", "stage": name, "receipt": str(receipt_path), "outputs": receipt.get("outputs", [])}

    def require_predecessor(self, name: str) -> None:
        index = STAGES.index(name)
        if index == 0:
            return
        previous_name = STAGES[index - 1]
        previous = self.stage(previous_name)
        if previous["status"] != "PASS":
            raise StateError(f"cannot run {name}; prerequisite {previous_name} is {previous['status']}")
        self.verify_stage(previous_name)

    def complete(self, name: str, input_hash: str, outputs: List[str], status: str = "PASS", reason: Optional[str] = None) -> None:
        if status not in {"PASS", "FAIL", "BLOCKED"}:
            raise StateError(f"invalid completion status: {status}")
        if not isinstance(input_hash, str) or not input_hash.strip():
            raise StateError("stage input_hash must be a non-empty string")
        index = STAGES.index(name)
        self.require_predecessor(name)
        current = self.stage(name)
        output_receipts = self._output_receipts(outputs)

        same_input = current.get("input_hash") == input_hash
        refresh = False
        if current.get("status") == "PASS" and same_input:
            if current.get("output_receipts") == output_receipts:
                self.verify_stage(name)
                return
            if name not in REFRESHABLE_SAME_INPUT_STAGES:
                raise StateError(f"stage {name} has the same input hash but different output bytes")
            refresh = True

        changed = current.get("input_hash") not in {None, input_hash} or refresh
        predecessor_id = self.stage(STAGES[index - 1]).get("receipt_id") if index else None
        receipt_core = {
            "schema_version": 1,
            "stage": name,
            "status": status,
            "input_hash": input_hash,
            "predecessor_receipt_id": predecessor_id,
            "outputs": output_receipts,
            "reason": reason,
        }
        receipt_id = recipe_hash(receipt_core)
        receipt = sign_payload({**receipt_core, "receipt_id": receipt_id}, purpose="stage-receipt-v1")
        receipt_dir = secure_mkdirs(self.root, ".reelctl", "receipts", name.lower())
        receipt_path = receipt_dir / f"{receipt_id}.json"
        if receipt_path.exists():
            existing = load_json(receipt_path, root=self.root)
            if existing != receipt:
                raise StateError(f"immutable receipt collision for stage {name}")
        else:
            atomic_write_json(receipt_path, receipt, root=self.root)
        receipt_sha = sha256_file(receipt_path)
        current.update(
            {
                "status": status,
                "input_hash": input_hash,
                "outputs": list(outputs),
                "output_receipts": output_receipts,
                "receipt": str(receipt_path.relative_to(self.root)),
                "receipt_id": receipt_id,
                "receipt_sha256": receipt_sha,
                "reason": reason,
                "updated_at_utc": _now(),
            }
        )
        if changed:
            for downstream_name in STAGES[index + 1 :]:
                downstream = self.stage(downstream_name)
                if downstream["status"] != "PENDING":
                    downstream.update(
                        {
                            "status": "STALE",
                            "reason": f"upstream {name} recipe changed",
                            "updated_at_utc": _now(),
                        }
                    )
        self.save()

    def block(self, name: str, input_hash: str, reason: str, outputs: Optional[List[str]] = None) -> None:
        self.complete(name, input_hash, outputs or [], status="BLOCKED", reason=reason)

    def next_stage(self) -> Optional[str]:
        verified: Set[str] = set()
        for name in STAGES:
            stage = self.stage(name)
            if stage["status"] in {"PENDING", "STALE", "FAIL", "BLOCKED"}:
                return name
            if stage["status"] == "PASS":
                try:
                    self.verify_stage(name, _verified=verified)
                except StateError:
                    return name
        return None

    def summary(self) -> Dict[str, Any]:
        integrity: Dict[str, Any] = {}
        verified: Set[str] = set()
        for name in STAGES:
            if self.stage(name)["status"] == "PASS":
                try:
                    integrity[name] = self.verify_stage(name, _verified=verified)["status"]
                except StateError as exc:
                    integrity[name] = f"FAIL: {exc}"
        return {
            "project_id": self.data["project_id"],
            "next_stage": self.next_stage(),
            "stages": self.data["stages"],
            "integrity": integrity,
            "updated_at_utc": self.data["updated_at_utc"],
        }
