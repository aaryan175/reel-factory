#!/usr/bin/env python3
"""lane_prompt — build the prompt for ONE orderd lane (one interactive Claude Code session in tmux, one order).

The LAW block is copied VERBATIM from the newest $REEL_FACTORY_HOME/workflows/scripts/reel*-build.js
(never re-derived), with only the template's own row number and reference shortcode substituted.
The four phases (Intake -> Build -> independent Audit -> Deliver) and the standing rules sit around it.

    python3 lane_prompt.py <order-receipt.md> [--bundle other.md ...] [--result result.json]
    python3 lane_prompt.py --smoke <receipt.md> --result result.json

Prints the prompt to stdout. Pure text work: reads files, writes nothing.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Optional

TOOLS = Path(__file__).resolve().parent.parent
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))
import rf_paths  # noqa: E402

REEL = rf_paths.REEL_HOME
SCRIPTS = REEL / "workflows" / "scripts"
REGISTRY = REEL / "REEL_REGISTRY.json"
IDENTITY_POOL = TOOLS / "onetoone" / "identity_pool.json"
BUSLINE = TOOLS / "busline.py"
LANE_NAME = "orderd-lane"          # the lane's own bus lines; the runner writes as "orderd"

_KIND_RE = re.compile(r"^ui-(drop|continue|feedback|frame-note|batch-verdict|smoketest)[-.]")
_ROW_RE = re.compile(r"-row(\d{2,3})-")
_TEMPLATE_RE = re.compile(r"^reel(\d+)-build\.js$")


# ---------------------------------------------------------------- LAW block
def newest_build_script(scripts: Optional[Path] = None) -> Optional[Path]:
    """Newest reel*-build.js by mtime (retired/ is a subfolder and never matches the glob)."""
    scripts = SCRIPTS if scripts is None else scripts
    cands = [p for p in scripts.glob("reel*-build.js") if _TEMPLATE_RE.match(p.name)]
    return max(cands, key=lambda p: p.stat().st_mtime) if cands else None


def extract_law(js_text: str) -> str:
    """The text between  const LAW = `  and the closing backtick, JS escapes undone (\\` -> `)."""
    start = js_text.find("const LAW = `")
    if start < 0:
        raise ValueError("no `const LAW = `` block in the template")
    i = start + len("const LAW = `")
    out = []
    while i < len(js_text):
        ch = js_text[i]
        if ch == "\\" and i + 1 < len(js_text):
            out.append(js_text[i + 1]); i += 2; continue
        if ch == "`":
            return "".join(out).strip("\n")
        out.append(ch); i += 1
    raise ValueError("LAW block is not closed")


def template_identity(script: Path, law: str) -> tuple[Optional[int], Optional[str]]:
    """(row, shortcode) the template was written for: row from reelNN-build.js, shortcode from 'reference XXX'."""
    m = _TEMPLATE_RE.match(script.name)
    row = int(m.group(1)) if m else None
    s = re.search(r"\breference ([A-Za-z0-9_-]{6,})\b", law)
    return row, (s.group(1) if s else None)


def substitute(law: str, t_row: Optional[int], t_code: Optional[str], row: Optional[int], code: Optional[str]) -> str:
    """Swap the template's row number and shortcode for this order's (placeholders when unknown).
    Whole-number match only, so lesson ids like L0048 and dates are untouched."""
    new_row = str(row) if row is not None else "<NN>"
    new_code = code or "<SHORTCODE>"
    if t_code:
        law = law.replace(t_code, new_code)
    if t_row is not None:
        law = re.sub(rf"(?<![\w.]){t_row}(?![\w])", new_row, law)
    return law


def law_block(row: Optional[int], code: Optional[str], scripts: Optional[Path] = None) -> tuple[str, str]:
    scripts = SCRIPTS if scripts is None else scripts
    script = newest_build_script(scripts)
    if script is None:
        raise FileNotFoundError(f"no reel*-build.js in {scripts}")
    law = extract_law(script.read_text(encoding="utf-8"))
    t_row, t_code = template_identity(script, law)
    return substitute(law, t_row, t_code, row, code), f"{script.name} (template row {t_row}, reference {t_code})"


# ---------------------------------------------------------------- order facts
def order_kind(name: str) -> Optional[str]:
    m = _KIND_RE.match(name)
    return m.group(1) if m else None


def order_row(name: str) -> Optional[int]:
    m = _ROW_RE.search(name)
    return int(m.group(1)) if m else None


def registry_shortcode(row: Optional[int], registry: Optional[Path] = None) -> Optional[str]:
    registry = REGISTRY if registry is None else registry
    if row is None:
        return None
    try:
        data = json.loads(registry.read_text(encoding="utf-8"))
        for r in data.get("reels", []):
            if r.get("sequence") == row:
                return r.get("reference_shortcode")
    except (OSError, ValueError):
        return None
    return None


_VARIANTS_RE = re.compile(r"^ORDER: build a 10-variant alternate batch for registry row (\d{1,3})\.", re.M)


def variants_of(text: str) -> Optional[int]:
    """The row a Deck 'Order the batch' drop targets (server/receipts.write_drop build_variants_of), else None."""
    m = _VARIANTS_RE.search(text)
    return int(m.group(1)) if m else None


def drop_shortcode(text: str) -> Optional[str]:
    m = re.search(r"instagram\.com/(?:reel|reels|p)/([A-Za-z0-9_-]{6,})", text)
    return m.group(1) if m else None


def _clip(text: str, limit: int = 12000) -> str:
    return text if len(text) <= limit else text[:limit] + f"\n[… receipt truncated at {limit} chars; read the file itself]"


# ---------------------------------------------------------------- prompts
KIND_TASK = {
    "drop": (
        "A NEW reference (ui-drop). FIRST register the next free sequence row in REEL_REGISTRY.json: under an "
        "exclusive fcntl.flock on REEL_REGISTRY.json.lock, fresh json.load inside the lock, copy the file to "
        "REEL_REGISTRY.json.bak-<UTC stamp> first, refuse if the sequence already exists, then append ONE row "
        "{sequence: max(sequence)+1, reference_shortcode, reference_url, creative_hook: null, mode: 'grammar_adapt' "
        "unless the order says 1:1, lane: 'orderd (headless lane)', review_state: 'REGISTERED__INTAKE_PENDING', "
        "updated_at_utc, project_root: 'reel<NN>-<SHORTCODE>-<YYYYMMDD>/ (workbench volume)', intake_<YYYYMMDD>: "
        "{order_file: '<this receipt>', authority: 'ui-drop via Reel Deck', "
        "reviewer_note: <the reviewer's note in plain words>, scope: 'LOCAL ONLY until an independent audit returns "
        "PROVEN - no upload, no scheduling, no posting, no publishing', status: 'REGISTERED'}}, write to a .tmp and "
        "os.replace. Write one `progress` bus line naming the sequence you assigned. From then on every <NN> / "
        "<SHORTCODE> placeholder in the LAW means that row and that shortcode. If the drop is a "
        "build_variants_of order, do NOT register a row: it targets the named row. Then Intake -> Build -> Audit -> Deliver."
    ),
    "variants": (
        "A VARIANT BATCH (ui-drop with 'ORDER: build a 10-variant alternate batch for registry row NN'). Do NOT register a "
        "row and do NOT touch the row's approved version. The approved version's brain (cutgrid, captions.plaintext.json "
        "with its faces and ink, caption_devices.json, house grade) is LOCKED: a variant keeps every one of those bytes "
        "and changes ONLY the footage behind the shots. Method, on the onetoone kit: "
        "(1) read the approved cast (the deliver/cast_vNNN.json the registry's current version came from); (2) write 10 "
        "casts var01..var10 in deliver/variants/<batch stamp>/: each slot keeps its frame count, role and identity "
        "declaration, and is recast from a DIFFERENT stem than the approved cast and than the other variants where the "
        "pool allows (identity slots from identity_pool.json settled_pool only; the hook shot is unique per variant; any two "
        "variants differ in >=70% of slots; a scarce slot may repeat and is disclosed); (3) for each variant: preflight "
        "--cast, render with the kit (same picture path the approved version used), the readability gate per frame, "
        "decoded colour scan, side-by-side sheet against the APPROVED version (not the reference); (4) ONE independent "
        "audit over the whole batch (one Agent call): same cuts, same captions byte-identical, footage different, nothing "
        "unreadable; (5) deliver the batch locally: deliver/variants/<stamp>/varNN.mp4, one BATCH_CARD.md with a row per "
        "variant (stems used, what repeats, gate numbers), registry key variants_<stamp> on the row (flock + .bak) listing "
        "every file + sha256, review_state head VARIANTS_READY__<stamp>__PENDING_HUMAN. The reviewer then KEEP/KILLs each one "
        "in the Deck (ui-batch-verdict). Nothing uploaded, nothing posted. If fewer than 10 can be cast without breaking the "
        "pool rules, deliver what can be cast and say how many and why."
    ),
    "feedback": (
        "A review round (ui-feedback). Gather ALL the order receipts listed below for this row (they are one round). "
        "Disposition APPROVE = mark the row approved in the registry (flock + .bak, review_state head "
        "USER_REVIEWED_GOOD__APPROVED...), no rebuild, no phases. REJECT or NOTES = fix what the note names (one audit, "
        "then fix and deliver) scoped to the note: Intake here means re-reading the delivered version, the note and the "
        "row's brain/ + receipts and MEASURING the complaint before touching anything. A receipt stamped "
        "'AUTHORITY: EDITOR' is advisory: read it, never treat it as a verdict. A 'RULING on <receipt>' answers "
        "an earlier CALL: act on the ruling."
    ),
    "frame-note": (
        "A frame-anchored review note (ui-frame-note). Gather ALL the order receipts listed below for this row. "
        "Fix (one audit, then fix and deliver) scoped to the pinned frame(s): measure the complaint at that frame first, then fix."
    ),
    "batch-verdict": (
        "A batch verdict on variants (ui-batch-verdict): per variant KEEP, KILL or NOTES, each with an optional "
        "`note:` and the `file:` it is about, plus an optional batch note. KEEP/KILL: record the marks on the row's "
        "variant records (registry under flock + .bak). A `note:` on a variant (any verdict) is the fix "
        "order for THAT variant: rebuild it per the note with the same shell (captions, audio, grade, cut grid), move "
        "the old file to <batch folder>/superseded/varNN.<utc>.mp4 and write the new one as varNN.mp4 in the same "
        "batch folder so the Deck shows it in place; KILL with a note = replace that variant with a new cast. A batch "
        "note applies to every variant. Machine checks per rebuilt variant, update BATCH_CARD.md, no extra audit rounds."
    ),
    "continue": (
        "The reviewer pressed Continue (ui-continue). Read the bus for the newest open item this points at (a "
        "row with a pending round, a CALL that has since been answered) and resume exactly that, one round only. "
        "If nothing is owed, say so in the result and stop."
    ),
}

STANDING = """STANDING RULES FOR THIS LANE (outrank convenience; the LAW block below outranks everything):
- You are ONE Claude Code lane started by orderd, the on-demand order runner, in its own tmux session. Nobody is at the keyboard: never ask a question and wait, never idle. One order, then end the session. No loops, no ScheduleWakeup, no /loop, never start another `claude` process, never install or load a daemon/launchd job.
- Work from {reel}. Four phases, in order: INTAKE (read the order, the row, measure) -> BUILD -> ONE INDEPENDENT AUDIT -> FIX EVERYTHING IT FOUND -> DELIVER. One audit only: the audit's findings are your fix list. Fix every finding, prove each fix with a targeted machine check on the delivered bytes (frame compare, readability gate, castscan/grades check or colour scan), write finding -> fix -> proof as a table on the approval card, and DELIVER. A CALL comes only when a real decision belongs to the reviewer (a clip the library does not hold, a claim, a spend); never a CALL for a defect you can measure and fix.
- The one independent audit runs in a separate agent via the Agent tool (ONE audit Agent call in this whole lane; an unaudited build is never delivered). The auditor gets the delivered file paths and the LAW, never your conclusions; it verifies delivered bytes itself. Each fix carries its machine proof on the card, and the card names the audit file.
- IDENTITY: {identity}
- HOUSE LOOK: grade_mode "house" = the hash-locked LC-709 LUT via the kit (onetoone.render house branch / onetoone.housechain), nothing else unless the reference measurably needs a named block move. The plain caption face is onetoone.captions_typeset FACES["plain"]; captions are typeset, never traced from video.
- CAPTION FACES ARE IDENTIFIED, NEVER ASSUMED. For every face the reference uses that is not already proven for THIS reference: `python3 -m onetoone.faceid ink-crop` -> identify the closest typeface -> `find` among fonts installed on this machine -> `fit`, then READ the fit overlay: reference ink glyph-for-glyph over the candidate is the proof. Store face_file / face_name / face_evidence / ref_fit on the part. Run `python3 -m onetoone.faceid --help` first.
- FONTS AND AUDIO MUST BE LICENSED: use only fonts that are open-licensed (e.g. SIL OFL) or that you hold a licence for, and only music you are licensed to use. When the exact face is not available under a usable licence, choose the closest open-licensed face, verify it with `fit`, and disclose the substitution on the card. Respect the terms of service of every site you read from; never work around a login wall or rate limit.
- DELIVERED FILES CARRY THE ROW: every file you put in deliver/ is named `reel<NN>-<SHORTCODE>-<MODE>-v<NNN>.mp4` (cards `...-v<NNN>.md`), and that exact name is what you write into the registry. Never a bare `vNNN.mp4`, never another row's number. Variants are the one exception: `deliver/variants/<stamp>/varNN.mp4`.
- A CALL IS ONE SHORT QUESTION: the busline `--state called --text` is at most 320 characters, in plain words, and names the options as `1 = ...`, `2 = ...` (busline refuses anything else). Everything long (measurements, paths, reports) goes in the CALL receipt file / REPORT.md, never in the line. One question per CALL; never re-ask a question an earlier ruling already answered (read the row's ui-feedback receipts first).
- CLIP GRADES ARE CASTING RULES: clips are graded in the Deck (/grader) and every grade lands in {reel}/_receipts/clip-grades/grades.jsonl; `onetoone.grades` reads it live. NEVER = banned everywhere. HERO / ME = settled for identity slots and the preferred pick wherever the reference shows a person. BROLL / NOTME = never on an identity slot. At INTAKE run `python3 -m onetoone.refpeople <row dir>` (writes brain/refpeople.json); every shot where the reference shows a person is an identity slot. Grades are per 5-second SEGMENT (lines carry t0/t1): an identity slot's in-point must sit inside a HERO segment (`onetoone.grades.hero_windows`; castscan does this with `--identity auto`), and a NEVER segment bans just its window. Ungraded footage stays uncastable on identity slots: say so on the card.
- CAPTION SHADOW IS PER STATE: write `"shadow": false` on every state, run the render gate, and remove that key only on the states the gate names as unreadable. Dark ink never wears a dark shadow. Never flip a reference ink colour to get past the gate.
- CAPTION ENTRIES ARE MEASURED: `python3 -m onetoone.measure_devices <brain> <refframes dir with ref-NNN.png names> -o <candidate.json>`, check the scale drifts against raw ink boxes on a few reference frames, fix only frames that are provably not measurements, then save as brain/caption_devices.json.
- BEFORE THE AUDIT, LOOK: build side-by-side sheets (reference frame | our frame) for every caption state's settled frame and read them yourself.
- PREFLIGHT before ANY render: `cd {tools} && python3 -m onetoone.preflight --row <NN> --cast <cast.json>`; obey the exit code. Before reporting FINISHED: `python3 -m onetoone.preflight --row <NN> --finish`.
- DELIVER LOCAL ONLY: deliver/ in the row's work tree + registry review_state + the Deck row. Upload anywhere ONLY after an explicit go on the bus. NEVER post, publish, schedule or upload on your own.
- BUS: every state change goes through `python3 {busline} --receipt <ABSOLUTE receipt path> --lane {lane} --state <state> --text "<plain sentence>"` (never by hand). You may write: `running` when a phase starts and `progress` for milestones. Write lines on the PRIMARY receipt; the runner mirrors the final state to the bundle.
- HOW THIS LANE ENDS (orderd watches the receipt every minute): (1) write the RESULT FILE below; (2) write exactly ONE final busline on the PRIMARY receipt — `--state finished` (delivered after PROVEN, or an APPROVE/no-op handled), `--state called --why "<what the reviewer must decide>"`, or `--state blocked --why "<what stopped you>"`; (3) exit the session with /exit, and if you cannot type it yourself, reply with the single word DONE and stop. The final busline IS the end, so write nothing after it and never write it early.
- Never write personal identifiers or secrets into any file.
- Renders: ffmpeg only via onetoone.ffx; transcription only via tools/whisper_guard.py; one reel, one lane, no parallel build agents.
"""

RESULT_SPEC = """RESULT FILE (write it with the Write tool BEFORE your final busline; the runner reads it when it closes the lane):
{result}
JSON object: {{"outcome": "FINISHED" | "CALLED" | "BLOCKED", "row": <int or null>, "text": "<one plain sentence for the reviewer: what shipped (file + version) or what you need>", "artifact": "<delivered file path relative to the workbench, or null>", "why": "<for BLOCKED/CALLED: what it waits on, plain words>"}}
FINISHED = delivered locally after PROVEN (or an APPROVE/no-op handled). CALLED = the reviewer must decide. BLOCKED = could not proceed (disk floor, missing file, tool failure) — say exactly why. The outcome must match your final busline state."""


def identity_rule(pool: Optional[Path] = None) -> str:
    pool = IDENTITY_POOL if pool is None else pool
    if pool.exists():
        return (f"cast identity slots ONLY from `settled_pool` in {pool} (read it first) "
                "or from clips graded HERO / ME in the Deck grader (onetoone.grades; HERO first). "
                "Every cast slot declares an \"identity\" value accepted by onetoone.identity; no blacklisted stem, no "
                f"in-point inside a banned span; `cd {TOOLS} && python3 -m onetoone.identity "
                "<cast.json>` must print OK before preflight. Where the pool and the LAW's identity lists differ, "
                "the pool file wins.")
    return (f"the identity pool file {pool} does not exist yet; use the identity lists in the LAW "
            "block below, and SHOT_BLACKLIST.json on top.")


def build_prompt(receipt: Path, bundle: list[Path] | None = None, result: Optional[Path] = None,
                 scripts: Optional[Path] = None, registry: Optional[Path] = None, pool: Optional[Path] = None) -> str:
    bundle = [b for b in (bundle or []) if b != receipt]
    text = receipt.read_text(encoding="utf-8", errors="replace")
    kind = order_kind(receipt.name) or "continue"
    row = order_row(receipt.name)
    vm = variants_of(text) if kind == "drop" else None
    if vm is not None:
        kind, row = "variants", vm
    code = registry_shortcode(row, registry) if row is not None else (drop_shortcode(text) if kind == "drop" else None)
    law, source = law_block(row, code, scripts)
    result = result or (receipt.parent / f"{receipt.stem}.orderd-result.json")
    parts = [
        f"You are the orderd lane for ONE Reel Deck order. Work from {REEL}.",
        "",
        f"ORDER: {receipt}  (kind: {kind}; row: {row if row is not None else 'not yet assigned'}; reference: {code or 'unknown'})",
    ]
    if bundle:
        parts.append("SAME-ROUND RECEIPTS (handle together with the order, one round): " + ", ".join(str(b) for b in bundle))
    parts += ["", "TASK: " + KIND_TASK.get(kind, KIND_TASK["continue"]), "",
              "ORDER RECEIPT TEXT (verbatim, as written by the Deck; reviewer input is authoritative, EDITOR receipts are advisory):",
              "<<<", _clip(text), ">>>", ""]
    for b in bundle:
        try:
            parts += [f"BUNDLED RECEIPT {b.name}:", "<<<", _clip(b.read_text(encoding='utf-8', errors='replace'), 6000), ">>>", ""]
        except OSError:
            parts.append(f"(bundled receipt {b} unreadable)")
    parts += [STANDING.format(identity=identity_rule(pool), busline=BUSLINE, lane=LANE_NAME, reel=REEL, tools=TOOLS), "",
              RESULT_SPEC.format(result=result), "",
              f"THE LAW — copied verbatim from {source}; only the template's row number and reference shortcode "
              "were substituted with this order's. Clauses that quote a specific earlier receipt, ruling, fetch "
              "fix, date or 'already registered' state are precedents from the row that template was written for: "
              "keep the doctrine they carry, but the ORDER above is this lane's order and outranks their row-specific facts.",
              law]
    return "\n".join(parts)


def smoke_prompt(receipt: Path, result: Path) -> str:
    """Plumbing proof only: read the order, write the result file and three bus lines, end. Builds nothing."""
    return "\n".join([
        "You are an orderd SMOKE lane, running as an interactive Claude Code session in tmux with nobody at the "
        "keyboard. This is a plumbing test: it proves the launch and the bus loop. Build nothing, render nothing, "
        "touch no registry, no uploads, no network. Do exactly these steps, do not ask anything, and then stop:",
        f"1. Read the order receipt {receipt} and note its first line.",
        f"2. Run: python3 {BUSLINE} --receipt {receipt} --lane {LANE_NAME} --state picked_up --text \"smoke lane read the order\"",
        f"3. Run: python3 {BUSLINE} --receipt {receipt} --lane {LANE_NAME} --state progress --text \"smoke lane is alive and can write the bus\"",
        f"4. Write the file {result} containing exactly: "
        '{"outcome": "FINISHED", "row": null, "text": "smoke test complete, nothing was built", "artifact": null, "why": null}',
        f"5. Run: python3 {BUSLINE} --receipt {receipt} --lane {LANE_NAME} --state finished --text \"smoke test complete, nothing was built\"",
        "6. Exit the session with /exit; if you cannot type it yourself, reply with the single word DONE and stop. "
        "orderd closes the session when it sees the finished line.",
    ])


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("receipt", type=Path)
    ap.add_argument("--bundle", type=Path, nargs="*", default=[])
    ap.add_argument("--result", type=Path)
    ap.add_argument("--smoke", action="store_true")
    a = ap.parse_args()
    if a.smoke:
        print(smoke_prompt(a.receipt, a.result or a.receipt.with_suffix(".orderd-result.json")))
    else:
        print(build_prompt(a.receipt, a.bundle, a.result))
