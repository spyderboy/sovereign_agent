"""Velocity records carry provenance (2026-10-04): which harness ran the task, what
Xanadu had learned by itself by then, and a fingerprint of what humans had written —
so 'Xanadu improves with use' can be separated from model upgrades and human fixes."""
import json


def _project(tmp_path, guards=(), errors=0, drafts=0, strikes=None, roorules="be careful"):
    (tmp_path / "logs").mkdir(exist_ok=True)
    (tmp_path / ".sovereign_config.json").write_text(json.dumps({"additional_bad_patterns": list(guards)}))
    (tmp_path / "logs" / "errors.jsonl").write_text("".join('{"e": %d}\n' % i for i in range(errors)))
    if drafts:
        (tmp_path / "logs" / "rule_drafts.jsonl").write_text("".join('{"r": %d}\n' % i for i in range(drafts)))
    if strikes is not None:
        (tmp_path / "logs" / "strong_failures.json").write_text(json.dumps(strikes))
    (tmp_path / ".roorules").write_text(roorules)
    return tmp_path


def _last(tmp_path):
    return json.loads((tmp_path / "logs" / "velocity.jsonl").read_text().splitlines()[-1])


def test_record_carries_harness_learned_and_human(tmp_path):
    from sov import telemetry
    p = _project(tmp_path, guards=[{"pattern": "a", "hint": "h"}, {"pattern": "b", "hint": "h", "origin": "mined"},
                                   {"pattern": "c", "hint": "h", "origin": "mined"}],
                 errors=4, drafts=2, strikes={"t1": 2, "t2": 1})
    telemetry._append_velocity(str(p), {"task": "x", "outcome": "done"})
    r = _last(p)
    assert r["task"] == "x" and r["outcome"] == "done"
    assert r["harness"] and len(r["harness"].split("-")[0]) >= 7
    assert r["learned"] == {"guards_mined": 2, "error_patterns": 4, "rule_drafts": 2, "strikes": 2}
    assert r["human"]["guards"] == 1 and len(r["human"]["rules"]) == 8


def test_human_fingerprint_changes_only_when_humans_change_rules(tmp_path):
    from sov import telemetry
    p = _project(tmp_path)
    telemetry._append_velocity(str(p), {"task": "x", "outcome": "done"})
    first = _last(p)["human"]["rules"]
    (p / "logs" / "errors.jsonl").write_text('{"e": 1}\n')                 # Xanadu learning: not human
    telemetry._append_velocity(str(p), {"task": "x", "outcome": "done"})
    assert _last(p)["human"]["rules"] == first
    (p / ".roorules").write_text("be careful. also: never stub")            # a human edit
    telemetry._append_velocity(str(p), {"task": "x", "outcome": "done"})
    assert _last(p)["human"]["rules"] != first


def test_missing_files_are_zero_not_errors(tmp_path):
    from sov import telemetry
    telemetry._append_velocity(str(tmp_path), {"task": "x", "outcome": "skipped"})
    r = _last(tmp_path)
    assert r["learned"] == {"guards_mined": 0, "error_patterns": 0, "rule_drafts": 0, "strikes": 0}
    assert r["human"]["guards"] == 0


def test_mined_guards_are_marked(tmp_path, monkeypatch):
    import api_surface
    monkeypatch.setattr(api_surface, "mine_python", lambda project: [{"pattern": "p", "hint": "h"}])
    monkeypatch.setattr(api_surface, "language", lambda project: "python")
    (tmp_path / ".sovereign_config.json").write_text(json.dumps({"additional_bad_patterns": []}))
    monkeypatch.setattr("sys.argv", ["api_surface.py", str(tmp_path), "--mine", "--apply"])
    api_surface.main()
    cfg = json.loads((tmp_path / ".sovereign_config.json").read_text())
    assert cfg["additional_bad_patterns"] == [{"pattern": "p", "hint": "h", "origin": "mined"}]
