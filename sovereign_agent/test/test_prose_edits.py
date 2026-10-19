"""check_task_size.prose_edits: an open task on an existing file must give REPLACE `old` WITH `new` pairs."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from check_task_size import prose_edits  # noqa: E402


def test_prose_edit_of_an_existing_file_is_flagged_and_pairs_or_new_files_are_not(tmp_path):
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib/screen.dart").write_text("class Screen {}\n")
    roadmap = "\n".join([
        "- [ ] Make Screen read repositories in lib/screen.dart — change the imports and the loop — task gate: x passes",
        "- [ ] Make Screen tidy in lib/screen.dart — REPLACE `class Screen {}` WITH `class Screen { }` — task gate: x passes",
        "- [ ] Create Other in lib/other.dart — `class Other {}` — task gate: x passes",
        "- [x] Old done task in lib/screen.dart — change it",
    ])
    found = prose_edits(roadmap, str(tmp_path))
    assert [f.target for f in found] == ["lib/screen.dart"]
    assert len(found) == 1
