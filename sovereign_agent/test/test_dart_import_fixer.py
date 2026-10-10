"""dart_import_fixer: only real top-level declarations are indexed."""
import dart_import_fixer as f

WIDGET = """import 'package:flutter/material.dart';

class ObjectivesList extends StatelessWidget {
  const ObjectivesList({super.key});

  @override
  Widget build(BuildContext context) {
    return ListView(
      children: [1, 2].map((o) {
        return ExpansionTile(title: Text('$o'));
      }).toList(),
    );
  }
}
"""


def test_calls_inside_a_method_are_not_declarations():
    assert f.declared_symbols(WIDGET) == {"ObjectivesList"}


def test_top_level_functions_still_indexed():
    src = ("List<String> statusMoves(int from) => const [];\n"
           "Future<void> pushPage(BuildContext c, String t) async {}\n"
           "Map<String, String> objectiveNames(List<Objective> o) => {};\n"
           "String key(int a) {\n  return '';\n}\n")
    assert f.declared_symbols(src) == {"statusMoves", "pushPage", "objectiveNames", "key"}


def test_no_stray_import_for_a_widget_name(tmp_path):
    (tmp_path / "pubspec.yaml").write_text("name: app\n")
    lib = tmp_path / "lib"
    lib.mkdir()
    (lib / "objectives_list.dart").write_text(WIDGET)
    (lib / "wishlist_list.dart").write_text(
        "import 'package:flutter/material.dart';\n\n"
        "class WishlistList extends StatelessWidget {\n  const WishlistList({super.key});\n"
        "  @override\n  Widget build(BuildContext context) => ListView(children: const []);\n}\n")
    assert f.fix_dart_imports(str(tmp_path), ["lib/wishlist_list.dart"]) == []
