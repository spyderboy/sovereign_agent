"""_extract_py_api_stub: the read-only surface workers see for a locked .py file.

Loaded from work.py's source rather than by importing work.py, which has
import-time side effects (config, env). Guards the 2026-09-28 fix: .py files
used to fall through to the Dart extractor and produce an empty stub.
"""
import ast
from pathlib import Path

WORK = Path(__file__).resolve().parents[1] / "work.py"


def _load(name: str):
    src = WORK.read_text()
    tree = ast.parse(src)
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
    ns: dict = {}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), str(WORK), "exec"), ns)
    return ns[name]


SAMPLE = '''
from dataclasses import dataclass
from enum import Enum
import datetime as dt

SCHEMA_VERSION = 1

class Source(Enum):
    GIT = "git"

@dataclass(frozen=True)
class PullRequest:
    number: int
    merged_at: dt.datetime | None = None
    def is_merged(self) -> bool:
        """True when merged."""
        return self.merged_at is not None

def cycle_time_hours(pr: PullRequest, *, business: bool = False) -> float | None:
    return 0.0

def _private():
    pass
'''


def test_keeps_constants_fields_members_and_signatures():
    stub = _load("_extract_py_api_stub")(SAMPLE)
    assert "SCHEMA_VERSION = 1" in stub
    assert "GIT = 'git'" in stub
    assert "merged_at: dt.datetime | None = None" in stub
    assert "def is_merged(self) -> bool: ...  # True when merged." in stub
    assert "def cycle_time_hours(pr: PullRequest, *, business: bool=False) -> float | None: ..." in stub


def test_drops_bodies_privates_and_imports():
    stub = _load("_extract_py_api_stub")(SAMPLE)
    assert "return" not in stub
    assert "_private" not in stub
    assert "import" not in stub


def test_unparseable_source_is_returned_raw():
    assert _load("_extract_py_api_stub")("def broken(:\n") == "def broken(:\n"


def test_dispatch_routes_py_to_python_extractor():
    src = WORK.read_text()
    assert 'if ext == ".py":\n        return _extract_py_api_stub(' in src
