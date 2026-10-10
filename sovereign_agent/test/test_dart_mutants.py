"""refcheck.dart_mutants: code-only textual mutants for Dart targets."""
from sov.refcheck import dart_mutants

SRC = """import 'package:flutter/material.dart'; // a == b
import '../tr.dart';

/// a && b in a doc comment
String keyFor(int n, bool on) => n == 0 && !on ? t('nav.wishlist') : 'a == b';
Widget w() => Key('menu') == null ? const SizedBox() : Text(t('task.postpone'));
List<int> xs(List<int> a) => a.isEmpty ? const [] : [for (final x in a) if (x > 1) x];
"""


def test_only_code_is_mutated_and_keys_are_tried():
    descs = [d for d, _ in dart_mutants(SRC, 50)]
    joined = "\\n".join(descs)
    assert "'nav.wishlist' → 'nav.wishlistX'" in joined
    assert "'menu' → 'menuX'" in joined
    assert "'task.postpone' → 'task.postponeX'" in joined
    assert any("&& → ||" in d for d in descs)
    assert any("isEmpty → .isNotEmpty" in d for d in descs)
    assert any("> → >=" in d for d in descs)
    assert any("! → removed" in d for d in descs)
    # strings, comments and imports are untouched: line 1 and the 'a == b' literal never mutate
    for d, mut in dart_mutants(SRC, 50):
        assert mut.splitlines()[0] == SRC.splitlines()[0], d
        assert "'a == b'" in mut, d
        assert "/// a && b in a doc comment" in mut, d
    # generics are not comparisons
    assert not any("List<int>" not in m for _, m in dart_mutants(SRC, 50) if "< →" in _)


def test_limit_spreads_sites():
    assert len(dart_mutants(SRC, 3)) == 3
