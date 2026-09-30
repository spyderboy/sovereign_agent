"""bench.py --engine aider: same case and gate, Aider edits; rewriting the
test to pass is not a pass. Uses a stand-in `aider` on PATH (no model)."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bench  # noqa: E402

TASK = ("- [ ] In `pkg/a.py`: implement `f() -> int` returning 1 — task gate: "
        "python3 -m pytest -q \"tests/test_a.py::test_f\" passes")


@pytest.fixture
def suite(tmp_path, monkeypatch):
    proj = tmp_path / "proj"
    (proj / "pkg").mkdir(parents=True)
    (proj / "tests").mkdir()
    (proj / "pkg" / "a.py").write_text("def f():\n    return 0\n")
    (proj / "tests" / "test_a.py").write_text(
        "import sys, os\nsys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))\n"
        "from pkg.a import f\n\ndef test_f():\n    assert f() == 1\n")
    (proj / "ROADMAP.md").write_text(TASK + "\n- [ ] In `pkg/b.py`: later task\n")
    (proj / ".sovereign_config.json").write_text(json.dumps({"locked_files": ["pkg/model.py"]}))
    for args in (["init", "-q", "-b", "main"], ["add", "."],
                 ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "base"]):
        subprocess.run(["git", *args], cwd=proj, check=True)
    base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=proj, capture_output=True, text=True).stdout.strip()
    (tmp_path / "profiles").mkdir()
    monkeypatch.setattr(bench, "HERE", tmp_path)
    monkeypatch.setattr(bench, "WORK", tmp_path / "work")
    (tmp_path / "profiles" / "p.toml").write_text('[tiers.1]\nmodel = "m:14b"\n')
    case = {"id": "c1", "base": base, "task_line": TASK, "target": "pkg/a.py",
            "gate": f'{sys.executable} -m pytest -q -p no:cacheprovider "tests/test_a.py::test_f"'}
    return {"project": str(proj), "main_branch": "main", "link": []}, case


def fake_aider(tmp_path, monkeypatch, script):
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    (bindir / "aider").write_text("#!/bin/sh\n" + script)
    (bindir / "aider").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")


def test_honest_fix_passes(suite, tmp_path, monkeypatch):
    s, case = suite
    fake_aider(tmp_path, monkeypatch, "printf 'def f():\\n    return 1\\n' > pkg/a.py\n")
    rec = bench.run_case_aider(case, s, dict(os.environ), 60, tmp_path, profile="p")
    assert rec["passed"] and rec["gate_ok"] and rec["edited_protected"] == []
    assert rec["model"] == "m:14b" and rec["files_changed"] == ["pkg/a.py"]


def test_rewriting_the_test_is_not_a_pass(suite, tmp_path, monkeypatch):
    s, case = suite
    fake_aider(tmp_path, monkeypatch, "printf 'def test_f():\\n    pass\\n' > tests/test_a.py\n")
    rec = bench.run_case_aider(case, s, dict(os.environ), 60, tmp_path, profile="p")
    assert rec["gate_ok"] and not rec["passed"]
    assert rec["edited_protected"] == ["tests/test_a.py"]


def test_command_carries_task_gate_and_files(suite):
    _, case = suite
    cmd = bench.aider_cmd(case, "m:14b", ["pkg/a.py", "tests/test_a.py"], 60)
    assert cmd[cmd.index("--model") + 1] == "ollama_chat/m:14b"
    assert cmd[cmd.index("--test-cmd") + 1] == case["gate"]
    assert "--auto-test" in cmd and "--no-auto-commits" in cmd and cmd[-2:] == ["pkg/a.py", "tests/test_a.py"]
    assert bench.gate_files(case["gate"]) == ["tests/test_a.py"]
