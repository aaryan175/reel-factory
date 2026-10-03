# Production Lessons

Condensed, generic lessons for reference-led reel builds. Format: **rule / why / check**.

## Pitfalls

**P1. Audio identity is not musical sync.**
Rule: prove sync with onset deltas per cut plus a normal-speed watch. Why: the same song under different cuts can feel late. Check: an onset-delta table exists for every creative cut.

**P2. Shot count is not picture-state count.**
Rule: rebuild against the picture-block ledger. Why: merging held states and pulses into shots makes the edit feel mistimed. Check: candidate block count equals reference block count (exact mode).

**P3. One LUT is not a grade plan.**
Rule: normalize by source profile, then look, then per-shot trim. Why: mixed sources through one transform drift apart. Check: each shot's input profile is recorded and proven.

**P4. Candidate-only QC is not visual parity.**
Rule: visual verdicts compare reference and candidate at the same frames. Why: a clean candidate can still be nothing like the reference. Check: paired exact-frame boards exist.

**P5. Chat history is not state.**
Rule: keep project, stage and receipt state on disk; resume from disk. Why: a new session loses context and may re-run or skip stages. Check: `status` from disk alone names the next action.

**P6. No per-revision one-off scripts.**
Rule: change the project contract and re-run the shared pipeline. Why: one-off scripts fork behaviour and cannot be re-audited. Check: every render cites the pipeline version and contract hash.

**P7. A rejected reel is not a template.**
Rule: rejected outputs become negative fixtures, never starting points. Why: they carry the defect forward. Check: no selection or asset cites a rejected revision as a source.

**P8. Path, size and mtime are not identity.**
Rule: content-hash every source at index, selection and render. Why: files get replaced in place. Check: render receipt hash == selection hash == index hash.

**P9. Feasibility is a separate stage.**
Rule: lock feasibility before selection. Why: folding it into selection lets weak matches through quietly. Check: a locked feasibility manifest predates the selection file.

**P10. Nominal limited range is not decoded proof.**
Rule: measure range on the decoded output. Why: tags do not move pixels. Check: `signalstats` on the decoded review shows Y within 16-235 (8-bit).

**P11. Never overwrite the first blocked QC report.**
Rule: re-runs write new, suffixed reports. Why: the first failure is the evidence. Check: report directory is append-only.

**P12. "Completed" from a background job is a notification.**
Rule: validate outputs before promoting a job result. Why: async jobs finish on stale inputs or with missing files. Check: output hashes and input snapshot match the active revision.

**P13. Literal 1:1 is never promised before feasibility.**
Rule: say "exact" only after >= 80% exact coverage is locked. Why: the library may not contain the scenes. Check: feasibility ratio is quoted with the promise.

**P14. Never invent CLI flags from prose.**
Rule: read the live `--help` of every tool before use. Why: documentation drifts from the installed build. Check: commands in the log match `--help` output.

## Revision recovery (when a review says "this is wrong")

1. Stop outward changes; preserve the rejected render as a negative fixture.
2. Translate the complaint into observable contracts (missing blocks, wrong glyph raster, blanket grade, repeated source, wrong effect direction).
3. Audit reference and candidate together at exact frames and at normal speed.
4. Revoke any visual pass that the complaint contradicts - do not defend it with hashes.
5. Change the smallest authoritative upstream contract.
6. Re-render the changed scope; re-run the complete final gate.
7. Do not call a patch a clean rebuild if it inherits the same collapsed blueprint or guessed assets.

## One-slot repairs

To replace one weak slot in an otherwise accepted master, require three conjunctive gates: structural (same boundaries), source-window legality (inside the authorized root, enough handle, no reuse), and editorial proof (verb readable on frame one, composited with the real caption, watched at speed). A solver's top survivor is not automatically the winner. Prove scope on an intra-frame master: `framemd5` diff shows only the allowed frames changed.

## Durable control state

If a queue or UI drives production, use a transactional store (e.g. SQLite in WAL mode with full sync) as the authority, leases with heartbeats (e.g. 15 s heartbeat on a 60 s TTL), and never infer liveness from a PID file or mtime. Dashboards project that store; they do not own state.

## Cycle-time planning (estimates, not commitments)

- First strict build to local review: roughly 5-8 active hours; steady state 3-5.
- A complex new reference forensics pack adds roughly 2-4.5 hours.
- Keep work-in-progress to one strict reel at a time, with at most two bounded correction passes after first review.
- After five comparable reels, replace these with your own median and p80.
