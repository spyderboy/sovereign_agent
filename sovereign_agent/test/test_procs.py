"""sov/procs.py: only real interpreter-runs-script processes count (never shells/editors)."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sov.procs import mentions, script_of  # noqa: E402

ROOT = "/Volumes/Lexar/Code/xmeasure"


def test_real_run_processes():
    assert script_of(f"bash ./sweep.sh {ROOT}")[0] == "sweep.sh"
    assert script_of(f"/bin/bash ./supervisor.sh {ROOT} --workers 2")[0] == "supervisor.sh"
    s, rest = script_of(f"/opt/homebrew/Frameworks/Python.app/Contents/MacOS/Python -u "
                        f"/x/sovereign_agent/work.py --project {ROOT}")
    assert s == "work.py" and mentions(rest, os.path.realpath(ROOT)) is (os.path.realpath(ROOT) == ROOT)
    assert script_of("python3 -W ignore monitor.py /p")[0] == "monitor.py"
    assert script_of("python -u xrun.py guard /p") == ("xrun.py", ["guard", "/p"])


def test_the_shell_that_killed_itself_does_not_match():
    # 2026-09-29: runctl.stop killed the very shell running this kind of command
    cmd = (f"/bin/zsh -c for p in 'bench/overnight.py' 'monitor.py' 'sweep.sh'; do pgrep -f $p; "
           f"done; cd {ROOT} && git status")
    assert script_of(cmd) == (None, [])
    assert script_of(f"bash -c 'python work.py --project {ROOT}'") == (None, [])


def test_editors_viewers_and_modules_do_not_match():
    assert script_of(f"vim {ROOT}/sovereign_agent/monitor.py")[0] is None
    assert script_of(f"tail -f {ROOT}/logs/work.py")[0] is None
    assert script_of("python -m pytest test/") == (None, [])
    assert script_of("") == (None, [])
