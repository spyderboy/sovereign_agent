"""The final tier can be a self-hosted Ollama model instead of Claude
(2026-09-30): code that may not leave the building still gets an escalation."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sov import escalation  # noqa: E402


def test_self_hosted_model_takes_the_final_tier(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(escalation, "ESCALATION_MODEL", "llama3.3:70b")
    monkeypatch.setattr(escalation.llm, "ollama",
                        lambda model, system, user, timeout=0:
                        calls.append((model, system)) or '{"pkg/a.py": "x = 1\\n"}')
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")            # Claude is not reachable
    monkeypatch.chdir(tmp_path)
    changes, ok = escalation._escalate_to_claude("In `pkg/a.py`: x", {}, "err", str(tmp_path), False)
    assert ok and changes == {"pkg/a.py": "x = 1\n"}
    assert calls[0][0] == "llama3.3:70b"
    assert "Dart" not in calls[0][1]                        # stack-neutral prompt


def test_chronic_path_uses_it_too(monkeypatch, tmp_path):
    monkeypatch.setattr(escalation, "ESCALATION_MODEL", "llama3.3:70b")
    monkeypatch.setattr(escalation.llm, "ollama",
                        lambda *a, **k: '{"files": {"pkg/a.py": "x = 1\\n"}, "new_rule": ""}')
    monkeypatch.chdir(tmp_path)
    files, ok = escalation._escalate_to_claude_chronic("In `pkg/a.py`: x", {}, 2, str(tmp_path), False)
    assert ok and files == {"pkg/a.py": "x = 1\n"}


def test_a_failing_endpoint_is_reported_not_raised(monkeypatch, tmp_path):
    def down(*a, **k):
        raise ConnectionError("pod stopped")
    monkeypatch.setattr(escalation, "ESCALATION_MODEL", "llama3.3:70b")
    monkeypatch.setattr(escalation.llm, "ollama", down)
    monkeypatch.chdir(tmp_path)
    assert escalation._escalate_to_claude("t", {}, "e", str(tmp_path), False) == ({}, False)


def test_without_it_the_final_tier_is_claude(monkeypatch):
    monkeypatch.setattr(escalation, "ESCALATION_MODEL", "")
    assert escalation.final_tier_name().startswith("Claude")


def test_console_plan_writes_and_validates_the_final_tier():
    from sov import runctl
    plan = runctl.load_plan("/tmp/some-project")
    assert runctl.validate(plan) == [] or all("final-tier" not in e for e in runctl.validate(plan))
    plan["strong"]["escalation_url"] = "gpu-box:11434"
    errs = runctl.validate(plan)
    assert any("must be a URL" in e for e in errs) and any("needs a final-tier model" in e for e in errs)
    plan["strong"].update(escalation_model="llama3.3:70b", escalation_url="http://gpu-box:11434/")
    assert not any("final-tier" in e for e in runctl.validate(plan))
    text = runctl.profile_text(plan, "strong")
    assert 'escalation_model = "llama3.3:70b"' in text and 'escalation_url = "http://gpu-box:11434"' in text
    assert "escalation" not in runctl.profile_text(plan, "cheap")
