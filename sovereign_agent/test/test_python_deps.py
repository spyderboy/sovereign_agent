"""Python tasks are deferred while what they call hasn't landed (2026-09-30)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sov.deps import _missing_python_deps  # noqa: E402

GATE = " — done when: its tests pass; task gate: pytest -q tests/x.py passes"


def _proj(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("")
    (tmp_path / "pkg" / "flow.py").write_text("def flow_metric(i, d, w):\n    return 1\n")
    (tmp_path / "pkg" / "API.md").write_text("```python\n# pkg/compute.py\ndef compute_window(d, w, c): ...\n```\n")
    return str(tmp_path)


def test_unlanded_helper_call_is_missing(tmp_path):
    root = _proj(tmp_path)
    task = ("In `pkg/compute.py`: implement `compute_window(d, w, c) -> R` — call `flow_metric(id, d, w)` "
            "and `quality_metric(id, d, w, c)`; imports from pkg.flow and pkg.quality" + GATE)
    assert _missing_python_deps(task, root) == ["pkg/quality.py", "quality_metric()"]


def test_api_md_function_whose_file_is_missing(tmp_path):
    root = _proj(tmp_path)
    task = "In `pkg/build.py`: implement `build(c) -> R` — baseline = compute_window(d, c.baseline, c)" + GATE
    assert _missing_python_deps(task, root) == ["pkg/compute.py"]


def test_no_false_positives(tmp_path):
    root = _proj(tmp_path)
    task = ("In `pkg/cli.py`: implement `main(argv) -> int` — collect (--since), default 'pkg.yaml', "
            "`flow_metric(i, d, w)`, `len(x)`, `print(y)`" + GATE)
    assert _missing_python_deps(task, root) == []


def test_own_parameters_and_imports_are_not_missing(tmp_path):
    root = _proj(tmp_path)
    task = ("In `pkg/c.py`: implement `collect(config: C, log=print, get=fetch) -> dict` — `log(f\"done\")`, "
            "`get(url)`, `replace(config, pilot=None)`; imports `from dataclasses import replace`" + GATE)
    assert _missing_python_deps(task, root) == []


def test_modules_the_gate_test_imports(tmp_path):
    root = _proj(tmp_path)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_html.py").write_text("from pkg.build import build\nfrom pkg.flow import flow_metric\n")
    task = ("In `pkg/html.py`: implement `render(d) -> str` — task gate: pytest -q \"tests/test_html.py\" passes")
    assert _missing_python_deps(task, root) == ["pkg/build.py"]


def test_a_template_another_task_builds_is_a_dependency(tmp_path):
    root = _proj(tmp_path)
    (tmp_path / "ROADMAP.md").write_text(
        "- [ ] In `pkg/report.html.j2`: create the template — task gate: true passes\n"
        "- [ ] In `pkg/html.py`: implement `render(d) -> str` — render 'report.html.j2' — task gate: true passes\n")
    task = "In `pkg/html.py`: implement `render(d) -> str` — render 'report.html.j2'" + GATE
    assert _missing_python_deps(task, root) == ["pkg/report.html.j2"]
    (tmp_path / "pkg" / "report.html.j2").write_text("<html></html>")
    assert _missing_python_deps(task, root) == []
    cfg_task = "In `pkg/cli.py`: implement `main() -> int` — default 'pkg.yaml', `timedelta(days=1)`" + GATE
    assert _missing_python_deps(cfg_task, root) == []      # a config name and a stdlib call


def test_gate_node_scans_only_its_own_test(tmp_path):
    """Sibling tests' imports are not this task's dependencies (2026-10-01: a
    whole-file scan made jira_issue_v2 wait for jira_history, which needs it)."""
    from sov.deps import _missing_python_deps, gate_test_sources
    root = _proj(tmp_path)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_j.py").write_text(
        "import json\n\ndef helper():\n    return 1\n\n"
        "def test_mine():\n    from pkg.flow import flow_metric\n\n"
        "def test_sibling():\n    from pkg.history import fetch_all\n")
    src = gate_test_sources('pytest -q "tests/test_j.py::test_mine"', root)[0]
    assert "test_mine" in src and "helper" in src and "test_sibling" not in src
    task = "In `pkg/issue.py`: implement `map_it(x) -> int` — task gate: pytest -q \"tests/test_j.py::test_mine\" passes"
    assert _missing_python_deps(task, root) == []
    whole = "In `pkg/issue.py`: implement `map_it(x) -> int` — task gate: pytest -q \"tests/test_j.py\" passes"
    assert _missing_python_deps(whole, root) == ["pkg/history.py"]


def test_python_keywords_are_not_calls(tmp_path):
    root = _proj(tmp_path)
    task = ("In `pkg/m.py`: implement `m(item) -> int` — `x if a else ()`, `y or (1)`, `not (b)`" + GATE)
    assert _missing_python_deps(task, root) == []


def test_pairs_private_helpers_and_format_strings(tmp_path):
    root = _proj(tmp_path)
    task = ("In `pkg/io.py`: implement the pair `save_x(conn, xs) -> int` / `load_x(conn) -> tuple` — "
            "`load_x(conn)` round-trips; private helper `_rate(n)` is allowed; "
            "error `f'{name}: unreadable report ({e})'`" + GATE)
    assert _missing_python_deps(task, root) == []


def test_project_import_of_an_unbuilt_function_is_missing(tmp_path):
    root = _proj(tmp_path)                                     # pkg/flow.py defines flow_metric only
    task = ("In `pkg/reopen.py`: implement `r(x) -> int` — calls it. Imports, EXACTLY: "
            "`from pkg.flow import flow_metric, was_reopened`, `from dataclasses import replace`" + GATE)
    assert _missing_python_deps(task, root) == ["was_reopened()"]


def test_template_target_waits_for_what_its_gate_imports(tmp_path):
    root = _proj(tmp_path)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_t.py").write_text("def test_it():\n    from pkg.build import build\n")
    task = "In `pkg/report.html.j2`: include the partials — task gate: pytest -q \"tests/test_t.py\" passes"
    assert _missing_python_deps(task, root) == ["pkg/build.py"]
