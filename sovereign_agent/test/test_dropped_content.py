"""Rewrites of existing files must keep their content and never elide it (2026-10-03)."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sov.files import _dropped_content  # noqa: E402

FIX = Path(__file__).parent / "fixtures"
BEFORE = (FIX / "report_template_before.html.j2").read_text()
AFTER = (FIX / "report_template_after.html.j2").read_text()
# what qwen2.5-coder:14b actually returned for the two-line include task
STUB = ('<!-- Insert the v2 partials in the main template -->\n\n{% include "scorecard.html.j2" %}\n\n'
        '<h2>Summary</h2>\n\n{% include "v2_groups.html.j2" %}\n{% include "voices.html.j2" %}\n\n'
        '{% if first_read %}\n  <!-- Existing content -->\n{% endif %}')


def test_the_real_stub_is_rejected():
    assert _dropped_content(BEFORE, STUB, "x/report.html.j2")


def test_the_real_two_line_insertion_passes():
    assert _dropped_content(BEFORE, AFTER, "x/report.html.j2") is None


def test_a_placeholder_is_rejected_even_when_the_length_is_kept():
    py = "".join(f"def f{i}():\n    return {i}\n\n" for i in range(8))
    elided = py.replace("def f3():\n    return 3\n", "# ... rest of the file ...\n")
    assert "placeholder" in _dropped_content(py, elided)
    assert _dropped_content(py, py.replace("return 3", "return 33")) is None


def test_ordinary_comments_and_small_files_are_not_flagged():
    py = "".join(f"x{i} = {i}\n" for i in range(12))
    assert _dropped_content(py, py + "y = f()  # delegate to other functions\n") is None
    assert _dropped_content("a\nb\n", "c\n") is None          # tiny files: too small to judge retention


def test_retention_applies_to_markup_not_code():
    md = "".join(f"line {i}\n" for i in range(20))
    assert "keeps only" in _dropped_content(md, "new\n" * 20, "docs/a.md")
    dart = "".join(f"final x{i} = {i};\n" for i in range(20))
    assert _dropped_content(dart, "void main() => run();\n", "lib/main.dart") is None   # a legit scaffold replacement
