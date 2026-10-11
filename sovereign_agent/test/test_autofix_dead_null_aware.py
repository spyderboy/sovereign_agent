import autofix


def _fix(tmp_path, src, analyze):
    (tmp_path / 'lib').mkdir()
    f = tmp_path / 'lib' / 'a.dart'
    f.write_text(src)
    n, unfixed = autofix.apply_mechanical_fixes(analyze, str(tmp_path))
    return n, f.read_text()


def _warn(line, col):
    return (f"warning • The left operand can't be null, so the right operand is never "
            f"executed. Try removing the operator and the right operand • "
            f"lib/a.dart:{line}:{col} • dead_null_aware_expression\n")


def test_removes_list_default_in_argument(tmp_path):
    src = "f() {\n                items: task.checklist ?? [],\n}\n"
    line = src.splitlines()[1]
    col = line.index('[]') + 1
    n, out = _fix(tmp_path, src, _warn(2, col))
    assert n == 1
    assert "items: task.checklist,\n" in out


def test_operand_with_call_and_closing_paren(tmp_path):
    src = "x = foo(a.name ?? fallback(1, 2));\n"
    col = src.index('fallback') + 1
    n, out = _fix(tmp_path, src, _warn(1, col))
    assert n == 1
    assert out == "x = foo(a.name);\n"


def test_no_operator_before_column_leaves_file(tmp_path):
    src = "x = a + b;\n"
    n, out = _fix(tmp_path, src, _warn(1, 9))
    assert n == 0
    assert out == src


def test_two_on_one_line_both_removed(tmp_path):
    src = "g(a ?? 1, b ?? 2);\n"
    out_cols = [src.index('1') + 1, src.index('2') + 1]
    n, out = _fix(tmp_path, src, _warn(1, out_cols[0]) + _warn(1, out_cols[1]))
    assert n == 2
    assert out == "g(a, b);\n"
