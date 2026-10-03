"""Functions a task did not ask to change are restored after the write (2026-10-03)."""
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sov.preserve import restore, restore_files  # noqa: E402

ORIG = '''from pkg.stats import mean


def share(issues, links, window, types):
    return mean([1.0]) * 100


def per_story(issues, links, window, types):
    return 2
'''
TASK = ("In `pkg/x.py`: implement `unlinked(tasks, window) -> R` — n = done tasks. "
        "Keep the other two functions exactly as they are. — done when: its tests pass")


def test_a_rewritten_function_is_put_back_and_the_new_one_kept():
    new = ("def share(issues, links):\n    return 0.5\n\n\ndef per_story(issues, links, window, types):\n    return 2\n\n\n"
           "def unlinked(tasks, window):\n    return 3\n")
    out, names = restore(TASK, new, ORIG)
    assert names == ["share"]
    assert "return mean([1.0]) * 100" in out and "def unlinked(tasks, window)" in out and "return 0.5" not in out
    assert out.startswith("from pkg.stats import mean")            # the import it needs came back


def test_a_shadowing_second_copy_is_removed():
    new = ORIG + "\n\ndef unlinked(t, w):\n    return 1\n\n\ndef per_story(a, b):\n    return 99\n"
    out, names = restore(TASK, new, ORIG)
    assert names == ["per_story"] and out.count("def per_story") == 1 and "return 99" not in out


def test_own_functions_private_helpers_and_untouched_files_are_left_alone():
    extend = "In `pkg/x.py`: extend `share(issues, links, window, types)` — add a cap. — done when: x"
    new = ORIG.replace("* 100", "* 50")
    assert restore(extend, new, ORIG) == (new, [])
    priv = ORIG + "\n\ndef _helper():\n    return 1\n"
    assert restore(TASK, priv.replace("return 1", "return 2"), priv)[1] == []
    assert restore(TASK, ORIG, ORIG)[1] == []


def test_restore_files_reads_the_original_from_git(tmp_path):
    run = lambda *a: subprocess.run(["git", *a], cwd=tmp_path, check=True, capture_output=True)  # noqa: E731
    run("init", "-q", "-b", "main"); run("config", "user.email", "t@t"); run("config", "user.name", "t")
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "x.py").write_text(ORIG)
    run("add", "-A"); run("commit", "-qm", "init")
    (tmp_path / "pkg" / "x.py").write_text("def unlinked(t, w):\n    return 1\n")      # dropped both
    (tmp_path / "pkg" / "new.py").write_text("def f():\n    return 1\n")                # not at HEAD: ignored
    assert restore_files(TASK, ["pkg/x.py", "pkg/new.py"], str(tmp_path)) == ["pkg/x.py:share", "pkg/x.py:per_story"]
    src = (tmp_path / "pkg" / "x.py").read_text()
    assert "def unlinked" in src and "def share" in src and "def per_story" in src
