"""llm.ollama: a reasoning model that thinks through a whole attempt without answering is retried with
thinking off, and stays on no-think for the rest of the process (2026-10-19, EstateWise)."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sov import llm  # noqa: E402


class FakeResponse:
    def __init__(self, chunks):
        self.chunks = chunks

    def raise_for_status(self):
        pass

    def iter_lines(self):
        for c in (self.chunks() if callable(self.chunks) else self.chunks):  # lazy: the clock moves as it streams
            yield json.dumps(c).encode()

    def close(self):
        pass


@pytest.fixture
def fake(monkeypatch):
    calls = []
    clock = {"t": 0.0}
    monkeypatch.setattr(llm.time, "time", lambda: clock["t"])
    monkeypatch.setattr(llm, "_model_size_gb", lambda m: 1.0)        # small: no unloading, no lock
    monkeypatch.setattr(llm, "_unload_others", lambda *a, **k: [])
    monkeypatch.setattr(llm, "heartbeat", lambda *a, **k: None)
    monkeypatch.setattr(llm, "endpoint_for", lambda m: "http://ollama.test")
    monkeypatch.setattr(llm, "_NO_THINK_MODELS", set())
    monkeypatch.setattr(llm.state, "THINK_OVERRIDE", None)
    monkeypatch.setattr(llm, "OLLAMA_THINK", None)

    def post(url, json=None, **kw):
        calls.append(json.get("think", "unset"))
        if json.get("think") is False:
            return FakeResponse([{"message": {"content": "ANSWER"}}, {"done": True}])
        # Thinking: reasoning chunks while the clock runs past the deadline, never an answer.
        def chunks():
            for _ in range(5):
                clock["t"] += 100
                yield {"message": {"thinking": "hmm "}}
        return FakeResponse(chunks)

    monkeypatch.setattr(llm.requests, "post", post)
    return calls, clock


def test_reasoning_through_the_deadline_retries_without_thinking(fake):
    calls, _ = fake
    assert llm.ollama("qwen-thinker", "sys", "user", timeout=300) == "ANSWER"
    assert calls == ["unset", False]
    assert "qwen-thinker" in llm._NO_THINK_MODELS


def test_after_that_the_model_goes_straight_to_no_think(fake):
    calls, _ = fake
    llm.ollama("qwen-thinker", "sys", "user", timeout=300)
    calls.clear()
    assert llm.ollama("qwen-thinker", "sys", "user", timeout=300) == "ANSWER"
    assert calls == [False]


def test_thinking_already_off_still_raises(fake, monkeypatch):
    calls, clock = fake
    monkeypatch.setattr(llm.state, "THINK_OVERRIDE", False)
    def slow(url, json=None, **kw):
        calls.append(json.get("think"))
        def chunks():
            for _ in range(5):
                clock["t"] += 100
                yield {"message": {"content": ""}}
        return FakeResponse(chunks)
    monkeypatch.setattr(llm.requests, "post", slow)
    with pytest.raises(llm.ReadTimeout):
        llm.ollama("qwen-thinker", "sys", "user", timeout=300)


def test_a_model_that_answers_slowly_is_not_switched(fake, monkeypatch):
    calls, clock = fake
    def slow_answer(url, json=None, **kw):
        calls.append(json.get("think", "unset"))
        def chunks():
            for _ in range(5):
                clock["t"] += 100
                yield {"message": {"content": "x"}}
        return FakeResponse(chunks)
    monkeypatch.setattr(llm.requests, "post", slow_answer)
    with pytest.raises(llm.ReadTimeout) as e:
        llm.ollama("slow-writer", "sys", "user", timeout=300)
    assert not isinstance(e.value, llm.ThinkingTimeout)
    assert "slow-writer" not in llm._NO_THINK_MODELS
    assert calls == ["unset"]
