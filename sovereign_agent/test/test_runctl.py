"""sov/runctl.py: plans, generated profiles, the pass hook, launch refusals."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
from sov import runctl  # noqa: E402


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    root.mkdir()
    run = lambda *a: subprocess.run(["git", *a], cwd=root, check=True, capture_output=True)  # noqa: E731
    run("init", "-q", "-b", "main")
    run("config", "user.email", "t@t")
    run("config", "user.name", "t")
    (root / "ROADMAP.md").write_text("- [x] In `a.py`: a\n- [ ] In `b.py`: b\n")
    (root / ".gitignore").write_text("logs/\n")
    run("add", "-A")
    run("commit", "-qm", "init")
    monkeypatch.setattr(runctl, "RUNS", tmp_path / "runs")
    monkeypatch.setattr(runctl, "PROFILES", tmp_path / "profiles")
    (tmp_path / "profiles").mkdir()
    return root


def test_defaults_and_validation(repo):
    plan = runctl.load_plan(str(repo))
    assert runctl.validate(plan) == []
    plan["strong"]["where"] = "url"
    plan["strong"]["url"] = "pod.example"
    plan["cheap"]["workers"] = 9
    errs = " ".join(runctl.validate(plan))
    assert "server URL" in errs and "workers" in errs


def test_runpod_needs_a_budget(repo):
    plan = runctl.load_plan(str(repo))
    plan["strong"]["where"] = "runpod"
    plan["runpod"]["max_run_usd"] = 0
    assert any("spending cap" in e for e in runctl.validate(plan))


def test_save_and_reload_round_trip(repo):
    plan = runctl.load_plan(str(repo))
    plan["strong"]["model"] = "big:70b"
    plan["runpod"]["max_run_usd"] = 25
    assert runctl.save_plan(plan) == []
    again = runctl.load_plan(str(repo))
    assert again["strong"]["model"] == "big:70b" and again["runpod"]["max_run_usd"] == 25
    assert again["cheap"]["model"] == runctl.PASS_DEFAULTS["cheap"]["model"]


def test_generated_profile_loads_in_the_harness(repo, tmp_path):
    plan = runctl.load_plan(str(repo))
    plan["strong"].update(model="big:70b", params=70, think="first-only", call_timeout_s=1200)
    text = runctl.profile_text(plan, "strong", "https://pod-11434.proxy.runpod.net")
    name = "zz-test-generated"
    path = HERE / "profiles" / f"{name}.toml"
    path.write_text(text)
    try:
        code = ("import sov.config as c; print(c.TIER1_MODEL, c.endpoint_for(c.TIER1_MODEL), "
                "c.CALL_TIMEOUT_S, c.OLLAMA_THINK_RETRIES, c.MAIN_BRANCH)")
        env = {k: v for k, v in os.environ.items() if not k.startswith(("TIER", "CALL_", "OLLAMA_THINK"))}
        env["SOVEREIGN_PROFILE"] = name
        out = subprocess.run([sys.executable, "-W", "ignore", "-c", code], cwd=HERE, env=env,
                             capture_output=True, text=True, check=True).stdout.split("\n")[-2]
        assert out == "big:70b https://pod-11434.proxy.runpod.net 1200 False main"
    finally:
        path.unlink()


def test_hook_starts_this_pass_pod_and_stops_the_idle_one(repo, monkeypatch):
    plan = runctl.load_plan(str(repo))
    plan["strong"]["where"] = "runpod"
    calls = []
    monkeypatch.setattr(runctl, "ensure_pod", lambda p, name, log: calls.append(("ensure", name)) or "u")
    monkeypatch.setattr(runctl, "stop_run_pods", lambda p, only=None: calls.append(("stop", only)) or [])
    assert runctl.hook(plan, "cycle1-strong", lambda m: None) == 0
    assert ("ensure", "strong") in calls
    calls.clear()
    assert runctl.hook(plan, "cycle2-cheap", lambda m: None) == 0
    assert calls == [("stop", "strong")]                     # strong pod idle during cheap pass


def test_shared_pod_serves_both_passes_and_is_never_stopped_between(repo, monkeypatch):
    plan = runctl.load_plan(str(repo))
    plan["cheap"]["where"] = plan["strong"]["where"] = "runpod"
    plan["runpod"]["shared_pod"] = True
    assert runctl.pod_key(plan, "cheap") == runctl.pod_key(plan, "strong") == "shared"
    calls = []
    monkeypatch.setattr(runctl, "ensure_pod", lambda p, name, log: calls.append(("ensure", name)) or "u")
    monkeypatch.setattr(runctl, "stop_run_pods", lambda p, only=None: calls.append(("stop", only)) or [])
    assert runctl.hook(plan, "cycle1-strong", lambda m: None) == 0
    assert calls == [("ensure", "strong")]
    url = runctl.pass_url(plan, "cheap", {"pods": {"shared": "abc"}})
    assert url == runctl.pass_url(plan, "strong", {"pods": {"shared": "abc"}})
    assert url == "https://abc-11434.proxy.runpod.net"
    plan["cheap"]["where"] = "local"                          # one pass local: no sharing
    assert runctl.pod_key(plan, "strong") == "strong"


def test_hook_failure_skips_the_pass(repo, monkeypatch):
    plan = runctl.load_plan(str(repo))
    plan["strong"]["where"] = "runpod"
    monkeypatch.setattr(runctl, "stop_run_pods", lambda p, only=None: [])

    def boom(*a):
        raise RuntimeError("budget reached")
    monkeypatch.setattr(runctl, "ensure_pod", boom)
    assert runctl.hook(plan, "cycle1-strong", lambda m: None) == 1


def test_launch_refuses_uncommitted_changes(repo, monkeypatch):
    (repo / "ROADMAP.md").write_text("human edit\n")
    monkeypatch.setattr(runctl, "_spawn", lambda *a, **k: pytest.fail("launched"))
    r = runctl.launch(runctl.load_plan(str(repo)), lambda m: None)
    assert not r["ok"] and "uncommitted" in r["error"]
    assert (repo / "ROADMAP.md").read_text() == "human edit\n"


def test_status_counts(repo):
    s = runctl.status(runctl.load_plan(str(repo)))
    assert (s["done"], s["total"], s["running"]) == (1, 2, False)


def test_park_discards_worker_debris_on_a_task_branch(repo):
    run = lambda *a: subprocess.run(["git", *a], cwd=repo, check=True, capture_output=True)  # noqa: E731
    run("checkout", "-q", "-b", "task-3-something")
    (repo / "ROADMAP.md").write_text("half-written by a worker\n")
    (repo / "junk.py").write_text("broken(\n")
    assert runctl.park_repo(str(repo), "main") == []
    assert "In `b.py`" in (repo / "ROADMAP.md").read_text()
    assert not (repo / "junk.py").exists()
    assert "task-3" not in subprocess.run(["git", "branch"], cwd=repo, capture_output=True,
                                          text=True).stdout


def test_park_never_discards_an_edit_on_main(repo):
    (repo / "ROADMAP.md").write_text("human edit\n")
    assert runctl.park_repo(str(repo), "main")
    assert (repo / "ROADMAP.md").read_text() == "human edit\n"


def test_launch_refuses_when_a_finished_gate_fails_on_main(repo, monkeypatch):
    run = lambda *a: subprocess.run(["git", *a], cwd=repo, check=True, capture_output=True)  # noqa: E731
    (repo / "ROADMAP.md").write_text("- [x] In `a.py`: a — task gate: true passes\n"
                                     "- [x] In `jq.py`: build_jql — task gate: false passes\n"
                                     "- [ ] In `b.py`: b\n")
    run("commit", "-qam", "roadmap")
    monkeypatch.setattr(runctl, "_spawn", lambda *a, **k: pytest.fail("launched"))
    r = runctl.launch(runctl.load_plan(str(repo)), lambda m: None)
    assert not r["ok"] and "jq.py" in r["error"] and "a.py" not in r["error"]
    assert runctl.broken_done_gates(str(repo)) == (["jq.py"], True)
