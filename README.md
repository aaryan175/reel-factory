# Reference-Driven Reel Factory

A local, file-driven production system that takes a **reference short-form video** (a vertical reel whose *edit* you want to study) and rebuilds that edit with **your own footage**: the same cut rhythm, beat sync, caption typesetting, colour family and edit devices, rendered from your clips, checked by machine gates and by an independent audit, and handed to a human for review.

It is designed to be run by an AI coding agent (Claude Code) as the operator, with a human supplying references and verdicts. Every step leaves a receipt on disk, every human comment becomes a numbered lesson, and lessons are enforced by tests so the same mistake fails a gate the next time.

> **Status:** extracted from a working private system; expect to adapt paths. The code was written on macOS for a single user and a single machine. Paths, fonts, the camera LUT and a few platform-specific tools (face detection, font identification) must be changed before it runs anywhere else. See [docs/01_ARCHITECTURE.md](docs/01_ARCHITECTURE.md) (stand-up section) and [docs/05_CODE_GUIDE.md](docs/05_CODE_GUIDE.md) (porting checklist).

---

## What it does

- **Reference forensics.** Measures a reference frame by frame: every cut, the beat map, each shot's role, every caption state (words, in/out frame, box, font, ink, shadow, entry device), the colour family and the edit devices (flashes, glitches, match cuts, splits).
- **Casting from your library.** Picks clips from an indexed library of your own footage by role, action, world, energy and crop safety, honouring an identity pool (which clips are allowed to show a person), blacklists and freshness rules.
- **Deterministic rendering.** Grades every shot through one fixed "house" colour pipe (camera log → vendor LUT → bounded per-shot correction), typesets captions with real fonts rasterised by Pillow/fontTools, builds devices out of footage (never template overlays), and renders through a single serialised ffmpeg wrapper.
- **Four separate gates.** `technical PASS ≠ agent visual PASS ≠ human approval ≠ publish approval`. A passing test never sets creative approval.
- **Independent audit.** A second agent reads the delivered bytes and lists every measurable defect; the builder fixes every finding and proves each fix with a machine check.
- **Learning loop.** Feedback → inbox → lesson → test → preflight refuses to render until the lesson is triaged. 118 lessons ship with the repo in [docs/04_LESSONS.md](docs/04_LESSONS.md).
- **Review surface.** A small FastAPI web app (the "Deck") where a human watches candidates next to the reference, approves, rejects with a reason, or pins a note to an exact frame.

**The factory does not post.** Nothing is ever posted, uploaded or published by the machine; there is no posting or scheduling automation in this repository. Outward actions stay with the human, done by hand, one explicit approval per item.

## The pipeline at a glance

```
reference URL/file
   │
   ▼
INTAKE ── lock the exact reference (hash, frame count, fps, audio) ── forensics:
   │      cut grid · beat map · shot roles · caption states · fonts · ink · colour · devices
   ▼
CAST ──── query the footage library by role/world/action/energy ── identity pool + blacklist
   │      ── distinct sources, masters not proxies, action peak on the beat
   ▼
PREFLIGHT ── tests green · lessons inbox triaged · disk floors · cast identity law  (refuses otherwise)
   ▼
BUILD ─── house grade → framing/crop → captions typeset → devices → one ffmpeg → measure
   ▼
INDEPENDENT AUDIT ── separate agent, measures delivered bytes at cited frames → findings
   ▼
FIX EVERY FINDING ── each fix proven by a machine check on the new bytes
   ▼
DELIVER (local only) ── file + approval card ── registry row updated ── bus line "finished"
   ▼
HUMAN REVIEW (Deck) ── approve / reject with reason / frame note ── each becomes a new order
   ▼
(only on explicit, per-item human approval) upload or publish — outside the factory
```

The longer stage machine used by the legacy signed runtime (`reelctl`) is:

`REFERENCE_LOCKED → BLUEPRINT_LOCKED → FOOTAGE_INDEXED → FEASIBILITY_REPORTED → SELECTION_LOCKED → ASSETS_LOCKED → RENDERED → TECHNICAL_QC → STRUCTURE_QC → VISUAL_QC → LOCAL_REVIEW_READY → HUMAN_APPROVED → DELIVERY_APPROVED → DELIVERED`

## Two production modes

| Mode | What is preserved | When |
|---|---|---|
| **grammar-adapt** (default) | The reference's grammar: rhythm, role progression, energy arc, caption system, colour family. Footage, and where needed caption words, are yours. | Most references. |
| **1:1 (reference-locked)** | Exact picture-state timing, frame count, caption states and lifecycle, devices and audio structure; any literal scene you cannot supply is declared, never faked. | Style studies where the footage pool can genuinely perform every role. |

Never switch modes silently. If the mode is ambiguous, the operator agent raises one short question.

## Quick start

These steps assume macOS or Linux, Python 3.9+, ffmpeg with `lut3d`, and Claude Code installed. Replace the placeholders throughout:

| Placeholder | Meaning |
|---|---|
| `<REPO>` | where you cloned this repo |
| `<WORKDRIVE>` | a large work volume (plan ~1 TB; keep ≥100 GB free) |
| `<FOOTAGE>` | your authorized footage library (masters + proxies), e.g. `<WORKDRIVE>/footage-library` |
| `<REFS>` | downloaded references (study material only, never a footage source) |
| `<RENDERS>` | per-reel work trees and deliveries, e.g. `<WORKDRIVE>/workbench` |
| `<DECK_PORT>` | the local port the review Deck listens on (bind to 127.0.0.1) |

```bash
# 1. Toolchain
brew install ffmpeg tmux yt-dlp          # or your distro's packages; ffmpeg must have lut3d
python3 -m venv <REPO>/.venv && . <REPO>/.venv/bin/activate
pip install numpy pillow opencv-python-headless fonttools scipy pytest
pip install -e "<REPO>/code/reelctl[dev]"   # legacy runtime + shared colour data

# 2. Paths: grep and replace every hard-coded path (see docs/05_CODE_GUIDE.md, porting checklist)
grep -rnE "/Users/|/Volumes/|/opt/homebrew|/System/Library|~/reel-production|~/apps" <REPO>/code

# 3. Bring your own camera LUT, fonts and music
#    - Download the Sony LC-709 LUT (S-Log3/S-Gamut3.Cine → LC-709) from Sony yourself (it is not
#      redistributed here) and set REEL_FACTORY_LUT to its path; hash-lock it:
export REEL_FACTORY_LUT=/path/to/your/Sony-LC-709.cube
#    - licensed or open fonts for the caption faces you need (see Rights & licensing)
#    - a licensed music track per reel (renders mux it with --audio; see Rights & licensing)

# 4. Tests (each suite with its own interpreter)
cd <REPO>/code/reel-production-tools && python -m pytest -q onetoone/tests orderd/tests
cd <REPO>/code/caption-learning && python -m pytest -q tests
cd <REPO>/code/reel-deck && python -m pytest -q tests
cd <REPO>/code/reelctl && python -m pytest -q

# 5. Preflight (refuses to render until tests, lessons and disk floors pass)
cd <REPO>/code/reel-production-tools && python -m onetoone.preflight --row 1 --skip-tests

# 6. Review Deck on localhost only
cd <REPO>/code/reel-deck && python -m uvicorn server.main:app --host 127.0.0.1 --port <DECK_PORT>

# 7. Open Claude Code in your production directory and paste the bootstrap prompt
#    from docs/07_WORKING_WITH_AN_AI_OPERATOR.md
```

Optional automation (order runner + question relay as launchd/systemd timers) is covered in [docs/01_ARCHITECTURE.md](docs/01_ARCHITECTURE.md). Keep the factory kill switch **closed** until the tests, preflight and a smoke order pass.

## Repository layout

```
README.md
docs/
  01_ARCHITECTURE.md              components, bus/registry/lanes, stage machine, stand-up, failure modes
  02_CRAFT_PLAYBOOK.md            the editing method: cuts, beats, colour, captions, casting, devices, audio
  03_QA_SYSTEM.md                 four gates, independent audit, preflight, learning loop, checklists
  04_LESSONS.md                   L0001–L0118: rule · why · enforced by
  05_CODE_GUIDE.md                module-by-module, call graph, deps, tests, known bugs, porting
  06_REFERENCE_FORENSICS.md       the 13-step method for breaking down a reference
  07_WORKING_WITH_AN_AI_OPERATOR.md   bootstrap prompt, feedback→lessons, gating, concurrency
code/
  reel-production-tools/
    onetoone/                     the one-to-one kit: preflight, render, grade, captions_typeset,
                                  devices, framing, identity, faceid, measure_devices, ffx, housechain …
    orderd/                       order runner (orderd.py), lane prompt builder, question relay (asker.py)
    busline.py                    the only writer of bus status lines
    lessons.py                    learning loop: harvest / add / triage / brief / check
    whisper_guard.py, vocal_guard.py   one-ASR / one-separation-process guards
    library_find.py, beatmap.py, reuse_map.py, blackout_scan.py, …   footage query and helpers
    ditl/                         side kit for day-in-the-life style edits
    reelctl_resolve_handoff.py, resolve_ready.py   optional DaVinci Resolve handoff
  caption-learning/               caption readback / word-truth / ink audit library + tests
  reel-deck/                      FastAPI review app (server/, web/, tests/, tools/deck-user)
  reelctl/                        legacy signed stage-machine runtime, schemas, colour profiles, tests
config/
  launchd/                        example plists for the Deck, order runner and asker
  control/                        example kill-switch and runner state files
  reel-production.claude-settings.json   example Claude Code permissions for the production dir
```

Media, renders, references, fonts, LUTs and secrets are **not** in the repo (`.gitignore` excludes them). Bring your own.

## Requirements

| Piece | Version known to work | Notes |
|---|---|---|
| OS | macOS 15 | Linux works for everything except Apple Vision face detection (`refpeople`) and the system Helvetica Neue path; swap in another detector and a licensed font. |
| Python (kit + runner) | 3.9+ | numpy, Pillow, opencv-python-headless, fontTools, scipy, pytest. Kit modules use `from __future__ import annotations`. |
| Python (reelctl) | ≥3.9 (developed on 3.13) | `pip install -e code/reelctl[dev]`: fastapi, fonttools, jsonschema, numpy, opencv-python-headless, Pillow, uvicorn, yt-dlp; dev: httpx, pytest, ruff. |
| Python (Deck) | 3.11 | fastapi, starlette, uvicorn, itsdangerous, Jinja2, python-multipart; Playwright for the walk-through tests. |
| ffmpeg / ffprobe | 8.x | Needs `lut3d` (tetrahedral), `colorspace`, libx264, AAC. drawtext/libass are **not** needed: captions are rasterised in Python. |
| Whisper | openai-whisper, `medium` max | Only through `whisper_guard.py`: one ASR process machine-wide, ≥6 GB free RAM. |
| yt-dlp | current | Keep it updated; a stale build can fake a "login wall". Anonymous fetch only, within the platform's terms. |
| tmux | 3.x | Lanes run interactive Claude Code inside tmux. |
| Claude Code | current CLI | Logged in interactively on the box. |
| Storage | internal ≥20 GB free, work volume ≥100 GB free | A footage library of a few hundred clips runs ~150 GB with proxies. |
| Optional | DaVinci Resolve (official scripting API), a messaging bot (asker) | None is required for a render. |

## Rights & licensing

This project is a tool for studying and practising editing craft. Use it responsibly:

- **Remakes are for style study and personal edits.** Rebuilding another creator's edit is a learning exercise. **Get permission before closely recreating another creator's work** for anything you publish, and credit the original where appropriate.
- **Music:** use only audio you have rights to: the platform's own audio library when you post there, or tracks you have licensed. The default audio policy is `licensed_track_required`: renders mux a track you supply (`--audio` / `--audio-track`). Muxing a reference's own audio is an explicit opt-in (`--reference-audio-rights-held`) for when you hold the rights to it.
- **Fonts:** the default for anything you publish is a licensed font or an open-licensed font (e.g. SIL OFL). The repo includes glyph-tracing tooling (letterforms traced from reference pixels) **only as an analysis and matching aid**; traced outlines are not a publishing asset. To publish with a typeface, license the original or choose an open alternative.
- **Downloading references:** prefer a local reference file you are allowed to use. If you download, respect each platform's terms of service and the creator's rights; keep references as private study material, and never treat them as a footage source.
- **Footage and people:** use footage you shot or are licensed to use, and have consent from people who appear in it.
- **Code license:** MIT — see [LICENSE](LICENSE). The license covers this code only; it grants no rights to any reference video, music, font or footage you use with it.

## Status

Extracted from a working private system; expect to adapt paths. The one-to-one kit (`code/reel-production-tools/onetoone/`) is the active render path; `reelctl` is the earlier signed stage machine, kept for its schemas, colour profile data and tests. Personal data, footage, history and credentials were removed. Some tests reference fixtures that are not shipped (media is excluded); they will skip or need your own fixtures.

## Documentation

1. [docs/01_ARCHITECTURE.md](docs/01_ARCHITECTURE.md)
2. [docs/02_CRAFT_PLAYBOOK.md](docs/02_CRAFT_PLAYBOOK.md)
3. [docs/03_QA_SYSTEM.md](docs/03_QA_SYSTEM.md)
4. [docs/04_LESSONS.md](docs/04_LESSONS.md)
5. [docs/05_CODE_GUIDE.md](docs/05_CODE_GUIDE.md)
6. [docs/06_REFERENCE_FORENSICS.md](docs/06_REFERENCE_FORENSICS.md)
7. [docs/07_WORKING_WITH_AN_AI_OPERATOR.md](docs/07_WORKING_WITH_AN_AI_OPERATOR.md)
