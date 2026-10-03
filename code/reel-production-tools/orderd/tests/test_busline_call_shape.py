"""A factory CALL is one short question with numbered options. busline refuses
an essay or a CALL without '1 = ' / '2 = ' options; a short one with options passes."""
import importlib.util, sys
from pathlib import Path

spec = importlib.util.spec_from_file_location("busline", Path(__file__).resolve().parents[2] / "busline.py")
bl = importlib.util.module_from_spec(spec); spec.loader.exec_module(bl)


def test_short_call_with_options_passes():
    assert bl.call_shape_problem("S4 hides the caption. Reframe once more? 1 = reframe S4 (recommended), 2 = accept with a disclosure") is None
    assert bl.call_shape_problem("Fonts? RULING 1 = buy, RULING 2 = substitute") is None


def test_essay_or_optionless_call_is_refused():
    essay = "x" * 400 + " 1 = a, 2 = b"
    assert "at most" in bl.call_shape_problem(essay)
    assert "options" in bl.call_shape_problem("Row 14 reference needs a decision and the build agent stopped.")
    assert "options" in bl.call_shape_problem("pick: 1 = only one option given")


def test_append_refuses_a_bad_call(tmp_path):
    r = tmp_path / "ui-feedback-row14-x.md"; r.write_text("# x\n")
    import pytest
    with pytest.raises(SystemExit):
        bl.append(r, "lane", "called", "a long question with no options at all " * 3)
    line = bl.append(r, "lane", "called", "Reframe S4? 1 = yes, 2 = accept as is", why="audit HARD")
    assert line.startswith("CALL — lane — ") and "1 = yes" in line
