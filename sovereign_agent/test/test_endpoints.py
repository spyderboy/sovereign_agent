"""Per-tier endpoints: a tier's url routes its model's calls; nothing else moves."""
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]


def endpoints(env: dict, profile: str | None = None) -> list[str]:
    e = {k: v for k, v in os.environ.items() if not k.startswith("TIER")}
    e.update(env)
    if profile:
        e["SOVEREIGN_PROFILE"] = profile
    code = "import sov.config as c; print('|'.join(c.endpoint_for(m) for m in c.TIER_MODELS))"
    out = subprocess.run([sys.executable, "-W", "ignore", "-c", code], cwd=HERE, env=e,
                         capture_output=True, text=True, check=True).stdout
    return out.strip().splitlines()[-1].split("|")


def test_default_is_local():
    assert set(endpoints({"LOCAL_MODEL_URL": "http://localhost:11434"})) == {"http://localhost:11434"}


def test_tier_url_wins_over_local_even_with_env_local_url():
    urls = endpoints({"LOCAL_MODEL_URL": "http://localhost:11434",
                      "TIER1_MODEL": "a:1b", "TIER2_MODEL": "b:2b", "TIER3_MODEL": "b:2b",
                      "TIER4_MODEL": "b:2b", "TIER2_URL": "https://pod-11434.proxy.runpod.net/"})
    assert urls[0] == "http://localhost:11434"
    assert urls[1:] == ["https://pod-11434.proxy.runpod.net"] * 3      # trailing / stripped
