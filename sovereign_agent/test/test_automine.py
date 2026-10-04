"""automine (2026-10-04): between sweep passes Xanadu applies the guards it mined from its own
failures — without a human running `api_surface.py --mine --apply` — so 'self-healing' needs
no asterisk. Safe by construction: a guard that matches committed code, doesn't compile, or
already exists is dropped; the config change is committed so forced checkouts keep it."""
import json
import subprocess


def _repo(tmp_path, cfg_extra=None):
    cfg = {"language": "python", "additional_bad_patterns": [{"pattern": r"\bold_guard\b", "hint": "h"}]}
    cfg.update(cfg_extra or {})
    (tmp_path / ".sovereign_config.json").write_text(json.dumps(cfg, indent=2))
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "mod.py").write_text("def real():\n    return Config(name='x').name\n")
    (tmp_path / "logs").mkdir()
    for cmd in (["init", "-q", "-b", "main"], ["add", "-A"], ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"]):
        subprocess.run(["git", "-C", str(tmp_path)] + cmd, check=True, capture_output=True)
    return tmp_path


def _git(p, *args):
    return subprocess.run(["git", "-C", str(p)] + list(args), capture_output=True, text=True).stdout


def test_applies_safe_mined_guards_and_commits(tmp_path):
    import automine
    p = _repo(tmp_path)
    mined = [{"pattern": r"\.summary\b", "hint": "Issue has no attribute 'summary'"},          # safe
             {"pattern": r"\.name\b", "hint": "would block real code"},                          # matches pkg/mod.py
             {"pattern": r"(unclosed", "hint": "bad regex"},                                     # does not compile
             {"pattern": r"\bold_guard\b", "hint": "already there"}]                             # duplicate
    added = automine.auto_mine(str(p), mine_fn=lambda project: mined, today="2026-10-04")
    assert [g["pattern"] for g in added] == [r"\.summary\b"]
    cfg = json.loads((p / ".sovereign_config.json").read_text())
    assert cfg["additional_bad_patterns"][-1] == {"pattern": r"\.summary\b", "hint": "Issue has no attribute 'summary'",
                                                  "origin": "mined", "mined_at": "2026-10-04"}
    assert "guards: +1 mined from run logs (automatic)" in _git(p, "log", "-1", "--format=%s")
    assert _git(p, "status", "--porcelain", "--untracked-files=no") == ""          # committed: survives checkout -f
    log = (p / "logs" / "automine.log").read_text()
    assert "kept \\.summary\\b" in log and "dropped \\.name\\b: matches committed code (pkg/mod.py)" in log


def test_nothing_new_changes_nothing(tmp_path):
    import automine
    p = _repo(tmp_path)
    head = _git(p, "rev-parse", "HEAD")
    assert automine.auto_mine(str(p), mine_fn=lambda project: []) == []
    assert _git(p, "rev-parse", "HEAD") == head


def test_cap_per_pass_and_opt_out(tmp_path):
    import automine
    p = _repo(tmp_path)
    many = [{"pattern": rf"\bghost{i}\b", "hint": "h"} for i in range(15)]
    assert len(automine.auto_mine(str(p), mine_fn=lambda project: many)) == automine.MAX_NEW_PER_PASS == 10
    (tmp_path / "q").mkdir()
    q = _repo(tmp_path / "q", {"auto_mine_guards": False})
    assert automine.auto_mine(str(q), mine_fn=lambda project: many) == []


def test_refuses_a_dirty_tree(tmp_path):
    import automine
    p = _repo(tmp_path)
    (p / "pkg" / "mod.py").write_text("# someone's uncommitted edit\n")
    assert automine.auto_mine(str(p), mine_fn=lambda project: [{"pattern": r"\bghost\b", "hint": "h"}]) == []
    assert (p / "pkg" / "mod.py").read_text() == "# someone's uncommitted edit\n"          # never touched


def test_dry_run_writes_nothing(tmp_path):
    import automine
    p = _repo(tmp_path)
    head, cfg = _git(p, "rev-parse", "HEAD"), (p / ".sovereign_config.json").read_text()
    added = automine.auto_mine(str(p), mine_fn=lambda project: [{"pattern": r"\bghost\b", "hint": "h"}], dry_run=True)
    assert [g["pattern"] for g in added] == [r"\bghost\b"]
    assert _git(p, "rev-parse", "HEAD") == head and (p / ".sovereign_config.json").read_text() == cfg
