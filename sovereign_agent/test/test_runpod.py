"""sov/runpod.py: spend accounting and the only-our-pods guardrail (API mocked)."""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sov import runpod  # noqa: E402


def ev(at, event, pod="p1", rate="2.00"):
    return json.dumps({"at": at, "event": event, "pod": pod, "costPerHr": rate})


def test_spend_counts_closed_and_open_intervals():
    lines = [ev("2026-09-28T20:00:00+00:00", "create"),
             ev("2026-09-28T21:30:00+00:00", "stop"),              # 1.5h × $2 = $3
             ev("2026-09-28T22:00:00+00:00", "start"),
             ev("2026-09-28T22:00:00+00:00", "create", pod="p2", rate="1.00")]
    now = datetime(2026, 9, 28, 23, 0, tzinfo=timezone.utc)        # p1 1h×2 + p2 1h×1
    assert runpod.spend_since("2026-09-28T19:00:00+00:00", lines, now) == 6.0


def test_spend_only_counts_after_since():
    lines = [ev("2026-09-28T20:00:00+00:00", "start"), ev("2026-09-28T22:00:00+00:00", "stop")]
    assert runpod.spend_since("2026-09-28T21:00:00+00:00", lines) == 2.0


class FakeResp:
    def __init__(self, data, code=200):
        self._d, self.status_code, self.text = data, code, json.dumps(data)

    def json(self):
        return self._d


@pytest.fixture
def api(monkeypatch, tmp_path):
    calls = []
    pods = {"a": {"id": "a", "name": "xanadu-strong", "costPerHr": "2.1"},
            "b": {"id": "b", "name": "clients-own-pod", "costPerHr": "9.0"}}

    def fake(method, url, **kw):
        calls.append((method, url))
        path = url.split("/v1", 1)[1]
        if method == "GET" and path == "/pods":
            return FakeResp(list(pods.values()))
        if method == "GET" and path.startswith("/pods/"):
            return FakeResp(pods[path.split("/")[2]])
        return FakeResp({"id": "new", "name": kw.get("json", {}).get("name"), "costPerHr": "1"})

    monkeypatch.setattr(runpod.requests, "request", fake)
    monkeypatch.setattr(runpod, "api_key", lambda: "k")
    monkeypatch.setattr(runpod, "LEDGER", str(tmp_path / "ledger.jsonl"))
    return calls


def test_lists_only_xanadu_pods(api):
    assert [p["id"] for p in runpod.list_pods()] == ["a"]


def test_refuses_to_touch_other_pods(api):
    with pytest.raises(runpod.RunPodError):
        runpod.stop_pod("b")
    assert ("POST", "https://rest.runpod.io/v1/pods/b/stop") not in api


def test_stop_ours_is_ledgered(api, tmp_path):
    runpod.stop_pod("a")
    assert ("POST", "https://rest.runpod.io/v1/pods/a/stop") in api
    assert '"event": "stop"' in (tmp_path / "ledger.jsonl").read_text()


def test_create_forces_prefix(api):
    pod = runpod.create_pod("strong", "NVIDIA L40S")
    assert pod["name"] == "xanadu-strong"


def test_no_key_is_a_clear_error(monkeypatch):
    monkeypatch.setattr(runpod, "api_key", lambda: "")
    with pytest.raises(runpod.RunPodError, match="API key"):
        runpod.list_pods()
