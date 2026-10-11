import autofix

SRC = ("import 'package:flutter/material.dart';\n"
       "import '../tr.dart';\n"
       "sprint_screen.dart';\n"
       "\n"
       "class A {}\n")

ANALYZE = """\
  error • Functions must have an explicit list of parameters • lib/a.dart:3:1 • missing_function_parameters
  error • Expected to find ';' • lib/a.dart:3:15 • expected_token
  error • Unterminated string literal • lib/a.dart:3:20 • unterminated_string_literal
  error • Expected a method, getter, setter or operator declaration • lib/a.dart:3:14 • expected_executable
"""


def _run(tmp_path, src, analyze):
    (tmp_path / 'lib').mkdir()
    f = tmp_path / 'lib' / 'a.dart'
    f.write_text(src)
    n, _ = autofix.apply_mechanical_fixes(analyze, str(tmp_path))
    return n, f.read_text()


def test_restores_import_keyword(tmp_path):
    n, out = _run(tmp_path, SRC, ANALYZE)
    assert n >= 1
    assert out.splitlines()[2] == "import 'sprint_screen.dart';"
    assert out.count("import 'sprint_screen.dart';") == 1


def test_leaves_real_code_alone(tmp_path):
    src = "void f() {}\nx = 'a.dart';\n"
    n, out = _run(tmp_path, src, "  error • x • lib/a.dart:2:1 • missing_const_final_var_or_type\n")
    assert n == 0
    assert out == src
