from __future__ import annotations

from typing import Any, Dict, List, Tuple


class TrialFamilyError(ValueError):
    pass


def _slot_map(slots: List[Dict[str, Any]], *, owner: str) -> Dict[str, Dict[str, Any]]:
    mapped: Dict[str, Dict[str, Any]] = {}
    for slot in slots:
        slot_id = str(slot["slot_id"])
        if slot_id in mapped:
            raise TrialFamilyError(f"{owner} repeats slot_id {slot_id}")
        start = int(slot["source_start_frame"])
        end = int(slot["source_end_frame_exclusive"])
        frames = int(slot["frames"])
        if end <= start:
            raise TrialFamilyError(f"{owner} slot {slot_id} has an empty or reversed source interval")
        if end - start < frames:
            raise TrialFamilyError(f"{owner} slot {slot_id} source interval is shorter than its output frame count")
        mapped[slot_id] = slot
    return mapped


def _visual_source_signature(slot: Dict[str, Any]) -> str:
    """Return clip identity, deliberately excluding in-point and metadata changes.

    A new interval from the same source can be a useful editorial revision, but it
    does not satisfy this system's conservative new-clip rule for Trial families.
    """

    return str(slot["source_sha256"])


def _clip_package_signature(slots: Dict[str, Dict[str, Any]], order: List[str]) -> Tuple[str, ...]:
    return tuple(_visual_source_signature(slots[slot_id]) for slot_id in order)


def validate_trial_family(payload: Dict[str, Any]) -> Dict[str, Any]:
    master_slots = _slot_map(list(payload["master"]["slots"]), owner="master")
    master_order = list(master_slots)
    policy = payload["policy"]
    variants = list(payload["variants"])
    minimum_variants = int(policy["minimum_variants"])
    if len(variants) < minimum_variants:
        raise TrialFamilyError(f"family requires at least {minimum_variants} variants")

    opening_slot_id = str(policy["opening_slot_id"])
    if opening_slot_id not in master_slots:
        raise TrialFamilyError(f"opening slot {opening_slot_id} is missing from master")

    required_changed_slots = int(policy["minimum_changed_video_slots"])
    required_changed_ratio = float(policy["minimum_changed_visual_ratio"])
    seen_variant_ids = set()
    seen_packages: Dict[Tuple[str, ...], str] = {}
    seen_opening_sources = set()
    changed_counts: List[int] = []
    changed_ratios: List[float] = []
    render_hashes: List[str] = []

    for variant in variants:
        variant_id = str(variant["variant_id"])
        if variant_id in seen_variant_ids:
            raise TrialFamilyError(f"duplicate variant_id {variant_id}")
        seen_variant_ids.add(variant_id)

        variant_slots = _slot_map(list(variant["slots"]), owner=f"variant {variant_id}")
        if set(variant_slots) != set(master_slots):
            missing = sorted(set(master_slots) - set(variant_slots))
            extra = sorted(set(variant_slots) - set(master_slots))
            raise TrialFamilyError(f"variant {variant_id} slot contract differs from master: missing={missing}, extra={extra}")

        for slot_id in master_order:
            master = master_slots[slot_id]
            candidate = variant_slots[slot_id]
            if candidate["role"] != master["role"] or int(candidate["frames"]) != int(master["frames"]):
                raise TrialFamilyError(f"variant {variant_id} changed locked role/frame contract for {slot_id}")

        package = _clip_package_signature(variant_slots, master_order)
        duplicate_of = seen_packages.get(package)
        if duplicate_of is not None:
            raise TrialFamilyError(f"variant {variant_id} is a duplicate clip package of {duplicate_of}")

        opening_source = _visual_source_signature(variant_slots[opening_slot_id])
        master_opening_source = _visual_source_signature(master_slots[opening_slot_id])
        if opening_source == master_opening_source:
            raise TrialFamilyError(f"variant {variant_id} must use a new opening clip")
        if bool(policy["unique_opening_source_per_variant"]) and opening_source in seen_opening_sources:
            raise TrialFamilyError(f"variant {variant_id} reuses a sibling opening clip")

        changed = [
            slot_id
            for slot_id in master_order
            if _visual_source_signature(variant_slots[slot_id]) != _visual_source_signature(master_slots[slot_id])
        ]
        changed_count = len(changed)
        changed_ratio = changed_count / len(master_order)
        changed_counts.append(changed_count)
        changed_ratios.append(changed_ratio)
        if changed_count < required_changed_slots:
            raise TrialFamilyError(
                f"variant {variant_id} changes only {changed_count} video slots; at least {required_changed_slots} new clips are required"
            )
        if changed_ratio + 1e-12 < required_changed_ratio:
            raise TrialFamilyError(
                f"variant {variant_id} changes {changed_ratio:.3f} of visual slots; at least {required_changed_ratio:.3f} is required"
            )

        seen_packages[package] = variant_id
        seen_opening_sources.add(opening_source)

        review = variant["review"]
        if payload["family_state"] == "LOCAL_REVIEW_READY":
            ready = (
                review["status"] == "LOCAL_REVIEW_READY"
                and review["technical_qc"] == "PASS"
                and review["visual_qc"] == "PASS"
                and bool(review["normal_speed_full_watch"])
                and bool(review["render_sha256"])
            )
            if not ready:
                raise TrialFamilyError(f"variant {variant_id} is not fully QC'd for family LOCAL_REVIEW_READY")
            render_hashes.append(str(review["render_sha256"]))

    render_hashes_unique = len(render_hashes) == len(set(render_hashes))
    if payload["family_state"] == "LOCAL_REVIEW_READY" and not render_hashes_unique:
        raise TrialFamilyError("LOCAL_REVIEW_READY variants must have unique render hashes")

    return {
        "status": "PASS",
        "family_id": payload["family_id"],
        "family_state": payload["family_state"],
        "variant_count": len(variants),
        "minimum_required_variants": minimum_variants,
        "minimum_observed_changed_video_slots": min(changed_counts),
        "minimum_observed_changed_visual_ratio": min(changed_ratios),
        "opening_sources_unique": len(seen_opening_sources) == len(variants),
        "clip_packages_unique": len(seen_packages) == len(variants),
        "render_hashes_unique": render_hashes_unique,
        "publication_allowed": False,
        "warning": (
            "PASS proves compliance with the local conservative variant contract only. "
            "It is a creative-variety check, not a statement about how any platform will treat the variants; "
            "follow each platform's own rules on reposted or similar content."
        ),
    }
