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


def test_a_field_named_like_a_function_elsewhere_is_not_imported(tmp_path):
    (tmp_path / "pubspec.yaml").write_text("name: app\n")
    lib = tmp_path / "lib"
    lib.mkdir()
    (lib / "milestones.dart").write_text("List<int> milestones(int n) => [n];\n")
    (lib / "card.dart").write_text(
        "class MilestonesCard {\n  final List<int> milestones;\n  const MilestonesCard({required this.milestones});\n"
        "  int get n => milestones.length;\n}\n")
    (lib / "user.dart").write_text("int count() => milestones(3).length;\n")
    assert f.fix_dart_imports(str(tmp_path), ["lib/card.dart"]) == []
    fixes = f.fix_dart_imports(str(tmp_path), ["lib/user.dart"])
    assert fixes and "milestones.dart" in fixes[0]


def test_member_access_labels_and_method_declarations_are_not_uses(tmp_path):
    (tmp_path / "pubspec.yaml").write_text("name: app\n")
    lib = tmp_path / "lib"
    lib.mkdir()
    (lib / "fns.dart").write_text("int milestones() => 1;\nint objectiveNames() => 2;\nvoid deleteTask(int r, String id) {}\n")
    (lib / "a.dart").write_text("int f(dynamic model) => model.milestones.length + g(objectiveNames: 3);\nint g({int objectiveNames = 0}) => 0;\n")
    (lib / "repo.dart").write_text("abstract class Repo {\n  Future<void> deleteTask(String id);\n}\n")
    (lib / "b.dart").write_text("void h() {\n  deleteTask(1, 'x');\n  final n = milestones();\n}\n")
    assert f.fix_dart_imports(str(tmp_path), ["lib/a.dart", "lib/repo.dart"]) == []
    fixes = f.fix_dart_imports(str(tmp_path), ["lib/b.dart"])
    assert fixes and "fns.dart" in fixes[0]


def test_symbols_reached_through_an_export_are_already_imported(tmp_path):
    (tmp_path / "pubspec.yaml").write_text("name: app\n")
    lib = tmp_path / "lib"
    lib.mkdir()
    (lib / "gem_transform.dart").write_text("class GemTransform {}\n")
    (lib / "gem_painter.dart").write_text("export 'gem_transform.dart';\nclass GemPainter {}\n")
    (lib / "view.dart").write_text("import 'gem_painter.dart';\nGemTransform? t;\nGemPainter? p;\n")
    assert f.fix_dart_imports(str(tmp_path), ["lib/view.dart"]) == []
