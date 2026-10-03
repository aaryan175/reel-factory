---
name: reel-review-loop
description: Run the human review loop for reels - hand over candidates, turn reviewer feedback into scoped feedback records, lessons and regression tests, recover from rejections without losing evidence, keep status dashboards honest, and operate behind explicit approval gates for upload, sharing and publishing. Use when a reviewer comments on a reel, when asked "what's the status of the reels?", when preparing a handoff, or before any outward action (upload, share, post).
---

# Reel Review Loop

The builder's ceiling is **local review**. Everything after - upload, replacement, sharing, publishing - is a separate action that needs an explicit go bound to the exact file, destination and action.

## 1. Handoff

- Lead with the playable file and the exact revision name (e.g. `v003`), then receipts. One file per candidate unless asked for more.
- Match the request: "just the video" -> the file, no audit dump. "What do you think?" -> APPROVE / REVISE / REJECT first, then concrete strengths, release blockers and the smallest correct intervention. "How are you making it?" -> viewer-facing outcome first, 4-7 visible qualities, then a short pipeline note.
- State the evidence tier of your own review (full AV watch, frames only, etc.).

## 2. Feedback becomes records, not memory

For every review comment, write an append-only `FeedbackEvent`:

```json
{"id": "fb-0007", "revision": "v003", "verbatim": "<reviewer's exact words>",
 "scope": "this_reel|this_reference|class_level", "frames": [112, 140],
 "contract_changes": ["caption c004 ink -> white", "block p009 replace: verb not readable"],
 "status": "OPEN|APPLIED|VERIFIED|WONT_FIX"}
```

- Keep the reviewer's words verbatim; your paraphrase goes in `contract_changes`.
- Translate taste into observable contracts: "feels slow" -> cut density in named spans + onset deltas; "font looks off" -> face/size/baseline audit at cited frames; "too dull" -> exposure/WB/density diagnosis (not "add saturation").
- Promote a feedback item to a reusable **lesson** only when the reviewer confirms it applies beyond this reel. Lessons are rule / why / check, and each gets a test or a checklist line that can fail.

## 3. Lessons -> tests

- Every lesson that can be checked mechanically becomes a regression test with a **positive control** (a known-good case that passes) and a **holdout** (a known-bad case that must fail). A test that cannot go red is decoration.
- Rejected renders become negative fixtures; approved renders become immutable baselines. Never edit either.
- Triage new lessons before closing a job: each is `encoded-as-test`, `encoded-as-checklist`, or `prose-only` (with a reason).

## 4. Rejection recovery

1. Stop outward changes; preserve the rejected candidate under a `*-rejected` name.
2. Revoke any visual pass the feedback contradicts - immediately, without defending it with hashes.
3. Audit reference and candidate at the cited frames and at normal speed.
4. Change the smallest authoritative upstream contract; invalidate everything downstream of it.
5. Re-render the changed scope; re-run the **complete** final gate (not just the test that failed).
6. Hand over the new revision with a one-line "what changed".

Limit: at most two bounded correction passes after first review before stepping back to re-check the blueprint, footage or mode.

## 5. Reviewer disagreement and delegated reviews

Never majority-vote reviewers. A reviewer that could not ingest the media produced `REVIEW_INCONCLUSIVE`, not a vote. A review done against the wrong reference or contract is a `CONTRACT_PREMISE_ERROR`. Authority order when results conflict: owner decision > active receipt > creative review > technical review > tool exit code.

## 6. Approval gates (outward actions)

Before any action visible to others or hard to undo:

- Was this exact action requested, for this exact file hash and destination?
- Is the candidate's human approval recorded (not inferred from a file existing or a test passing)?
- After an approved upload: read back the remote object ID, size and checksum, and its sharing state. Making a link public is a separate sharing action.

Status ladder - keep each distinct: rendered -> QC-passed -> approved -> uploaded -> shared -> published.

## 7. Honest status and dashboards

- Real reel names first; IDs and hashes secondary. The complete roster is visible from the home view.
- Sections: current work / needs your decision / to redo-parked / not started / history. "Not started" (intake only) is never called "cancelled"; "rejected" only after an explicit rejection.
- "Rendering now" is checked against live processes, not file mtimes. "Ready" requires a proven playable file.
- Never infer a destructive state change from a typo, an audit finding or a previous summary. If a wrong change was made, add an append-only compensating event, explain it plainly, and verify the state survives a service restart.

Patterns and lessons: `references/review-lessons.md`.
