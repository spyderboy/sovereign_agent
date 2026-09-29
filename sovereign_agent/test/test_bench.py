"""bench.py: suite building helpers and case isolation."""
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bench  # noqa: E402

LINE_A = ("- [ ] In `pkg/a.py`: implement `a()` — done when: x; task gate: "
          ".venv/bin/python -m pytest -q \"tests/test_a.py::test_a\" passes")
LINE_B = "- [ ] In `pkg/b.py`: implement `b()` — task gate: pytest -q tests/test_b.py passes"
ROADMAP = f"# Roadmap\n\n- [x] In `pkg/z.py`: done already\n{LINE_A}\n{LINE_B}\n"


def test_extract_gate_and_target():
    assert bench.extract_gate(LINE_A) == '.venv/bin/python -m pytest -q "tests/test_a.py::test_a"'
    assert bench.target_of(bench.task_text(LINE_A)) == "pkg/a.py"


def test_find_task_line_by_commit_prefix():
    prefix = bench.task_text(LINE_A)[:72]
    assert bench.find_task_line(ROADMAP, prefix) == LINE_A
    assert bench.find_task_line(ROADMAP, "In `pkg/") is None      # ambiguous → refuse


def test_only_open_keeps_exactly_one_task_open():
    out = bench.only_open(ROADMAP, LINE_A)
    assert LINE_A in out
    assert "- [ ] In `pkg/b.py`" not in out and "- [x] In `pkg/b.py`" in out
    assert out.count("- [ ] ") == 1


def test_summarize_counts_harness_lies():
    recs = [
        {"passed": True, "marked": True, "gate_ok": True, "attempts": 1, "wall_s": 100},
        {"passed": True, "marked": True, "gate_ok": True, "attempts": 3, "wall_s": 300},
        {"passed": False, "marked": True, "gate_ok": False, "attempts": 2, "wall_s": 50},
        {"passed": False, "marked": False, "gate_ok": False, "attempts": None, "wall_s": 900},
    ]
    s = bench.summarize(recs)
    assert (s["passed"], s["first_attempt"], s["harness_lied"]) == (2, 1, 1)
    assert s["median_wall_s_passed"] == 200.0


def test_tag_of():
    assert bench.tag_of({"history": {"model": "qwen2.5-coder:14b", "attempts": 1}}) == "easy"
    assert bench.tag_of({"history": {"model": "qwen3.8:27b", "attempts": 1}}) == "hard"
    assert bench.tag_of({"history": {"model": "qwen2.5-coder:14b", "attempts": 2}}) == "hard"
    assert bench.tag_of({"history": None}) == "unknown"


def test_prepare_isolates_the_case(tmp_path, monkeypatch):
    src = tmp_path / "src"
    src.mkdir()
    run = lambda *a: subprocess.run(["git", *a], cwd=src, check=True, capture_output=True)  # noqa: E731
    run("init", "-q", "-b", "main")
    run("config", "user.email", "t@t")
    run("config", "user.name", "t")
    (src / "ROADMAP.md").write_text(ROADMAP)
    run("add", "-A")
    run("commit", "-qm", "base")
    base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=src, capture_output=True,
                          text=True).stdout.strip()
    (src / ".venv").mkdir()
    monkeypatch.setattr(bench, "WORK", tmp_path / "work")
    case = {"id": "a-case", "base": base, "task_line": LINE_A}
    wd = bench.prepare(case, {"project": str(src), "main_branch": "main", "link": [".venv"]},
                       {"rag_enabled": True})
    import json
    assert json.loads((wd / ".sovereign_config.json").read_text())["rag_enabled"] is True
    text = (wd / "ROADMAP.md").read_text()
    assert text.count("- [ ] ") == 1 and LINE_A in text
    assert (wd / ".venv").is_symlink()
    status = subprocess.run(["git", "status", "--porcelain"], cwd=wd, capture_output=True,
                            text=True).stdout
    assert status.strip() == "?? logs/" or status.strip() == ""   # clean apart from logs
    remotes = subprocess.run(["git", "remote"], cwd=wd, capture_output=True, text=True).stdout
    assert remotes.strip() == ""                                   # can never push back


def test_spread_prefers_different_files():
    cases = [{"id": f"t{i}", "target": t} for i, t in
             enumerate(["a.py", "a.py", "a.py", "b.py", "c.py", "c.py"])]
    assert [c["id"] for c in bench.spread(cases, 3)] == ["t0", "t3", "t4"]
    assert [c["id"] for c in bench.spread(cases, 5)] == ["t0", "t3", "t4", "t1", "t5"]


def _recs(spec):
    return [{"case": c, "passed": p, "wall_s": w, "attempts": a} for c, p, w, a in spec]


def test_pick_winner_keeps_baseline_on_noise():
    base = _recs([("a", True, 800, 1), ("b", True, 700, 1), ("c", True, 900, 1)])
    arm = _recs([("a", True, 720, 1), ("b", True, 650, 1), ("c", True, 860, 1)])      # ~10% faster
    assert bench.pick_winner([("base", {}, base), ("arm", {"X": "1"}, arm)])[0] == "base"


def test_pick_winner_ignores_retry_only_setting_that_never_engaged():
    base = _recs([("a", True, 800, 1), ("b", True, 800, 1), ("c", True, 800, 1)])
    arm = _recs([("a", True, 400, 1), ("b", True, 400, 1), ("c", True, 400, 1)])      # 50% faster, no retries
    assert bench.pick_winner([("base", {}, base), ("arm", {"OLLAMA_THINK_RETRIES": "0"}, arm)])[0] == "base"


def test_pick_winner_takes_more_passes_or_a_clear_consistent_speedup():
    base = _recs([("a", True, 800, 1), ("b", False, 900, 3), ("c", True, 800, 1)])
    more = _recs([("a", True, 900, 1), ("b", True, 950, 2), ("c", True, 850, 1)])
    assert bench.pick_winner([("base", {}, base), ("more", {"X": "1"}, more)])[0] == "more"
    fast = _recs([("a", True, 500, 1), ("b", False, 900, 3), ("c", True, 560, 1)])     # ~34% faster on both
    label, _, why = bench.pick_winner([("base", {}, base), ("fast", {"X": "1"}, fast)])
    assert label == "fast" and "faster" in why
