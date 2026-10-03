"""sov/refcheck.py: build-order simulation and weak-gate mutants (2026-10-03)."""
import ast
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sov.refcheck import apply_reference, mutants, simulate  # noqa: E402

REF = '''import math

LIMIT = 3


def _clip(x):
    return min(x, LIMIT)


def first(xs):
    return _clip(len(xs))


def second(xs):
    return math.floor(sum(xs))
'''


def test_apply_reference_takes_only_the_named_function_and_what_it_needs():
    out = apply_reference("In `p/m.py`: implement `first(xs) -> int` — count. — done when: x", None, REF)
    names = {n.name for n in ast.parse(out).body if isinstance(n, ast.FunctionDef)}
    assert names == {"first", "_clip"} and "LIMIT = 3" in out
    out2 = apply_reference("In `p/m.py`: implement `second(xs) -> int` — floor. — done when: x", out, REF)
    assert "import math" in out2 and {"first", "second", "_clip"} <= {
        n.name for n in ast.parse(out2).body if isinstance(n, ast.FunctionDef)}


def test_mutants_cover_the_usual_mistakes():
    f = ast.parse("def f(x, ys):\n    if x > 0 and x in ys:\n        return x + 1\n    return 0\n").body[0]
    kinds = " | ".join(d for d, _ in mutants(f, limit=50))
    for k in ("Gt → GtE", "In → NotIn", "if-condition forced True", "And → Or", "returns None", "constant 1 → 2"):
        assert k in kinds, k


def _repo(tmp_path, test_body):
    run = lambda *a: subprocess.run(["git", *a], cwd=tmp_path, check=True, capture_output=True)  # noqa: E731
    run("init", "-q", "-b", "main"); run("config", "user.email", "t@t"); run("config", "user.name", "t")
    (tmp_path / "p").mkdir()
    (tmp_path / "p" / "__init__.py").write_text("")
    (tmp_path / "test_m.py").write_text(test_body)
    gate = f"{sys.executable} -m pytest -q test_m.py"
    (tmp_path / "ROADMAP.md").write_text(
        f"- [ ] In `p/m.py`: implement `pos(x) -> bool` — True when x > 0. — done when: its tests pass; "
        f"task gate: {gate} passes\n")
    run("add", "-A"); run("commit", "-qm", "init")
    ref = tmp_path.parent / (tmp_path.name + "-ref")
    (ref / "p").mkdir(parents=True)
    (ref / "p" / "m.py").write_text("def pos(x):\n    return x > 0\n")
    return str(tmp_path), str(ref)


def test_a_weak_gate_lets_a_boundary_mistake_survive(tmp_path):
    root, ref = _repo(tmp_path, "from p.m import pos\n\ndef test_it():\n    assert pos(5) and not pos(-5)\n")
    [r] = simulate(root, ref, log=lambda m: None)
    assert r["fails_before"] and r["passes_after"]
    assert any("Gt → GtE" in s for s in r["survivors"])         # pos(0) is never tested


def test_a_strong_gate_kills_it(tmp_path):
    root, ref = _repo(tmp_path, "from p.m import pos\n\ndef test_it():\n    assert pos(5) and pos(1) and not pos(0) and not pos(-5)\n")
    [r] = simulate(root, ref, log=lambda m: None)
    assert r["fails_before"] and r["passes_after"] and r["survivors"] == []


def test_a_hanging_gate_is_killed_with_its_children(tmp_path):
    import time
    from sov.refcheck import run_gate
    t = time.time()
    assert run_gate(f"{sys.executable} -c 'while True: pass'", str(tmp_path), timeout=1) is False
    assert time.time() - t < 5
    out = subprocess.run(["pgrep", "-f", "while True: pass"], capture_output=True, text=True).stdout
    assert out.strip() == ""


def test_isolated_env_drops_editable_finders(tmp_path):
    from sov.refcheck import isolated_env
    env = isolated_env(str(tmp_path))
    out = subprocess.run([sys.executable, "-c", "import sys; print(any('editable' in type(f).__name__.lower() "
                          "for f in sys.meta_path))"], env=env, capture_output=True, text=True).stdout.strip()
    assert out == "False" and env["PYTHONDONTWRITEBYTECODE"] == "1"
