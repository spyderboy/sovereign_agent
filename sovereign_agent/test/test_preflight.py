"""preflight.py static checks and dry-run failure classification (2026-09-30)."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import preflight  # noqa: E402


def _proj(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "pkg" / "constants.py").write_text("LIMIT = 3\n")
    (tmp_path / "pkg" / "errors.py").write_text("class PdfUnavailable(Exception): ...\n")
    (tmp_path / "API.md").write_text("```python\n# pkg/stats.py\ndef median(v): ...\n```\n")
    (tmp_path / ".sovereign_config.json").write_text(json.dumps(
        {"context_always_include": ["API.md", "pkg/constants.py"]}))
    (tmp_path / "tests" / "test_pdf.py").write_text(
        "from pkg.errors import PdfUnavailable\nfrom pkg.constants import LIMIT\n"
        "from pkg.stats import median\nfrom pkg.pdf import render\n")
    return str(tmp_path)


def test_coverage_flags_only_names_the_worker_cannot_locate(tmp_path):
    root = _proj(tmp_path)
    line = "- [ ] In `pkg/pdf.py`: implement `render()` — task gate: pytest -q tests/test_pdf.py passes"
    gaps = preflight.coverage_gaps(line, root, preflight.context_text(root))
    assert len(gaps) == 1 and "`PdfUnavailable` from `pkg.errors`" in gaps[0]
    named = line.replace("implement `render()`", "implement `render()`, raise PdfUnavailable from pkg.errors")
    assert preflight.coverage_gaps(named, root, preflight.context_text(root)) == []


def test_order_flags_a_dependency_built_by_a_later_task(tmp_path):
    root = _proj(tmp_path)
    tasks = ["- [ ] In `pkg/b.py`: implement `b()` — calls `helper(x)` — task gate: true passes",
             "- [ ] In `pkg/a.py`: implement `helper(x) -> int` — task gate: true passes"]
    (tmp_path / "API.md").write_text("```python\n# pkg/a.py\ndef helper(x): ...\n```\n")
    probs = preflight.order_problems(tasks, root)
    assert any("pkg/b.py needs pkg/a.py" in p and "later task" in p for p in probs)


def test_classify_names_the_likely_cause():
    assert preflight.classify("x\n timeout ... (12 chars produced) — the model is streaming")[0] == "over-scoped"
    assert preflight.classify("E   ModuleNotFoundError: No module named 'pkg.build'")[0] == "missing dependency"
    assert preflight.classify("E   ImportError: cannot import name 'Config' from 'pkg.model'")[0] == "wrong import"
    assert preflight.classify("AttributeError: 'RepoConfig' object has no attribute 'nam'")[0] == "contract gap"
    assert preflight.classify("(blocked bad pattern: x)")[0] == "guard conflict"
    assert preflight.classify("assert 1 == 2")[0] == "model"


def test_dry_run_keeps_passes_and_reopens_the_rest(tmp_path, monkeypatch):
    import subprocess
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "ROADMAP.md").write_text("- [x] In `a.py`: done — task gate: true passes\n"
                                     "- [ ] In `b.py`: first — task gate: true passes\n"
                                     "- [ ] In `c.py`: second — task gate: true passes\n")
    for args in (["init", "-q", "-b", "main"], ["add", "."],
                 ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"]):
        subprocess.run(["git", *args], cwd=proj, check=True, capture_output=True)
    monkeypatch.setattr(preflight, "WORK", tmp_path / "work")
    real_run = subprocess.run
    subprocess.run(["git", "checkout", "-q", "--detach"], cwd=proj, check=True)   # like a snapshot

    def fake_run(cmd, **kw):
        if "work.py" in " ".join(map(str, cmd)):
            wdir = tmp_path / "work" / "proj"
            assert kw["env"]["MAIN_BRANCH"] == "preflight"
            assert real_run(["git", "branch", "--show-current"], cwd=wdir, capture_output=True,
                            text=True).stdout.strip() == "preflight"         # never stranded
            real_run(["git", "checkout", "-q", "-b", "task-9-x"], cwd=wdir, check=True)  # a worker leaves this
            rm = (wdir / "ROADMAP.md")
            text = rm.read_text()
            if "- [ ] In `b.py`" in text:                      # b passes, c fails
                rm.write_text(text.replace("- [ ] In `b.py`", "- [x] In `b.py`"))
            else:
                kw["stdout"].write("E   ModuleNotFoundError: No module named 'pkg.build'\n")
            return subprocess.CompletedProcess(cmd, 0)
        return real_run(cmd, **kw)

    monkeypatch.setattr(preflight.subprocess, "run", fake_run)
    runs = preflight.dry_run(str(proj), "p", 60, log=lambda m: None)
    assert [(r["target"], r["passed"], r["cause"]) for r in runs] == [
        ("b.py", True, ""), ("c.py", False, "missing dependency")]
    final = (tmp_path / "work" / "proj" / "ROADMAP.md").read_text()
    assert "- [x] In `b.py`" in final and "- [ ] In `c.py`" in final and "- [x] In `a.py`" in final
    assert (proj / "ROADMAP.md").read_text().count("- [ ]") == 2   # the real project is untouched
