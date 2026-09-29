"""console.py: local-only guards and the plan API, against a live server."""
import json
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import console  # noqa: E402
from sov import runctl  # noqa: E402


@pytest.fixture
def server(tmp_path, monkeypatch):
    proj = tmp_path / "code" / "demo"
    proj.mkdir(parents=True)
    (proj / "ROADMAP.md").write_text("- [x] In `a.py`: a\n- [ ] In `b.py`: b\n")
    (proj / ".sovereign_config.json").write_text("{}")
    monkeypatch.setattr(runctl, "RUNS", tmp_path / "runs")
    monkeypatch.setattr(console, "SETTINGS", tmp_path / "runs" / "console.json")
    monkeypatch.setattr(console, "HERE", tmp_path / "xanadu")
    (tmp_path / "xanadu").mkdir()
    console.save_settings({"projects_dir": str(tmp_path / "code")})
    srv = ThreadingHTTPServer(("127.0.0.1", 0), console.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", str(proj)
    srv.shutdown()


def call(url, path, body=None, token=True, host=None):
    req = urllib.request.Request(url + path, data=json.dumps(body).encode() if body is not None else None)
    if token:
        req.add_header("X-Xanadu-Token", console.TOKEN)
    if host:
        req.add_header("Host", host)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def test_api_needs_the_token(server):
    url, _ = server
    assert call(url, "/api/projects", token=False)[0] == 403
    assert call(url, "/api/launch", {"project": "x"}, token=False)[0] == 403


def test_foreign_host_is_refused(server):
    url, _ = server
    assert call(url, "/api/projects", host="evil.example")[0] == 403


def test_projects_and_plan_round_trip(server):
    url, proj = server
    code, d = call(url, "/api/projects")
    assert code == 200 and d["projects"][0]["name"] == "demo" and d["projects"][0]["done"] == 1
    code, d = call(url, "/api/plan", {"project": proj, "plan": {"cheap": {"workers": 9}}})
    assert code == 400 and any("workers" in e for e in d["errors"])
    code, d = call(url, "/api/plan", {"project": proj, "plan": {"strong": {"model": "big:70b", "params": 70}}})
    assert code == 200 and d["errors"] == []
    code, d = call(url, f"/api/plan?project={proj}")
    assert d["plan"]["strong"]["model"] == "big:70b"


def test_api_key_goes_to_env_file_with_private_mode(server, tmp_path, monkeypatch):
    url, _ = server
    monkeypatch.delenv("RUNPOD_API_KEY", raising=False)
    call(url, "/api/runpod/key", {"key": "rp_secret"})
    env = tmp_path / "xanadu" / ".env"
    assert "RUNPOD_API_KEY=rp_secret" in env.read_text()
    assert oct(env.stat().st_mode & 0o777) == "0o600"
