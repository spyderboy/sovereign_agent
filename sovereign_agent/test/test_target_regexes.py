"""Every 'In <target>:' pattern in the harness accepts the backticked form (2026-10-03).

Ten copies matched only `In path:`; tasks written as ``In `path`:`` never had
their target detected, so SEARCH/REPLACE mode and the target-in-context rule
silently never applied to them."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PAT = re.compile(r"""r(['"])(\\bIn [^'"]*?:)\1""")


def test_target_patterns_accept_backticks():
    found = 0
    for f in list((ROOT / "sov").glob("*.py")) + [ROOT / "work.py"]:
        for m in PAT.finditer(f.read_text()):
            rx = re.compile(m.group(2))
            ext = re.search(r"\\\.\(?([a-z]+)", m.group(2))
            name = "a/b." + (ext.group(1) if ext and ext.group(1) != "w" else "py")
            for task in (f"In {name}: implement f", f"In `{name}`: implement f"):
                if "implement" in m.group(2):
                    task = task.replace("implement f", "implement `f(x)`")
                assert rx.search(task), f"{f.name}: {m.group(2)!r} misses {task!r}"
            found += 1
    assert found >= 10
