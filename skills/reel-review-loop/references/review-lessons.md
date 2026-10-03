# Review Loop Lessons

Format: **rule / why / check**.

**R1. Feedback is data with scope.**
Rule: record each comment verbatim with revision, frames and scope. Why: unscoped notes get over-applied ("never use blur") or forgotten. Check: every applied change cites a feedback ID.

**R2. Revoke first, investigate second.**
Rule: when a reviewer says it looks wrong, mark the visual pass revoked before analysing. Why: a defended pass anchors the investigation on the builder's evidence instead of the viewer's experience. Check: verdict file shows `visual_parity: FAIL|PENDING_REVIEW` with the feedback ID.

**R3. Smallest upstream fix, full downstream rerun.**
Rule: change one contract at the highest layer that explains the complaint, then re-run every gate below it. Why: patching the output leaves the cause; partial reruns leave stale passes. Check: invalidation list matches the dependency graph.

**R4. Tests must be red-capable.**
Rule: each regression test ships with a passing control and a failing holdout. Why: a builder and checker sharing a bug agree forever. Check: CI runs the holdout and expects failure.

**R5. Rejected stays rejected.**
Rule: keep rejected renders as named negative fixtures and never reuse them as templates. Why: the defect propagates silently. Check: no source manifest cites a `*-rejected` file.

**R6. Restore, don't re-derive, what was approved.**
Rule: if a reviewer says an earlier version of an element was right, restore that exact asset byte-for-byte. Why: re-deriving drifts. Check: asset hash equals the earlier approved hash.

**R7. Approval is explicit and bound.**
Rule: approval names a file hash, destination and action. Why: "looks good" on v003 is not approval for v004 or for publishing. Check: approval record contains all three fields.

**R8. Readback after every outward action.**
Rule: confirm remote ID, size and checksum after upload; confirm sharing state separately. Why: a CLI "success" can mean a partial or duplicated object. Check: checksum readback logged.

**R9. Status counts are separate.**
Rule: report distinct concepts, rendered revisions, approved, uploaded and published as separate numbers. Why: re-encodes and variants inflate counts and blur what is actually done. Check: dedupe key per concept documented.

**R10. Liveness from processes, not files.**
Rule: "rendering now" requires a matching live process or an active lease. Why: a fresh mtime may be a finished or crashed job. Check: process list or lease table cited.

**R11. Compensate, don't erase.**
Rule: fix a wrong state change with a new event that restores the correct state. Why: history explains the correction and protects against repeats. Check: event log shows both the mistake and the compensation.

**R12. Bounded correction passes.**
Rule: after two correction passes without convergence, step back to blueprint, footage or mode. Why: repeated local patches on a structural problem burn time. Check: pass counter per candidate.

## Feedback translation table

| Reviewer says | Observable contract to check |
|---|---|
| "feels slow / flat" | cut density in named spans, onset deltas, action-peak timing within slots |
| "the font changed" | face index, size, baseline and ink per caption state across the reel |
| "too dull" | exposure, white balance, density, contrast - before saturation |
| "looks fake / glitchy" | effect operator: real-frame recall vs synthetic; one-frame pulse discipline |
| "doesn't match the reference" | picture-block count, hard cuts, caption states and roles in a paired board |
| "hard to read" | alpha-aware contrast on the composited caption, shadow/underlay |
