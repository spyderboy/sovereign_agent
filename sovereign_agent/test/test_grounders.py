"""
Tests for the per-language grounding gates (grounders.py, dart_grounding.py).

The Dart cases are taken from the real failures in logs/failure_ledger.tsv and
logs/errors.jsonl — the corpus that motivated the gate:

    146x  uri_does_not_exist    e.g. package:your_app_name/products/product_interface.dart
    139x  undefined_function
     73x  undefined_identifier
     45x  undefined_class

Run:  python test/test_grounders.py        (no pytest needed)
"""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import grounders          # noqa: E402
import dart_grounding     # noqa: E402


# ── Fixture ──────────────────────────────────────────────────────────────────

PUBSPEC = """\
name: galaxican
description: A fixture app.
publish_to: 'none'
version: 1.0.0

environment:
  sdk: '>=3.0.0 <4.0.0'

dependencies:
  flutter:
    sdk: flutter
  http: ^1.1.0
  flutter_riverpod: ^2.4.0

dev_dependencies:
  flutter_test:
    sdk: flutter
  mocktail: ^1.0.0
"""

USER_DART = """\
class User {
  final String id;
  final String email;
  const User({required this.id, required this.email});

  bool isValidEmail() => email.contains('@');
  String displayName() => email.split('@').first;
}

class UserRepository {
  final Map<String, User> _cache = {};

  Future<User?> getUserById(String id) async => _cache[id];
  Future<void> saveUser(User user) async { _cache[user.id] = user; }
  void clearCache() { _cache.clear(); }
}
"""

VALIDATOR_DART = """\
class UserInputValidator {
  bool validateEmail(String value) => value.contains('@');
  bool validatePassword(String value) => value.length >= 8;
  String? errorFor(String field) => null;
}
"""


def make_dart_project(with_package_config: bool = False) -> str:
    root = tempfile.mkdtemp(prefix="dg_")
    with open(os.path.join(root, ".sovereign_config.json"), "w") as f:
        json.dump({"language": "dart", "project": "galaxican"}, f)
    with open(os.path.join(root, "pubspec.yaml"), "w") as f:
        f.write(PUBSPEC)
    os.makedirs(os.path.join(root, "lib", "user"), exist_ok=True)
    os.makedirs(os.path.join(root, "lib", "validation"), exist_ok=True)
    with open(os.path.join(root, "lib", "user", "user.dart"), "w") as f:
        f.write(USER_DART)
    with open(os.path.join(root, "lib", "validation", "validator.dart"), "w") as f:
        f.write(VALIDATOR_DART)
    if with_package_config:
        os.makedirs(os.path.join(root, ".dart_tool"), exist_ok=True)
        with open(os.path.join(root, ".dart_tool", "package_config.json"), "w") as f:
            json.dump({"configVersion": 2, "packages": []}, f)
    dart_grounding.invalidate_project_cache(root)
    dart_grounding._sdk_cache.pop(root, None)
    return root


def check(root, rel, content, **kw):
    return grounders.for_language("dart").check(rel, content, root, **kw)


# ── Import resolution: the 146x error class ──────────────────────────────────

def test_rejects_invented_own_package_uri():
    """The literal failure from the ledger."""
    root = make_dart_project()
    src = ("import 'package:your_app_name/products/product_interface.dart';\n"
           "class Cart {}\n")
    v = check(root, "lib/cart/cart.dart", src)
    assert v, "should reject a package that is not this project"
    assert "your_app_name" in v[0] and "UNKNOWN IMPORT" in v[0], v


def test_rejects_own_package_path_that_does_not_exist():
    root = make_dart_project()
    src = ("import 'package:galaxican/orders/order_totals.dart';\n"
           "class Checkout {}\n")
    v = check(root, "lib/cart/cart.dart", src)
    assert v and "lib/orders/order_totals.dart" in v[0], v


def test_accepts_real_own_package_path():
    root = make_dart_project()
    src = ("import 'package:galaxican/user/user.dart';\n"
           "class Session { final User user; Session(this.user); }\n")
    assert check(root, "lib/session/session.dart", src) == []


def test_accepts_declared_third_party_and_sdk():
    root = make_dart_project()
    src = ("import 'package:flutter/material.dart';\n"
           "import 'package:http/http.dart';\n"
           "import 'package:flutter_riverpod/flutter_riverpod.dart';\n"
           "import 'dart:async';\n"
           "import 'dart:math';\n"
           "class Screen extends StatelessWidget {}\n")
    assert check(root, "lib/ui/screen.dart", src) == []


def test_rejects_undeclared_third_party():
    """pubspec.yaml is locked; inventing a dependency is not a fix."""
    root = make_dart_project()
    src = "import 'package:dio/dio.dart';\nclass Api {}\n"
    v = check(root, "lib/api/api.dart", src)
    assert v and "dio" in v[0] and "pubspec.yaml is locked" in v[0], v


def test_rejects_fake_dart_core_lib():
    root = make_dart_project()
    v = check(root, "lib/a.dart", "import 'dart:widgets';\nclass A {}\n")
    assert v and "dart:widgets" in v[0], v


def test_relative_import_resolution():
    root = make_dart_project()
    ok = check(root, "lib/session/s.dart", "import '../user/user.dart';\nclass S {}\n")
    assert ok == [], ok
    bad = check(root, "lib/session/s.dart", "import '../user/ghost.dart';\nclass S {}\n")
    assert bad and "ghost.dart" in bad[0], bad


def test_sibling_file_in_same_changeset_is_accepted():
    """Two files written together may import each other before either lands."""
    root = make_dart_project()
    src = ("import 'package:galaxican/cart/cart_item.dart';\n"
           "class Cart {}\n")
    v = check(root, "lib/cart/cart.dart", src,
              extra_files={"lib/cart/cart_item.dart"})
    assert v == [], v


# ── Identifier grounding ─────────────────────────────────────────────────────

def test_degraded_mode_flags_near_miss_only():
    """No package_config.json → cannot scan the SDK → only near-misses of real
    project names are flagged, mirroring grounding.py without GOROOT."""
    root = make_dart_project()
    near = ("import 'package:galaxican/user/user.dart';\n"
            "class S { void go(UserRepositry r) {} }\n")   # typo
    v = check(root, "lib/s.dart", near)
    assert any("UserRepositry" in x for x in v), v
    # A completely unrelated unknown name is NOT flagged in degraded mode.
    far = ("import 'package:galaxican/user/user.dart';\n"
           "class S { void go(ZZTopWidgetThing t) {} }\n")
    assert not any("ZZTopWidgetThing" in x for x in check(root, "lib/s.dart", far))


def test_legitimate_project_code_is_not_flagged():
    root = make_dart_project()
    src = ("import 'package:galaxican/user/user.dart';\n"
           "import 'package:galaxican/validation/validator.dart';\n"
           "class SignupFlow {\n"
           "  final UserRepository repo;\n"
           "  final UserInputValidator validator;\n"
           "  SignupFlow(this.repo, this.validator);\n"
           "  Future<void> submit(User user) async {\n"
           "    if (validator.validateEmail(user.email)) {\n"
           "      await repo.saveUser(user);\n"
           "    }\n"
           "  }\n"
           "}\n")
    assert check(root, "lib/signup/signup_flow.dart", src) == []


def test_codegen_files_are_skipped():
    root = make_dart_project()
    src = "import 'package:totally_made_up/x.dart';\nclass A {}\n"
    assert check(root, "lib/models/user.g.dart", src) == []
    assert not grounders.for_language("dart").handles("lib/models/user.g.dart")


def test_part_of_file_skips_identifier_check():
    root = make_dart_project()
    src = "part of 'user.dart';\n\nextension E on User { void ping() {} }\n"
    assert check(root, "lib/user/user_ext.dart", src) == []


# ── Preflight: the 76-failure environment bug ────────────────────────────────

def test_preflight_catches_missing_pub_get():
    root = make_dart_project(with_package_config=False)
    problems = grounders.for_language("dart").preflight(root)
    assert any("pub get" in p for p in problems), problems


def test_preflight_clean_when_resolved():
    root = make_dart_project(with_package_config=True)
    assert grounders.for_language("dart").preflight(root) == []


def test_preflight_flags_non_dart_dir():
    root = tempfile.mkdtemp(prefix="dg_empty_")
    assert grounders.for_language("dart").preflight(root)


# ── Registry / protocol ──────────────────────────────────────────────────────

def test_registry_resolves_aliases():
    assert grounders.for_language("flutter").language == "dart"
    assert grounders.for_language("golang").language == "go"
    assert grounders.for_language("nextjs").language == "typescript"


def test_unknown_language_gets_null_grounder():
    g = grounders.for_language("cobol")
    assert g.check("a.cob", "anything", "/tmp") == []
    assert not g.handles("a.cob")
    assert g.preflight("/tmp") == []


def test_all_grounders_satisfy_protocol():
    for name in grounders.supported():
        g = grounders.for_language(name)
        assert isinstance(g, grounders.Grounder), name
        for method in ("handles", "repair", "declared_names", "preflight", "check"):
            assert callable(getattr(g, method)), f"{name}.{method}"


def test_go_grounder_still_balances_braces():
    g = grounders.for_language("go")
    fixed, note = g.repair("package a\nfunc f() {\n  if true {\n")
    assert fixed.count("}") == 2 and note, (fixed, note)


def test_typescript_relative_import_gate():
    root = tempfile.mkdtemp(prefix="ts_")
    os.makedirs(os.path.join(root, "src", "lib"), exist_ok=True)
    open(os.path.join(root, "src", "lib", "util.ts"), "w").write("export const a = 1;\n")
    g = grounders.for_language("typescript")
    assert g.check("src/app/page.tsx", "import { a } from '../lib/util';", root) == []
    bad = g.check("src/app/page.tsx", "import { b } from '../lib/ghost';", root)
    assert bad and "ghost" in bad[0], bad


def test_no_grounder_raises_on_garbage():
    for name in grounders.supported():
        g = grounders.for_language(name)
        assert g.check("x" + g.suffixes[0], "\x00 not source at all {{{",
                       "/nonexistent/zzz") is not None


# ── Runner ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  ✓ {name}")
        except AssertionError as e:
            failed += 1
            print(f"  ✗ {name}\n      {str(e)[:300]}")
        except Exception as e:
            failed += 1
            print(f"  ✗ {name} — ERROR {type(e).__name__}: {e}")
    print(f"\n  {len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
