"""Ollama client layer: installed-model preflight, model-size heuristics, the
cross-worker large-model lock, the streaming chat call, and JSON extraction
from model output.

Extracted from work.py (2026-09-28); behaviour unchanged.
"""
from requests.exceptions import ReadTimeout
import contextlib
import fcntl
import json
import os
import re
import requests
import sys
import time
from sov.config import (
    OLLAMA_THINK,
    MODEL_CTX,
    OLLAMA_URL,
)
from sov.ui import DIM, RED, RESET
from sov.watchdog import heartbeat


class PromptTooLargeError(Exception):
    """Raised when Ollama reports the prompt was truncated to fit the context window.

    Signals that the caller should either trim the context (fewer files) or
    queue the task for a run with a larger context window (--deep pass).
    """
    def __init__(self, token_count: int, limit: int):
        self.token_count = token_count
        self.limit = limit
        super().__init__(
            f"Prompt truncated: {token_count} tokens used {token_count/limit*100:.0f}% "
            f"of {limit}-token context window"
        )

# All unchecked tasks are worked through in one run — no daily cap.
# Velocity (tasks completed / elapsed time) is logged and used by standup.py
# to project throughput. The standup measures what was done, not what to allow.


# ─── Ollama ───────────────────────────────────────────────────────────────────

def _ollama_installed_tags() -> list[str]:
    """Return the list of model tags Ollama currently has, or raise on failure.

    Raised exceptions are handled by preflight_models() so it can tell an
    Ollama-not-running error apart from a merely-missing model.
    """
    resp = requests.get(f"{OLLAMA_URL}/api/tags", timeout=10)
    resp.raise_for_status()
    return [m["name"] for m in resp.json().get("models", [])]


def preflight_models(required: list[str]) -> None:
    """Verify Ollama is up and every required model tag is already pulled.

    Fails fast with actionable guidance instead of silently streaming a
    multi-GB pull mid-setup or dying with an opaque connection error:
      • Ollama unreachable  → tell the user to start it (`ollama serve`).
      • Model tag missing    → print the exact `ollama pull <tag>` command.
    Auto-pulling was removed 2026-07-17: an unattended 18GB download at run
    start was surprising, and a missing tag is nearly always a typo'd model
    name (e.g. qwen3.6:35b vs qwen3-coder:30b) that a pull would only mask.
    """
    try:
        installed = _ollama_installed_tags()
    except Exception as e:
        print(f"  {RED}✗ Ollama is not reachable at {OLLAMA_URL}: {e}{RESET}")
        print(f"    Start it first:  ollama serve")
        sys.exit(1)

    def _present(tag: str) -> bool:
        return tag in installed or f"{tag}:latest" in installed

    missing = [m for m in dict.fromkeys(required) if not _present(m)]
    if missing:
        print(f"  {RED}✗ Required model(s) not pulled in Ollama:{RESET}")
        for m in missing:
            print(f"      ollama pull {m}")
        print(f"    Installed tags: {', '.join(installed) or '(none)'}")
        print(f"    (Tag must match `ollama list` exactly.)")
        sys.exit(1)


_LARGE_MODEL_THRESHOLD_GB = 15  # flush before loading anything this large

# Rough VRAM footprint by model name fragment (GB).
# Used to decide whether to flush before loading a tier3/4 model.
_MODEL_SIZE_HINTS: dict[str, float] = {
    "32b":   19.0,
    "30b":   18.0,
    "r1":    19.0,
    "35b":   20.0,  # qwen3.6:35b-a3b — MoE, full model ~20GB in VRAM
    "27b":   17.0,  # qwen3.8:27b — dense, ~17GB resident (GGUF Q4_K_M)
    "26b":   15.0,  # gemma4:26b       — MoE, full model ~15GB in VRAM
    "24b":   14.0,
    "20b":   13.0,
    "16b":    8.9,
    "14b":    9.0,
    "7b":     4.7,
    "4b":     4.0,
}

def _model_size_gb(model_name: str) -> float:
    """Estimate model VRAM footprint from its name.

    Fragments are matched with a digit boundary immediately before them, so
    "7b" doesn't match inside "27b"/"47b" and "4b" doesn't match inside
    "24b"/"14b" — plain substring containment silently misclassified
    qwen3.8:27b (17GB) as a 7b (4.7GB) model before this fix, which meant it
    never tripped the large-model VRAM lock or the longer coding timeout.
    """
    name = model_name.lower()
    for fragment, gb in _MODEL_SIZE_HINTS.items():
        if re.search(r"(?<!\d)" + re.escape(fragment), name):
            return gb
    return 10.0  # safe default if unknown


# 2026-07-11: cross-process lock so at most one worker at a time runs a
# tier2+ ("large") model. Parallel quick-sweep workers each escalate through
# tiers independently and were never coordinated with each other — only the
# --deep pass (which is exclusively tier2+ retries) had a VRAM guard, and it
# worked by forcing --workers 1 for the whole pass. That's too blunt for the
# quick pass, where most tasks stay on tier1 and losing all parallelism there
# would be a big throughput hit. Instead, tier1 calls stay fully parallel and
# only the large-model calls serialize across workers — same effect as the
# deep-pass guard, scoped to just the calls that actually need it. This
# matters more now that tier2/3/4 context windows were all doubled to 32768
# (2026-07-11), since two of these loaded at once is the exact VRAM-thrash
# scenario the deep-pass guard exists to prevent.
_TIER2_LOCK_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".tier2_lock")


@contextlib.contextmanager
def _large_model_lock():
    with open(_TIER2_LOCK_PATH, "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def ollama(model: str, system: str, user: str, timeout: int = 300) -> str:
    # Flush VRAM before loading any large model to avoid swap on 32GB Mac.
    # Tier1/2 (7b/16b) stay resident together (~13GB combined) — no flush needed.
    # Tier3/4 (32b, r1:32b) need clear headroom — flush everything first.
    keep_alive = -1  # default: keep in memory indefinitely
    size_gb = _model_size_gb(model)
    if size_gb >= _LARGE_MODEL_THRESHOLD_GB:
        # Keep large models alive for 10 min between retries — avoids reloading
        # 19 GB from scratch on every attempt.  With --workers 1 (required for
        # deep pass), this is safe; with --workers 2 the two 32B models still
        # won't coexist (19 GB each), so deep must always use a single worker.
        print(f"  {DIM}Loading {size_gb:.0f}GB model ({model.split(':')[0]}) — keeping resident 30m...{RESET}")
        keep_alive = "30m"

    # ── Pre-flight prompt size check ──────────────────────────────────────────
    # Estimate token count before sending so we never pay for a full inference
    # round-trip on a prompt Ollama will just silently truncate.
    # Code averages ~3.5 chars/token for BPE tokenisers; use 3 to be conservative.
    ctx_limit = MODEL_CTX.get(model, int(os.getenv("OLLAMA_CONTEXT_LENGTH", "24576")))
    estimated_tokens = (len(system) + len(user)) // 3
    if estimated_tokens >= int(ctx_limit * 0.85):
        raise PromptTooLargeError(estimated_tokens, ctx_limit)

    # stream=True: Ollama writes tokens as they're generated, so the server's
    # 10-minute write-deadline never fires on long tasks.  We reassemble the
    # content from newline-delimited JSON chunks — callers see no difference.
    def _do_request() -> str:
        resp = requests.post(
            f"{OLLAMA_URL}/api/chat",
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user",   "content": user},
                ],
                "options": {"num_ctx": ctx_limit},
                "stream": True,
                "keep_alive": keep_alive,
                **({"think": OLLAMA_THINK} if OLLAMA_THINK is not None else {}),
            },
            stream=True,
            timeout=timeout,
        )
        resp.raise_for_status()
        content = []
        done_chunk: dict = {}
        # WALL-CLOCK DEADLINE on the whole stream.
        #
        # `requests`' timeout is per socket READ, not total. A model trickling
        # one token every few seconds resets it forever, so a 1800s timeout
        # never fires and a single attempt can run for hours. Two 45-minute
        # hangs on 2026-08-12 were exactly this: the 35B streaming at a crawl
        # under memory pressure, the read timeout never tripping, and the stall
        # watchdog eventually killing the process because no attempt ever
        # finished. Abort the attempt instead — the tier ladder can then do its
        # job.
        _deadline = time.time() + timeout
        for raw_line in resp.iter_lines():
            if time.time() > _deadline:
                resp.close()
                raise ReadTimeout(
                    f"generation exceeded {timeout}s of wall clock "
                    f"({len(''.join(content))} chars produced) — the model is "
                    f"streaming but far too slowly")
            if not raw_line:
                continue
            heartbeat(f"streaming from {model}")
            try:
                chunk = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            delta = chunk.get("message", {}).get("content", "")
            if delta:
                content.append(delta)
            if chunk.get("done"):
                done_chunk = chunk
                break

        # Post-flight confirmation: verify the model didn't truncate despite our
        # estimate (e.g. tokeniser is denser than expected for this content).
        prompt_tokens = done_chunk.get("prompt_eval_count", 0)
        if prompt_tokens and prompt_tokens >= int(ctx_limit * 0.90):
            raise PromptTooLargeError(prompt_tokens, ctx_limit)  # ctx_limit already model-specific

        return "".join(content)

    # Only large (tier2+) models serialize across workers — tier1 stays fully
    # parallel. See _large_model_lock() above for why.
    if size_gb >= _LARGE_MODEL_THRESHOLD_GB:
        with _large_model_lock():
            return _do_request()
    return _do_request()


# ─── JSON extraction helper ───────────────────────────────────────────────────

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*\n(.*?)```", re.DOTALL)


def _extract_json_candidate(raw: str) -> str:
    """Return the text to search for a JSON object in a raw model response.

    Prefers the LAST ```json fenced block when the response contains one or
    more. Two things break a naive "first { to last }" scan of the whole raw
    text, and both were observed back-to-back on the same task (qwen2.5-coder
    then qwen3.8): (1) explanatory prose before/around the fence routinely
    contains a stray brace, e.g. an inline "`{ heading: string }`" type
    description, which a naive scan latches onto as the start; (2) a model
    that thinks out loud and revises itself emits more than one JSON block,
    and the span from the first block's "{" to the last block's "}" crosses
    both blocks plus everything in between, which is not valid JSON at any
    length. Scoping to just the last fenced block sidesteps both. Falls back
    to the raw text unchanged when there is no fence, for models that emit
    bare JSON with no markdown at all -- unchanged from the prior behavior.
    """
    fences = _JSON_FENCE_RE.findall(raw)
    return fences[-1] if fences else raw


def _normalize_backtick_strings(text: str) -> str:
    """Convert JS/TS template-literal (backtick) values into properly
    escaped JSON string literals.

    Seen on both qwen2.5-coder and qwen3.8: despite an explicit "values must
    be JSON-escaped strings, no markdown" instruction, the model sometimes
    writes file content as a backtick-delimited template literal instead of
    a quoted JSON string. That is invalid JSON -- json.loads rejects the
    whole object outright even though the file content itself is fine --
    and the failure was silently swallowed as "no output" with the raw text
    only visible in task_traces.jsonl. \` is treated as an escaped literal
    backtick inside the span. Safe to apply as a fallback after strict
    parsing has already failed: valid JSON never contains a bare backtick.
    """
    def repl(m: re.Match) -> str:
        content = m.group(1).replace("\\`", "`")
        return json.dumps(content)
    return re.sub(r"`((?:\\.|[^`\\])*)`", repl, text, flags=re.DOTALL)


def _extract_first_json_object(text: str) -> dict:
    """Extract the first complete, balanced JSON object from text.

    Uses a brace-depth scan so trailing prose after the closing brace (or a
    second JSON block) doesn't cause 'Extra data' errors.  Returns {} on any
    parse failure.
    """
    text = _extract_json_candidate(text)
    start = text.find("{")
    if start == -1:
        return {}
    depth = 0
    in_str = False
    escape = False
    for i, ch in enumerate(text[start:], start):
        if escape:
            escape = False
            continue
        if ch == "\\" and in_str:
            escape = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start : i + 1])
                except json.JSONDecodeError:
                    try:
                        return json.loads(_normalize_backtick_strings(text[start : i + 1]))
                    except json.JSONDecodeError:
                        return {}
    return {}
