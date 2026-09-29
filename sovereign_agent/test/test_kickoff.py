"""api_surface.py Python support and kickoff.py's transfer rules."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import api_surface  # noqa: E402
import kickoff      # noqa: E402

MODEL = '''
from dataclasses import dataclass
from enum import Enum

class Color(Enum):
    RED = "red"

@dataclass(frozen=True)
class Issue:
    key: str
    labels: tuple
    def is_open(self): return True
'''
SCHEMA = '''DDL = """
CREATE TABLE IF NOT EXISTS issues (
    key TEXT NOT NULL,
    labels TEXT,
    PRIMARY KEY (key, labels)
);
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
"""
'''
LOG = """
E   AttributeError: 'Issue' object has no attribute 'summary'
E   AttributeError: 'dict' object has no attribute 'nope'
E   ImportError: cannot import name 'get_conn' from 'pkg.store' (/x/pkg/store/__init__.py)
E   TypeError: Issue.__init__() got an unexpected keyword argument 'repo'
E   sqlite3.OperationalError: table issues has no column named subject
E   AttributeError: 'Issue' object has no attribute 'labels'
"""


@pytest.fixture
def proj(tmp_path):
    (tmp_path / "pkg" / "store").mkdir(parents=True)
    (tmp_path / "pkg" / "model.py").write_text(MODEL)
    (tmp_path / "pkg" / "store" / "__init__.py").write_text("def open_store(p): pass\n")
    (tmp_path / "pkg" / "schema.py").write_text(SCHEMA)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text("def test(): pass\n")
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "2026-09-28-work-w0.log").write_text(LOG)
    (tmp_path / ".sovereign_config.json").write_text(json.dumps(
        {"language": "python", "locked_files": ["pkg/model.py", "pkg/schema.py"]}))
    return tmp_path


def test_surfaces_and_sql(proj):
    classes, modules, tables = api_surface.py_surfaces(str(proj))
    assert classes["Issue"]["fields"] == ["key", "labels"]
    assert classes["Color"]["enum"] is True
    assert modules["pkg.store"]["names"] == ["open_store"]
    assert tables == {"issues": ["key", "labels"], "meta": ["key", "value"]}


def test_mine_python_turns_errors_into_guards(proj):
    pats = {g["pattern"]: g["hint"] for g in api_surface.mine_python(str(proj))}
    joined = " ".join(pats)
    assert "summary" in joined and "get_conn" in joined and "repo" in joined and "subject" in joined
    assert "nope" not in joined                  # builtins are not our contract
    assert "labels" not in joined                # it exists: transient error, no guard
    assert any("open_store" in h for h in pats.values())


def test_guard_never_matches_committed_code(proj):
    (proj / "pkg" / "report.py").write_text("x = issue_like.summary\n")   # legit elsewhere
    for g in api_surface.mine_python(str(proj)):
        if "summary" in g["hint"]:
            assert g["pattern"] != r"\.summary\b"                         # type-anchored instead


def test_rows_python(proj):
    rows = api_surface.rows_python(str(proj))
    assert any("`Issue` fields" in r for r in rows)
    assert any("`Color` members" in r and "`Color.RED`" in r for r in rows)
    assert any("table `issues` columns" in r for r in rows)


def test_transferable_rules(proj):
    names = kickoff.own_names(str(proj), "python")
    files = kickoff.tree_files(str(proj), "python")
    assert kickoff.transferable({"pattern": r"datetime\.utcnow\(", "hint": "h"}, names, files)
    assert not kickoff.transferable({"pattern": r"\bTeamPalette\b", "hint": "h"}, names, files)
    assert kickoff.transferable({"pattern": r"\bIssue\([^)]*repo=", "hint": "h"}, names, files)
    assert not kickoff.transferable({"pattern": r"def test", "hint": "h"}, names, files)  # hits tree
    assert not kickoff.transferable({"pattern": "x", "hint": "h", "except_paths": ["a"]}, names, files)
