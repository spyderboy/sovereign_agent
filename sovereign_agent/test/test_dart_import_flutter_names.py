"""dart_import_fixer: a project class named like a Flutter class (EstateWise's model `Key`) is never
imported into a file that imports Flutter — Flutter's own Key is meant there (2026-10-19)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dart_import_fixer as d  # noqa: E402


def project(tmp_path, screen: str):
    (tmp_path / "pubspec.yaml").write_text("name: demo\n")
    (tmp_path / "lib/models").mkdir(parents=True)
    (tmp_path / "lib/models/key.dart").write_text("class Key {\n  final String id;\n  const Key(this.id);\n}\n")
    (tmp_path / "lib/models/room.dart").write_text("class Room {}\n")
    (tmp_path / "lib/screen.dart").write_text(screen)
    return str(tmp_path)


def test_flutter_files_keep_flutters_key_but_still_get_project_imports(tmp_path, monkeypatch):
    monkeypatch.setattr(d, "_FLUTTER_NAMES", {"Key", "State", "Text"})
    root = project(tmp_path, "import 'package:flutter/material.dart';\n\nfinal k = Key('email');\nfinal r = Room();\n")
    d.fix_dart_imports(root, ["lib/screen.dart"])
    out = (tmp_path / "lib/screen.dart").read_text()
    assert "models/key.dart" not in out
    assert "models/room.dart" in out


def test_without_flutter_the_project_key_is_imported(tmp_path, monkeypatch):
    monkeypatch.setattr(d, "_FLUTTER_NAMES", {"Key"})
    root = project(tmp_path, "final k = Key('front door');\n")
    d.fix_dart_imports(root, ["lib/screen.dart"])
    assert "models/key.dart" in (tmp_path / "lib/screen.dart").read_text()
