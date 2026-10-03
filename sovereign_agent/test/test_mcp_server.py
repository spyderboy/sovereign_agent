"""mcp_server.py: tools are registered, annotated, resolve projects, keep stdout clean (2026-10-03)."""
import asyncio
import io
import json
import os
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
pytest.importorskip("mcp")
import mcp_server  # noqa: E402
from sov import runctl  # noqa: E402


@pytest.fixture
def proj(tmp_path, monkeypatch):
    root = tmp_path / "demo"
    root.mkdir()
    run = lambda *a: subprocess.run(["git", *a], cwd=root, check=True, capture_output=True)  # noqa: E731
    run("init", "-q", "-b", "main"); run("config", "user.email", "t@t"); run("config", "user.name", "t")
    (root / "ROADMAP.md").write_text("- [x] In `a.py`: a\n- [ ] In `b.py`: b\n")
    (root / ".gitignore").write_text("logs/\n")
    run("add", "-A"); run("commit", "-qm", "init")
    runs = tmp_path / "runs"
    runs.mkdir()
    (runs / "demo.json").write_text(json.dumps({"project": str(root)}))
    monkeypatch.setattr(runctl, "RUNS", runs)
    monkeypatch.setattr(runctl, "PROFILES", tmp_path / "profiles")
    (tmp_path / "profiles").mkdir()
    return root


def test_tools_are_registered_with_the_right_annotations():
    tools = {t.name: t for t in asyncio.run(mcp_server.server.list_tools())}
    assert set(tools) == {"list_projects", "show_plan", "run_status", "recent_activity", "preflight",
                          "launch_run", "stop_run"}
    for name in ("list_projects", "show_plan", "run_status", "recent_activity"):
        assert tools[name].annotations.read_only_hint is True
    for name in ("launch_run", "stop_run"):
        assert tools[name].annotations.destructive_hint is True


def test_projects_resolve_by_name_or_path(proj):
    assert mcp_server.resolve("demo") == os.path.realpath(proj)
    assert mcp_server.resolve(str(proj)) == os.path.realpath(proj)
    with pytest.raises(ValueError):
        mcp_server.resolve("nope")


def test_read_tools_report_the_project(proj):
    [p] = mcp_server.list_projects()
    assert (p["name"], p["done"], p["total"], p["running"]) == ("demo", 1, 2, False)
    assert mcp_server.run_status("demo")["done"] == 1
    assert mcp_server.show_plan("demo")["problems"] == []
    assert mcp_server.recent_activity("demo") == "(no monitor log yet)"
    (proj / "logs").mkdir()
    (proj / "logs" / "monitor.log").write_text("\x1b[2m10:00 ▶ w0 a.py\x1b[0m\n\n10:05 ✓ w0 a.py\n")
    assert mcp_server.recent_activity("demo", lines=1) == "10:05 ✓ w0 a.py"


def test_launch_refusal_is_returned_not_raised_and_stdout_stays_clean(proj, monkeypatch):
    (proj / "b.py").write_text("x = 1\n")                     # untracked file: launch must refuse
    monkeypatch.setattr(runctl, "is_running", lambda root: False)
    buf = io.StringIO()
    with redirect_stdout(buf):
        r = mcp_server.launch_run("demo")
    assert r["ok"] is False and "uncommitted" in r["error"] and buf.getvalue() == ""
