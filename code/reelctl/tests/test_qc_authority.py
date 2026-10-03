from __future__ import annotations

from reelctl.qc import combine_authorities


def test_technical_pass_cannot_hide_structure_or_visual_failure() -> None:
    report = combine_authorities(
        technical="PASS",
        structure="FAIL",
        visual="FAIL",
        human="REJECTED",
    )
    assert report["overall"] == "REJECT"
    assert report["local_review_ready"] is False


def test_machine_pass_stops_at_local_review_pending_human() -> None:
    report = combine_authorities(
        technical="PASS",
        structure="PASS",
        visual="PASS",
        human="PENDING",
    )
    assert report["overall"] == "LOCAL_REVIEW_READY"
    assert report["publishable"] is False


def test_missing_agent_visual_receipt_is_blocked_not_rejected() -> None:
    report = combine_authorities(
        technical="PASS",
        structure="PASS",
        visual="BLOCKED",
        human="PENDING",
    )
    assert report["overall"] == "BLOCKED"
    assert report["local_review_ready"] is False


def test_publication_requires_human_approval() -> None:
    report = combine_authorities(
        technical="PASS",
        structure="PASS",
        visual="PASS",
        human="APPROVED",
    )
    assert report["overall"] == "APPROVED"
    assert report["publishable"] is True
