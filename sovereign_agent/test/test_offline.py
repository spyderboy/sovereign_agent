from sov.offline import no_pub


def test_adds_no_pub_to_flutter_test_and_analyze():
    assert no_pub("flutter test test/logic/x_test.dart") == "flutter test --no-pub test/logic/x_test.dart"
    assert no_pub("flutter analyze --no-fatal-infos lib") == "flutter analyze --no-pub --no-fatal-infos lib"


def test_leaves_commands_that_already_have_it():
    cmd = "flutter analyze --no-pub --no-fatal-infos lib"
    assert no_pub(cmd) == cmd
    assert no_pub("flutter test test/a.dart --no-pub") == "flutter test test/a.dart --no-pub"


def test_each_command_in_a_chain():
    assert no_pub("flutter analyze lib && flutter test --no-pub test/a.dart") == (
        "flutter analyze --no-pub lib && flutter test --no-pub test/a.dart"
    )


def test_other_commands_untouched():
    for cmd in ["npm run typecheck:gate", "npx vitest run test/a.test.ts", "flutter pub get", "pytest -q"]:
        assert no_pub(cmd) == cmd
