"""
qwen_advisor.py — Fast local error classifier and rule drafter.

Uses ADVISOR_MODEL (via Ollama) to:
  1. Classify flutter analyze errors by category
  2. Generate targeted, actionable hints for the next attempt
  3. Draft new .roorules entries for unknown error patterns
  4. Log everything to logs/errors.jsonl and logs/rule_drafts.jsonl

ADVISOR_MODEL defaults to the same qwen2.5-coder:7b-instruct-q4_K_M weights as
TIER1_MODEL — not the smaller ~4B model this file used to describe. That's a
deliberate choice, not just a fallback: reusing Tier 1's weights means an
advisor call during a Tier 1 retry costs nothing extra, since the model is
already resident in VRAM. The catch is that any advisor call during a
Tier 2-4 retry (gemma4:26b / qwen3.6:35b / qwen2.5-coder:32b) would evict that
much larger model from GPU and force a ~20-30s reload afterward — so as of
2026-07-10, work.py only calls the advisor for Tier 1 attempts (tier_idx == 0)
and skips it entirely for Tier 2-4, where the eviction cost isn't worth an
enriched hint.

This runs after autofix.py has handled mechanical issues, so it only sees
genuinely novel errors that require semantic understanding.

Learning loop:
  - Every error is logged to errors.jsonl
  - Novel patterns get a draft rule written to rule_drafts.jsonl
  - promote_rules.py (run periodically) auto-promotes rules with 3+ occurrences
    to .roorules, closing the feedback loop without human involvement
"""

import os
import re
import json
import requests
from datetime import date

OLLAMA_URL    = os.getenv("LOCAL_MODEL_URL", "http://localhost:11434")
ADVISOR_MODEL = os.getenv("ADVISOR_MODEL",  "qwen2.5-coder:7b-instruct-q4_K_M")

ERRORS_LOG      = "logs/errors.jsonl"
RULE_DRAFTS_LOG = "logs/rule_drafts.jsonl"


# ── Ollama call ────────────────────────────────────────────────────────────────

def _call_qwen(prompt: str, timeout: int = 180) -> str:
    try:
        resp = requests.post(
            f"{OLLAMA_URL}/api/chat",
            json={
                "model": ADVISOR_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "stream": False,
                "options": {"temperature": 0.1, "num_predict": 800},
            },
            timeout=timeout,
        )
        resp.raise_for_status()
        return resp.json()["message"]["content"]
    except Exception as e:
        return f'{{"error": "{e}", "enriched_hint": "", "new_rule": null}}'


# ── Main advisor ───────────────────────────────────────────────────────────────

def _find_class_definition(class_name: str, project_root: str) -> str | None:
    """
    Search lib/ for a file that defines `class/enum/mixin <ClassName>`.
    Returns a package import string or None.
    """
    import pathlib
    lib_root = pathlib.Path(project_root) / "lib"
    pattern = re.compile(rf'\b(?:class|enum|mixin)\s+{re.escape(class_name)}\b')
    pkg_name = "astro_flux"
    pubspec = pathlib.Path(project_root) / "pubspec.yaml"
    if pubspec.exists():
        for line in pubspec.read_text().splitlines():
            if line.startswith("name:"):
                pkg_name = line.split(":", 1)[1].strip()
                break
    for dart_file in lib_root.rglob("*.dart"):
        try:
            if pattern.search(dart_file.read_text()):
                rel = dart_file.relative_to(lib_root)
                return f"package:{pkg_name}/{rel.as_posix()}"
        except Exception:
            pass
    return None


def undefined_class_hint(analyze_output: str, project_root: str) -> str:
    """
    Deterministic hint for undefined_class / undefined_identifier errors.
    Finds the actual file defining the missing type — bypasses qwen entirely.
    Returns a precise import directive, or empty string if not applicable.
    """
    names = list(dict.fromkeys(re.findall(
        r"(?:class|name|type|identifier) '(\w+)' "
        r"(?:isn't defined|isn't a type|can't be used as a type|doesn't exist)",
        analyze_output,
    )))
    if not names:
        return ""
    lines = []
    for name in names[:4]:
        loc = _find_class_definition(name, project_root)
        if loc:
            lines.append(
                f"  '{name}' is defined in {loc} — add "
                f"`import '{loc}';` to the file's import block. "
                f"DO NOT define a new '{name}' class or enum."
            )
        else:
            lines.append(
                f"  '{name}' is not defined anywhere in lib/. "
                f"Check spelling — do not invent a new class."
            )
    if not lines:
        return ""
    return (
        "IMPORT ERROR (deterministic): These types are undefined because their "
        "import is missing. DO NOT rewrite or create these classes — only add "
        "the correct import line at the top of the file:\n" + "\n".join(lines)
    )


def advise(analyze_output: str, task: str, project_root: str, attempt: int = 1) -> dict:
    """
    Ask qwen to classify errors and produce enriched guidance for the executor.

    Returns a dict:
      enriched_hint : str   — targeted correction to prepend to next attempt prompt
      new_rule      : str|None — suggested .roorules addition (None if nothing novel)
      categories    : list  — error categories found
      auto_fixable  : bool  — whether qwen thinks these are mechanically fixable
    """
    # ── Deterministic pre-check: undefined_class is almost always a missing import ──
    det_hint = undefined_class_hint(analyze_output, project_root)

    rules_path = os.path.join(project_root, ".roorules")
    rules_excerpt = ""
    if os.path.exists(rules_path):
        full = open(rules_path).read()
        for section in ["## Dart/Flame API", "## flame_audio", "## Lint rules"]:
            idx = full.find(section)
            if idx >= 0:
                rules_excerpt += full[idx:idx + 600] + "\n\n"

    # Escalate urgency when the model is looping on the same error
    escalation = ""
    if attempt >= 3:
        escalation = (
            f"\n\nWARNING: This is attempt {attempt}. The coder has failed "
            f"{attempt - 1} times with the same error. Previous hints were "
            f"ignored. Your hint MUST be more explicit: name the exact file, "
            f"the exact line to add or change, and forbid the wrong approach "
            f"by name (e.g. 'do NOT define a new Foo class')."
        )

    prompt = f"""You are a Dart/Flutter expert helping a coding pipeline fix compile errors fast.

TASK being implemented: {task}

FLUTTER ANALYZE OUTPUT:
{analyze_output[:2000]}

RELEVANT .roorules:
{rules_excerpt[:1200]}
{escalation}
Classify each error and respond with ONLY valid JSON (no markdown, no explanation):
{{
  "categories": ["hallucinated_api", "missing_import", "const_issue", "type_mismatch", "api_drift", "other"],
  "enriched_hint": "Write a short, specific, actionable correction. Name the exact wrong class/method and the correct fix. If the error is undefined_class, say explicitly which import to add and that the model must NOT define a new class. 2-4 sentences max.",
  "new_rule": "If this error reveals a gap in .roorules write the entry here as plain text. null if already covered.",
  "auto_fixable": false
}}"""

    raw = _call_qwen(prompt)
    start, end = raw.find("{"), raw.rfind("}") + 1
    if start < 0:
        result = {"enriched_hint": "", "new_rule": None, "categories": [], "auto_fixable": False}
    else:
        try:
            result = json.loads(raw[start:end])
        except Exception:
            result = {"enriched_hint": raw[start:end][:400], "new_rule": None,
                      "categories": [], "auto_fixable": False}

    # Prepend the deterministic import hint — it's more reliable than qwen's guess
    if det_hint:
        # Guard: LLM occasionally returns enriched_hint as a list instead of str
        existing = result.get("enriched_hint", "")
        if not isinstance(existing, str):
            existing = " ".join(str(x) for x in existing) if isinstance(existing, list) else str(existing)
        result["enriched_hint"] = det_hint + ("\n\n" + existing if existing else "")
        if "missing_import" not in result.get("categories", []):
            result.setdefault("categories", []).append("missing_import")

    return result


# ── Logging ────────────────────────────────────────────────────────────────────

def log_error_pattern(
    analyze_output: str,
    task: str,
    attempt: int,
    task_idx: int,
    project_root: str,
    categories: list[str] | None = None,
):
    """Append an error record to logs/errors.jsonl."""
    path = os.path.join(project_root, ERRORS_LOG)
    os.makedirs(os.path.dirname(path), exist_ok=True)

    # Extract error codes from analyze output
    codes = list(dict.fromkeys(
        re.findall(r'•\s+(\w+)\s*$', analyze_output, re.MULTILINE)
    ))[:10]

    # Extract file names mentioned
    files = list(dict.fromkeys(
        re.findall(r'(\S+\.dart):\d+:\d+', analyze_output)
    ))[:5]

    record = {
        "date":       date.today().isoformat(),
        "task_idx":   task_idx,
        "task":       task[:100],
        "attempt":    attempt,
        "error_codes": codes,
        "files":      files,
        "categories": categories or [],
        "snippet":    analyze_output[:400],
    }
    with open(path, "a") as f:
        f.write(json.dumps(record) + "\n")


def log_rule_draft(rule: str, source_task: int, project_root: str):
    """Append a candidate .roorules entry to logs/rule_drafts.jsonl.

    `rule` arrives from model-generated JSON, so its type is whatever the model
    felt like emitting. Coerce rather than trust: an uncaught AttributeError
    here kills the whole advisor call, and the advisor is the ONLY feedback the
    next attempt receives. The task then retries blind and escalates a tier over
    a type mismatch in a logging helper.
    """
    if isinstance(rule, (list, tuple)):
        rule = "; ".join(str(r).strip() for r in rule if str(r).strip())
    elif not isinstance(rule, str):
        rule = "" if rule is None else str(rule)
    rule = rule.strip()
    if not rule or rule.lower() in {"null", "none"}:
        return
    path = os.path.join(project_root, RULE_DRAFTS_LOG)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    record = {
        "date":        date.today().isoformat(),
        "source_task": source_task,
        "rule":        rule.strip(),
        "occurrences": 1,
        "promoted":    False,
    }
    with open(path, "a") as f:
        f.write(json.dumps(record) + "\n")


# ── Pattern frequency query ────────────────────────────────────────────────────

def top_error_patterns(project_root: str, n: int = 10) -> list[dict]:
    """
    Read errors.jsonl and return the N most frequent error codes.
    Useful for promote_rules.py and debugging sessions.
    """
    path = os.path.join(project_root, ERRORS_LOG)
    if not os.path.exists(path):
        return []
    freq: dict[str, int] = {}
    for line in open(path):
        try:
            rec = json.loads(line)
            for code in rec.get("error_codes", []):
                freq[code] = freq.get(code, 0) + 1
        except Exception:
            pass
    return sorted([{"code": k, "count": v} for k, v in freq.items()],
                  key=lambda x: -x["count"])[:n]
