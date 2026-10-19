"""prompting.ask_for_code: a prose-only reply ("Let me read the files first…") is re-asked once in the same
attempt; a reply with code, or an empty one, is returned as it is (2026-10-19, EstateWise)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sov import prompting  # noqa: E402


def fake_model(monkeypatch, replies):
    calls = []

    def ollama(model, system, user, timeout=300):
        calls.append(user)
        return replies[len(calls) - 1]

    monkeypatch.setattr(prompting, "ollama", ollama)
    return calls


def test_prose_without_code_is_asked_again_once(monkeypatch):
    calls = fake_model(monkeypatch, ["Let me start by reading the test files to understand the full picture.",
                                     '{"lib/a.dart": "void main() {}"}'])
    raw = prompting.ask_for_code("m", "sys", "TASK", 60)
    assert raw == '{"lib/a.dart": "void main() {}"}'
    assert len(calls) == 2
    assert calls[1].startswith("TASK")
    assert "Let me start by reading the test files" in calls[1]
    assert "You cannot read files" in calls[1]


def test_a_reply_with_code_is_not_asked_again(monkeypatch):
    calls = fake_model(monkeypatch, ['Here it is: {"lib/a.dart": "x"}'])
    assert prompting.ask_for_code("m", "sys", "TASK", 60) == 'Here it is: {"lib/a.dart": "x"}'
    assert len(calls) == 1


def test_an_empty_reply_is_left_to_the_usual_no_output_handling(monkeypatch):
    calls = fake_model(monkeypatch, [""])
    assert prompting.ask_for_code("m", "sys", "TASK", 60) == ""
    assert len(calls) == 1


def test_only_one_reask(monkeypatch):
    calls = fake_model(monkeypatch, ["I need to investigate.", "Still investigating."])
    assert prompting.ask_for_code("m", "sys", "TASK", 60) == "Still investigating."
    assert len(calls) == 2
