"""An existing file a task edits is changed only through SEARCH/REPLACE (2026-10-19, EstateWise): the
model rewrote whole screens from memory and invented fields. _file_on_main tells an edit from a create;
write_changes refuses a whole-file rewrite of a file in state.REQUIRE_PATCH."""
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sov import files, prompting, state  # noqa: E402

SCREEN = "lib/screen.dart"
ORIGINAL = "import 'a.dart';\n\nclass Screen {\n  final x = mockThing;\n}\n"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    def git(*args):
        subprocess.run(["git", "-C", str(tmp_path), *args], check=True, capture_output=True)
    git("init", "-q", "-b", "main")
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    (tmp_path / "lib").mkdir()
    (tmp_path / SCREEN).write_text(ORIGINAL)
    (tmp_path / "lib/a.dart").write_text("final mockThing = 1;\nfinal thingRepository = null;\n")
    git("add", "-A")
    git("commit", "-qm", "init")
    monkeypatch.setattr(prompting, "MAIN_BRANCH", "main")
    monkeypatch.setattr(state, "REQUIRE_PATCH", set())
    monkeypatch.setattr(files, "check_bad_patterns", lambda *a, **k: [])
    return tmp_path


def test_an_existing_file_is_on_main_and_a_new_one_is_not(repo):
    assert prompting._file_on_main(str(repo), SCREEN)
    (repo / "lib/new.dart").write_text("x")      # a create task's own attempt, not on main
    assert not prompting._file_on_main(str(repo), "lib/new.dart")
    assert not prompting._file_on_main(str(repo), "lib/missing.dart")


def test_a_whole_file_rewrite_of_a_required_file_is_refused(repo, monkeypatch):
    monkeypatch.setattr(state, "REQUIRE_PATCH", {SCREEN})
    written, err = files.write_changes({SCREEN: "class Screen { final x = invented.cost; }\n"}, str(repo))
    assert written == []
    assert "FULL REWRITE REFUSED" in err
    assert (repo / SCREEN).read_text() == ORIGINAL


def test_search_replace_blocks_on_a_required_file_are_applied(repo, monkeypatch):
    monkeypatch.setattr(state, "REQUIRE_PATCH", {SCREEN})
    patch = "<<<<<<< SEARCH\n  final x = mockThing;\n=======\n  final x = thingRepository.all();\n>>>>>>> REPLACE\n"
    written, err = files.write_changes({SCREEN: patch}, str(repo))
    assert not err, err
    assert written == [SCREEN]
    assert "thingRepository.all()" in (repo / SCREEN).read_text()
    assert "import 'a.dart';" in (repo / SCREEN).read_text()


def test_files_not_required_still_take_full_content(repo):
    written, err = files.write_changes({"lib/new.dart": "class New {}\n"}, str(repo))
    assert not err, err
    assert written == ["lib/new.dart"]
