"""api_surface --rows states which module each locked type lives in (2026-09-30)."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import api_surface  # noqa: E402


def test_one_import_row_per_locked_module(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "model.py").write_text("class Commit:\n    sha: str\n\nclass Window:\n    start: int\n")
    (tmp_path / "pkg" / "config_schema.py").write_text("class Config:\n    team: str\n")
    (tmp_path / ".sovereign_config.json").write_text(json.dumps(
        {"locked_files": ["pkg/model.py", "pkg/config_schema.py"]}))
    rows = [r for r in api_surface.rows_python(str(tmp_path)) if r.startswith("| import")]
    assert any("`from pkg.config_schema import Config`" in r and "`pkg.model`" in r for r in rows)
    assert any("`from pkg.model import Commit, Window`" in r for r in rows)


def test_rows_list_every_field_and_properties(tmp_path):
    (tmp_path / "pkg").mkdir()
    fields = "".join(f"    f{i}: int\n" for i in range(14))
    (tmp_path / "pkg" / "model.py").write_text(
        "class Big:\n" + fields + "\n    @property\n    def name(self):\n        return 1\n\n"
        "    def contains(self, t):\n        return True\n")
    (tmp_path / ".sovereign_config.json").write_text(json.dumps({"locked_files": ["pkg/model.py"]}))
    row = [r for r in api_surface.rows_python(str(tmp_path)) if r.startswith("| `Big` fields")][0]
    assert "`f13`" in row and "`name` (property)" in row and "`contains()`" in row
