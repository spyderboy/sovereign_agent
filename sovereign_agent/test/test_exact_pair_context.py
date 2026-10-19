"""runner._exact_pair_target: a task given as exact REPLACE/WITH pairs on an existing file gets only that file."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sov.runner import _exact_pair_target  # noqa: E402


def test_pairs_on_an_existing_file(tmp_path):
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib/home.dart").write_text("x")
    task = "Add a tab in lib/home.dart — REPLACE `a` WITH `b` — task gate: t passes"
    assert _exact_pair_target(task, str(tmp_path)) == "lib/home.dart"


def test_prose_or_new_files_keep_the_full_context(tmp_path):
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib/home.dart").write_text("x")
    assert _exact_pair_target("Add a tab in lib/home.dart — change the list", str(tmp_path)) is None
    assert _exact_pair_target("Create X in lib/new.dart — REPLACE `a` WITH `b`", str(tmp_path)) is None
