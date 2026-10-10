"""runner.blame_split: whose failure is it."""
from sov.runner import blame_split

ASSERT = """00:00 +0: done, points
The following TestFailure was thrown running a test:
Expected: exactly one matching candidate
  Actual: _TextWidgetFinder:<Found 0 widgets with text "12": []>
#4      main.<anonymous closure> (file:///w/test/ui/widgets/stats_bar_test.dart:28:7)
#6      TestWidgetsFlutterBinding._runTestBody (package:flutter_test/src/binding.dart:1952:5)
"""


def test_assertion_in_own_gate_is_this_tasks_failure():
    foreign, mine = blame_split(ASSERT, ["lib/ui/widgets/stats_bar.dart"], ["test/ui/widgets/stats_bar_test.dart"])
    assert mine == ["test/ui/widgets/stats_bar_test.dart"]
    assert foreign == []


def test_compile_error_in_an_earlier_file_is_still_foreign():
    out = "lib/ui/widgets/task_card.dart:12:3: Error: Undefined name 'x'.\n(file:///w/test/ui/widgets/board_view_test.dart)"
    foreign, mine = blame_split(out, ["lib/ui/widgets/board_view.dart"], ["test/ui/widgets/board_view_test.dart"])
    assert mine == []
    assert "lib/ui/widgets/task_card.dart" in foreign


def test_error_naming_the_written_file_is_mine():
    out = "error • x • lib/ui/widgets/task_form.dart:38:15 • invalid_assignment"
    foreign, mine = blame_split(out, ["lib/ui/widgets/task_form.dart"], [])
    assert mine == ["lib/ui/widgets/task_form.dart"] and foreign == []
