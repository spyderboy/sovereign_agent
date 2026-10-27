from sov.pairs import apply_exact_pairs, exact_pairs, pair_target

FILE = "import 'a.dart';\n\nclass Shell {\n  final tabs = [Dashboard(), Predict()];\n}\n"


def _project(tmp_path):
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib" / "shell.dart").write_text(FILE)
    return str(tmp_path)


def test_reads_pairs_with_line_breaks():
    task = "Edit lib/shell.dart — REPLACE `a\\nb` WITH `c`. REPLACE `x` WITH `y\\nz`. — task gate: flutter test passes"
    assert exact_pairs(task) == [("a\nb", "c"), ("x", "y\nz")]


def test_finds_the_target_in_each_task_style(tmp_path):
    root = _project(tmp_path)
    assert pair_target("- [ ] Edit lib/shell.dart — REPLACE `a` WITH `b`.", root) == "lib/shell.dart"
    assert pair_target("In `lib/shell.dart`: REPLACE `a` WITH `b`.", root) == "lib/shell.dart"
    assert pair_target("Edit lib/missing.dart — REPLACE `a` WITH `b`.", root) is None
    assert pair_target("Edit lib/shell.dart — add a People tab.", root) is None


def test_applies_every_pair_in_order(tmp_path):
    root = _project(tmp_path)
    task = ("Edit lib/shell.dart — REPLACE `import 'a.dart';` WITH `import 'a.dart';\\nimport 'p.dart';`. "
            "REPLACE `Predict()` WITH `People()`. — task gate: flutter test test/shell_test.dart passes")
    target, body = apply_exact_pairs(task, root)
    assert target == "lib/shell.dart"
    assert body == "import 'a.dart';\nimport 'p.dart';\n\nclass Shell {\n  final tabs = [Dashboard(), People()];\n}\n"
    assert (tmp_path / "lib" / "shell.dart").read_text() == FILE  # never writes


def test_refuses_when_old_text_is_missing_or_repeated(tmp_path):
    root = _project(tmp_path)
    assert apply_exact_pairs("Edit lib/shell.dart — REPLACE `Nowhere()` WITH `X()`.", root) is None
    assert apply_exact_pairs("Edit lib/shell.dart — REPLACE `a` WITH `b`.", root) is None  # 'a' occurs many times


def test_refuses_a_malformed_pair(tmp_path):
    root = _project(tmp_path)
    task = "Edit lib/shell.dart — REPLACE `Predict()` WITH `People()`. REPLACE `Dashboard()` with nothing."
    assert apply_exact_pairs(task, root) is None
