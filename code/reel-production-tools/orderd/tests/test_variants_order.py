"""Deck 'Order the batch' (ui-drop with build_variants_of) reaches the lane as a VARIANTS order on the named row."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import lane_prompt, orderd  # noqa: E402

BODY = """# ui-drop — operator input via Reel Deck — 2025-01-02T06:30:00Z

Sent by: admin (OPERATOR)

ORDER: build a 10-variant alternate batch for registry row 15.

Operator note (verbatim):

(none)
---
Factory: treat exactly like operator chat input (same authority). After acting, append `ACK — <lane> — <utc>` below this line. Append-only.
"""


def test_variants_of_reads_the_row():
    assert lane_prompt.variants_of(BODY) == 15
    assert lane_prompt.variants_of("# ui-drop\nURLs:\n- <instagram-link>\n") is None


def test_receipt_row_comes_from_the_order_line(tmp_path):
    p = tmp_path / "ui-drop-20250102T063000Z.md"; p.write_text(BODY)
    rec = orderd.load_receipt(p)
    assert rec.kind == "drop" and rec.row == 15


def test_prompt_uses_the_variants_brief(tmp_path):
    p = tmp_path / "ui-drop-20250102T063000Z.md"; p.write_text(BODY)
    scripts = tmp_path / "scripts"; scripts.mkdir()
    (scripts / "reel14-build.js").write_text("const LAW = `\nTHE LAW:\n- ROW 14, reference SYNTH0SHORT\n`\n")
    prompt = lane_prompt.build_prompt(p, result=tmp_path / "r.json", scripts=scripts,
                                      registry=tmp_path / "REEL_REGISTRY.json", pool=tmp_path / "pool.json")
    assert "kind: variants; row: 15" in prompt
    assert "VARIANT BATCH" in prompt and "var01..var10" in prompt
    assert "FIRST register the next free sequence row" not in prompt
