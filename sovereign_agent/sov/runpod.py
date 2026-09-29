"""RunPod pods for Xanadu: create, start, stop, terminate, and track spend.

REST API v1 (https://rest.runpod.io/v1, Bearer token). The key comes from
RUNPOD_API_KEY in sovereign_agent/.env (the console writes it there).

Guardrails, because this module can spend money:
  • it only ever touches pods whose name starts with PREFIX ("xanadu-") —
    never anything else on the account;
  • every start and stop is written to a ledger, so spend per run can be
    capped (see spend_since / sov.runctl's pod guard);
  • stop keeps the volume (models stay downloaded, a few cents a day);
    terminate deletes it.

Ollama in a pod: image ollama/ollama, port 11434 over RunPod's HTTPS proxy
(https://<pod-id>-11434.proxy.runpod.net). The proxy closes a connection
that sends nothing for 100 s; streamed replies keep it alive.
"""
import json
import os
import time
from datetime import datetime, timezone

import requests

API = "https://rest.runpod.io/v1"
PREFIX = "xanadu-"
OLLAMA_IMAGE = "ollama/ollama:latest"
_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LEDGER = os.path.join(_HERE, "logs", "runpod_ledger.jsonl")

# Common choices for the console; any RunPod GPU type id is accepted.
GPU_CHOICES = [
    ("NVIDIA RTX A6000", "48 GB · models up to ~70B at 4-bit"),
    ("NVIDIA L40S", "48 GB · faster than A6000"),
    ("NVIDIA A100 80GB PCIe", "80 GB · ~70B comfortably"),
    ("NVIDIA H100 80GB HBM3", "80 GB · fastest single GPU"),
    ("NVIDIA H200", "141 GB · large MoE models"),
]


class RunPodError(RuntimeError):
    pass


def api_key() -> str:
    key = os.getenv("RUNPOD_API_KEY", "")
    if not key:
        env = os.path.join(_HERE, ".env")
        if os.path.exists(env):
            for line in open(env):
                if line.startswith("RUNPOD_API_KEY="):
                    key = line.split("=", 1)[1].strip().strip('"')
    return key


def _req(method: str, path: str, **kw) -> requests.Response:
    key = api_key()
    if not key:
        raise RunPodError("no RunPod API key — add it in the console (Settings)")
    r = requests.request(method, f"{API}{path}", timeout=30,
                         headers={"Authorization": f"Bearer {key}"}, **kw)
    if r.status_code >= 400:
        raise RunPodError(f"RunPod {method} {path}: HTTP {r.status_code} {r.text[:200]}")
    return r


def _ours(name: str | None) -> bool:
    return bool(name) and name.startswith(PREFIX)


def list_pods() -> list[dict]:
    pods = _req("GET", "/pods").json()
    return [p for p in pods if _ours(p.get("name"))]


def get_pod(pod_id: str) -> dict:
    p = _req("GET", f"/pods/{pod_id}").json()
    if not _ours(p.get("name")):
        raise RunPodError(f"pod {pod_id} is not a Xanadu pod — refusing to touch it")
    return p


def endpoint(pod_id: str) -> str:
    return f"https://{pod_id}-11434.proxy.runpod.net"


def _ledger(event: str, pod: dict) -> None:
    os.makedirs(os.path.dirname(LEDGER), exist_ok=True)
    with open(LEDGER, "a") as f:
        f.write(json.dumps({"at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                            "event": event, "pod": pod.get("id"), "name": pod.get("name"),
                            "costPerHr": pod.get("costPerHr")}) + "\n")


def create_pod(name: str, gpu_type: str, gpu_count: int = 1, volume_gb: int = 200,
               cloud: str = "SECURE") -> dict:
    body = {
        "name": name if _ours(name) else PREFIX + name,
        "imageName": OLLAMA_IMAGE,
        "gpuTypeIds": [gpu_type],
        "gpuCount": gpu_count,
        "containerDiskInGb": 20,
        "volumeInGb": volume_gb,
        "volumeMountPath": "/workspace",
        "ports": ["11434/http"],
        "cloudType": cloud,
        "env": {"OLLAMA_HOST": "0.0.0.0:11434", "OLLAMA_MODELS": "/workspace/ollama",
                "OLLAMA_KEEP_ALIVE": "30m"},
    }
    pod = _req("POST", "/pods", json=body).json()
    _ledger("create", pod)
    return pod


def start_pod(pod_id: str) -> dict:
    pod = get_pod(pod_id)
    _req("POST", f"/pods/{pod_id}/start")
    _ledger("start", pod)
    return pod


def stop_pod(pod_id: str) -> dict:
    pod = get_pod(pod_id)
    _req("POST", f"/pods/{pod_id}/stop")
    _ledger("stop", pod)
    return pod


def terminate_pod(pod_id: str) -> dict:
    pod = get_pod(pod_id)
    _req("DELETE", f"/pods/{pod_id}")
    _ledger("terminate", pod)
    return pod


def spend_since(since_iso: str, ledger_lines: list[str] | None = None,
                now: datetime | None = None) -> float:
    """Dollars spent since `since_iso`, from the ledger: each start→stop (or
    start→now while running) interval × that pod's costPerHr."""
    lines = ledger_lines if ledger_lines is not None else (
        open(LEDGER).read().splitlines() if os.path.exists(LEDGER) else [])
    since = datetime.fromisoformat(since_iso)
    now = now or datetime.now(timezone.utc)
    running: dict[str, tuple[datetime, float]] = {}
    total = 0.0
    for line in lines:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        at = datetime.fromisoformat(e["at"])
        pid, rate = e.get("pod"), float(e.get("costPerHr") or 0)
        if e["event"] in ("create", "start"):
            running[pid] = (max(at, since), rate)
        elif e["event"] in ("stop", "terminate") and pid in running:
            start, r = running.pop(pid)
            if at > start:
                total += (at - start).total_seconds() / 3600 * r
    for start, r in running.values():
        if now > start:
            total += (now - start).total_seconds() / 3600 * r
    return round(total, 2)


def wait_ready(url: str, timeout_s: int = 900) -> bool:
    """Poll the pod's Ollama until it answers (image pull + boot take minutes)."""
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        try:
            if requests.get(f"{url}/api/tags", timeout=15).status_code == 200:
                return True
        except requests.RequestException:
            pass
        time.sleep(10)
    return False


def pull_model(url: str, model: str, progress=None) -> None:
    """Download a model into the pod's volume (skipped by Ollama if present)."""
    with requests.post(f"{url}/api/pull", json={"model": model, "stream": True},
                       stream=True, timeout=(30, 300)) as r:
        r.raise_for_status()
        for raw in r.iter_lines():
            if not raw:
                continue
            msg = json.loads(raw)
            if "error" in msg:
                raise RunPodError(f"pull {model}: {msg['error']}")
            if progress:
                progress(msg)


def models_at(url: str) -> list[str]:
    r = requests.get(f"{url}/api/tags", timeout=15)
    r.raise_for_status()
    return [m["name"] for m in r.json().get("models", [])]
