"""Skip-dirs must match inside the project, not in the path TO it: a project
under ~/work/web/ or .../build/ had every file skipped (2026-09-30)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dart_grounding     # noqa: E402
import prompt_artifacts   # noqa: E402


def _project(tmp_path):
    root = tmp_path / "web" / "build" / "proj"          # skip-dir names ABOVE the project
    (root / "lib").mkdir(parents=True)
    (root / "lib" / "a.dart").write_text("class Widgetron {}\n")
    (root / "build").mkdir()
    (root / "build" / "gen.dart").write_text("class Generatedon {}\n")
    return root


def test_dart_grounding_scans_a_project_under_a_skip_named_dir(tmp_path):
    root = _project(tmp_path)
    tokens = dart_grounding.project_whitelist(str(root))
    assert "Widgetron" in tokens and "Generatedon" not in tokens


def test_language_detection_under_a_skip_named_dir(tmp_path):
    root = _project(tmp_path)
    assert dict(prompt_artifacts.detect_project_languages(str(root))).get("dart") == 1


def test_prompt_whitelist_under_a_skip_named_dir(tmp_path):
    root = _project(tmp_path)
    prompt_artifacts.invalidate_cache()
    tokens = prompt_artifacts.project_whitelist(str(root), "dart")
    assert "Widgetron" in tokens and "Generatedon" not in tokens
