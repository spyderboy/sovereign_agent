"""The think switch reaching Ollama: OLLAMA_THINK, and the per-attempt
override the runner sets from OLLAMA_THINK_RETRIES."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sov import llm, state  # noqa: E402


class FakeResp:
    status_code = 200

    def raise_for_status(self):
        pass

    def iter_lines(self):
        yield json.dumps({"message": {"content": "ok"}, "done": False}).encode()
        yield json.dumps({"message": {"content": ""}, "done": True,
                          "prompt_eval_count": 1}).encode()

    def close(self):
        pass


@pytest.fixture
def sent(monkeypatch):
    bodies = []

    def fake_post(url, json=None, **kw):
        bodies.append(json)
        return FakeResp()

    monkeypatch.setattr(llm.requests, "post", fake_post)
    monkeypatch.setattr(state, "THINK_OVERRIDE", None)
    return bodies


def call():
    llm.ollama("qwen2.5-coder:14b", "sys", "user", timeout=30)


def test_unset_sends_no_think_key(sent, monkeypatch):
    monkeypatch.setattr(llm, "OLLAMA_THINK", None)
    call()
    assert "think" not in sent[-1]


def test_global_switch(sent, monkeypatch):
    monkeypatch.setattr(llm, "OLLAMA_THINK", False)
    call()
    assert sent[-1]["think"] is False


def test_per_attempt_override_wins(sent, monkeypatch):
    monkeypatch.setattr(llm, "OLLAMA_THINK", None)
    monkeypatch.setattr(state, "THINK_OVERRIDE", False)     # attempt 2+, retries fast
    call()
    assert sent[-1]["think"] is False
    monkeypatch.setattr(state, "THINK_OVERRIDE", None)      # attempt 1 again
    call()
    assert "think" not in sent[-1]


def test_large_model_unloads_other_models_first(monkeypatch):
    posts, gets = [], []

    class Ps:
        def json(self):
            return {"models": [{"name": "qwen2.5-coder:14b"}, {"name": "qwen3.8:27b"}]}

    monkeypatch.setattr(llm.requests, "get", lambda url, **k: gets.append(url) or Ps())
    monkeypatch.setattr(llm.requests, "post", lambda url, json=None, **k: posts.append((url, json)))
    assert llm._unload_others("qwen3.8:27b") == ["qwen2.5-coder:14b"]
    assert posts == [(posts[0][0], {"model": "qwen2.5-coder:14b", "keep_alive": 0})]


def test_unload_never_raises(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("ollama down")
    monkeypatch.setattr(llm.requests, "get", boom)
    assert llm._unload_others("qwen3.8:27b") == []
