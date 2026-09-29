#!/usr/bin/env python3
"""console.py — the Xanadu console: choose models, where they run, and
everything else per run; launch, watch and stop runs. Local only.

    .venv/bin/python console.py            # then open http://127.0.0.1:8765
    .venv/bin/python console.py --port 9000 --projects ~/Code

Standard library only (plus what Xanadu already uses), so it runs anywhere
the Xanadu folder does. It binds to 127.0.0.1 — nothing on the network can
reach it — and every action needs a token that is created each time the
console starts and embedded in the page, so a web page open in the same
browser cannot drive it. Hosts other than 127.0.0.1/localhost are refused.
"""
import argparse
import json
import os
import re
import secrets
import sys
import traceback
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from sov import runctl, runpod  # noqa: E402

TOKEN = secrets.token_urlsafe(24)
SETTINGS = runctl.RUNS / "console.json"
PAGE = HERE / "console" / "index.html"

# Suggestions for the model pickers (any Ollama tag can be typed instead).
MODEL_SUGGESTIONS = [
    {"model": "qwen2.5-coder:14b", "params": 14, "note": "fast pass on a 32 GB Mac"},
    {"model": "qwen3.8:27b", "params": 27, "note": "strong pass on a 32 GB Mac; reasons (thinks)"},
    {"model": "qwen2.5-coder:32b", "params": 32, "note": "needs ~24 GB free"},
    {"model": "qwen3-coder:30b", "params": 30, "note": "MoE, fast for its size"},
    {"model": "llama3.3:70b", "params": 70, "note": "RunPod: 48 GB+ GPU"},
    {"model": "qwen2.5:72b", "params": 72, "note": "RunPod: 48 GB+ GPU"},
    {"model": "deepseek-r1:70b", "params": 70, "note": "RunPod: reasoning, slow per answer"},
]


def settings() -> dict:
    s = {"projects_dir": str(HERE.parent.parent)}
    if SETTINGS.exists():
        s.update(json.loads(SETTINGS.read_text()))
    return s


def save_settings(s: dict) -> None:
    runctl.RUNS.mkdir(exist_ok=True)
    SETTINGS.write_text(json.dumps(s, indent=2) + "\n")


def projects() -> list[dict]:
    root = Path(os.path.expanduser(settings()["projects_dir"]))
    out = []
    if not root.is_dir():
        return out
    for d in sorted(root.iterdir()):
        if (d / "ROADMAP.md").exists() and (d / ".sovereign_config.json").exists():
            plan = runctl.load_plan(str(d))
            st = runctl.status(plan)
            out.append({"name": d.name, "path": str(d), "done": st["done"], "total": st["total"],
                        "running": st["running"], "has_plan": runctl.plan_path(str(d)).exists()})
    return out


def local_models() -> list[str]:
    from sov.config import OLLAMA_URL
    try:
        return runpod.models_at(OLLAMA_URL)
    except Exception:
        return []


def set_env_key(name: str, value: str) -> None:
    """Write NAME=value into sovereign_agent/.env (gitignored), mode 600."""
    env = HERE / ".env"
    lines = env.read_text().splitlines() if env.exists() else []
    lines = [l for l in lines if not l.startswith(f"{name}=")]
    if value:
        lines.append(f"{name}={value}")
    env.write_text("\n".join(lines) + "\n")
    os.chmod(env, 0o600)
    os.environ.pop(name, None)
    if value:
        os.environ[name] = value


def tail(path: Path, n: int = 200) -> str:
    if not path.exists():
        return ""
    lines = path.read_text(errors="ignore").splitlines()[-n:]
    return re.sub(r"\x1b\[[0-9;]*m", "", "\n".join(lines))


def newest_cycle_log(project: str) -> Path | None:
    sl = Path(project, "logs", "sweep.log")
    if not sl.exists():
        return None
    rd = None
    for line in sl.read_text(errors="ignore").splitlines():
        m = re.search(r"results=(\S+)", line)
        if m:
            rd = m.group(1)
    if not rd or not os.path.isdir(rd):
        return None
    logs = sorted(Path(rd).glob("cycle*.log"), key=lambda p: p.stat().st_mtime)
    return logs[-1] if logs else None


class Handler(BaseHTTPRequestHandler):
    server_version = "XanaduConsole/1"

    def log_message(self, fmt, *args):  # keep the terminal quiet
        pass

    # safety ------------------------------------------------------------------
    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").split(":")[0]
        return host in ("127.0.0.1", "localhost")

    def _token_ok(self) -> bool:
        return secrets.compare_digest(self.headers.get("X-Xanadu-Token", ""), TOKEN)

    def _send(self, code: int, body, ctype="application/json") -> None:
        data = body if isinstance(body, bytes) else (
            json.dumps(body).encode() if ctype == "application/json" else body.encode())
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; style-src 'self' 'unsafe-inline'; "
                         "script-src 'self' 'unsafe-inline'; connect-src 'self'")
        self.end_headers()
        self.wfile.write(data)

    def _json_body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    # routes ------------------------------------------------------------------
    def do_GET(self):
        if not self._host_ok():
            return self._send(HTTPStatus.FORBIDDEN, {"error": "local access only"})
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if u.path == "/":
                html = PAGE.read_text().replace("__TOKEN__", TOKEN)
                return self._send(200, html, "text/html")
            if u.path.startswith("/api/") and not self._token_ok():
                return self._send(HTTPStatus.FORBIDDEN, {"error": "missing console token"})
            if u.path == "/api/projects":
                return self._send(200, {"projects": projects(), "settings": settings()})
            if u.path == "/api/plan":
                plan = runctl.load_plan(q["project"])
                return self._send(200, {"plan": plan, "errors": runctl.validate(plan),
                                        "suggestions": MODEL_SUGGESTIONS,
                                        "gpus": runpod.GPU_CHOICES})
            if u.path == "/api/status":
                return self._send(200, runctl.status(runctl.load_plan(q["project"])))
            if u.path == "/api/models":
                if q.get("url"):
                    try:
                        return self._send(200, {"models": runpod.models_at(q["url"].rstrip("/"))})
                    except Exception as exc:
                        return self._send(200, {"models": [], "error": str(exc)[:200]})
                return self._send(200, {"models": local_models()})
            if u.path == "/api/runpod":
                info = {"has_key": bool(runpod.api_key()), "pods": [], "error": None}
                if info["has_key"]:
                    try:
                        info["pods"] = [{"id": p["id"], "name": p.get("name"),
                                         "status": p.get("desiredStatus"),
                                         "costPerHr": p.get("costPerHr"),
                                         "endpoint": runpod.endpoint(p["id"])}
                                        for p in runpod.list_pods()]
                    except Exception as exc:
                        info["error"] = str(exc)[:300]
                return self._send(200, info)
            if u.path == "/api/logs":
                project, which = q["project"], q.get("which", "monitor")
                path = {"monitor": Path(project, "logs", "monitor.log"),
                        "sweep": Path(project, "logs", "sweep.log"),
                        "pods": Path(project, "logs", "podguard.log")}.get(which)
                if which == "worker":
                    path = newest_cycle_log(project)
                return self._send(200, {"text": tail(path) if path else ""})
            return self._send(404, {"error": "not found"})
        except Exception as exc:
            traceback.print_exc()
            return self._send(500, {"error": str(exc)[:300]})

    def do_POST(self):
        if not self._host_ok() or not self._token_ok():
            return self._send(HTTPStatus.FORBIDDEN, {"error": "local console token required"})
        u = urlparse(self.path)
        try:
            body = self._json_body()
            if u.path == "/api/plan":
                plan = runctl.load_plan(body["project"])
                for k, v in body["plan"].items():
                    if k == "project":
                        continue
                    if isinstance(v, dict) and isinstance(plan.get(k), dict):
                        plan[k].update(v)
                    else:
                        plan[k] = v
                errs = runctl.save_plan(plan)
                return self._send(200 if not errs else 400, {"errors": errs})
            if u.path == "/api/launch":
                return self._send(200, runctl.launch(runctl.load_plan(body["project"]),
                                                     lambda m: None))
            if u.path == "/api/stop":
                return self._send(200, runctl.stop(runctl.load_plan(body["project"])))
            if u.path == "/api/settings":
                s = settings()
                if "projects_dir" in body:
                    s["projects_dir"] = body["projects_dir"]
                save_settings(s)
                return self._send(200, s)
            if u.path == "/api/runpod/key":
                set_env_key("RUNPOD_API_KEY", body.get("key", "").strip())
                return self._send(200, {"has_key": bool(runpod.api_key())})
            if u.path in ("/api/runpod/stop", "/api/runpod/start", "/api/runpod/terminate"):
                fn = {"stop": runpod.stop_pod, "start": runpod.start_pod,
                      "terminate": runpod.terminate_pod}[u.path.rsplit("/", 1)[1]]
                fn(body["pod_id"])
                return self._send(200, {"ok": True})
            return self._send(404, {"error": "not found"})
        except runpod.RunPodError as exc:
            return self._send(400, {"error": str(exc)})
        except Exception as exc:
            traceback.print_exc()
            return self._send(500, {"error": str(exc)[:300]})


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--projects", help="folder containing projects (remembered)")
    a = ap.parse_args()
    if a.projects:
        s = settings()
        s["projects_dir"] = os.path.abspath(os.path.expanduser(a.projects))
        save_settings(s)
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    print(f"Xanadu console: http://127.0.0.1:{a.port}  (Ctrl-C to stop)", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
