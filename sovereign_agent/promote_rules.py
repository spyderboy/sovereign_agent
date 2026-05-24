"""
promote_rules.py — Periodic rule promoter for the sovereign_agent learning loop.

Reads logs/rule_drafts.jsonl, deduplicates by semantic similarity (via qwen),
and promotes rules that have appeared 3+ times (or that qwen rates as high-confidence)
into .roorules under a ## Learned Rules section.

Usage:
    python promote_rules.py --project ~/Code/astro_flux
    python promote_rules.py --project ~/Code/astro_flux --threshold 2 --dry-run

Run this after a batch of tasks, or periodically from a cron/supervisor hook.
The promoted rules are what turns one-off fixes into permanent guardrails —
this is the "training" step in the learning loop.
"""

import os
import re
import json
import argparse
import requests
from datetime import date

OLLAMA_URL    = os.getenv("LOCAL_MODEL_URL", "http://localhost:11434")
ADVISOR_MODEL = os.getenv("ADVISOR_MODEL",  "qwen2.5-coder:7b-instruct-q4_K_M")

RULE_DRAFTS_LOG = "logs/rule_drafts.jsonl"
ERRORS_LOG      = "logs/errors.jsonl"
ROORULES        = ".roorules"
LEARNED_HEADER  = "## Learned Rules"


def load_drafts(project_root: str) -> list[dict]:
    path = os.path.join(project_root, RULE_DRAFTS_LOG)
    if not os.path.exists(path):
        return []
    drafts = []
    for line in open(path):
        try:
            drafts.append(json.loads(line))
        except Exception:
            pass
    return drafts


def load_error_freq(project_root: str) -> dict[str, int]:
    """Count how often each error code has appeared."""
    path = os.path.join(project_root, ERRORS_LOG)
    freq: dict[str, int] = {}
    if not os.path.exists(path):
        return freq
    for line in open(path):
        try:
            rec = json.loads(line)
            for code in rec.get("error_codes", []):
                freq[code] = freq.get(code, 0) + 1
        except Exception:
            pass
    return freq


def deduplicate_and_merge(drafts: list[dict], threshold: int) -> list[str]:
    """
    Group similar draft rules and return those that hit the threshold.
    Uses simple text-overlap grouping — no LLM needed for dedup.
    """
    # Count occurrences of near-identical rules (first 60 chars as key)
    groups: dict[str, list[str]] = {}
    for d in drafts:
        if d.get("promoted"):
            continue
        rule = d.get("rule", "").strip()
        if not rule or rule == "null":
            continue
        key = rule[:60].lower()
        groups.setdefault(key, []).append(rule)

    # Return rules that appear >= threshold times
    return [rules[0] for rules in groups.values() if len(rules) >= threshold]


def qwen_review_rules(rules: list[str], existing_roorules: str) -> list[str]:
    """
    Ask qwen to filter and polish the candidate rules before promotion.
    Returns the cleaned list.
    """
    if not rules:
        return []

    prompt = f"""You are reviewing candidate additions to a Dart/Flutter coding rules file (.roorules).

Existing .roorules (excerpt):
{existing_roorules[:1500]}

Candidate rules to review:
{json.dumps(rules, indent=2)}

For each candidate rule:
1. Is it already covered by the existing .roorules? (skip if yes)
2. Is it accurate and actionable? (keep if yes)
3. Polish the wording to match the style of existing rules (imperative, concise).

Return ONLY a JSON array of the rules to add (polished wording). Empty array if none qualify.
No markdown, no explanation."""

    try:
        resp = requests.post(
            f"{OLLAMA_URL}/api/chat",
            json={
                "model": ADVISOR_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "stream": False,
                "options": {"temperature": 0.1, "num_predict": 1000},
            },
            timeout=60,
        )
        resp.raise_for_status()
        raw = resp.json()["message"]["content"]
        start, end = raw.find("["), raw.rfind("]") + 1
        if start >= 0:
            return json.loads(raw[start:end])
    except Exception as e:
        print(f"  qwen review failed: {e} — using unreviewed rules")
    return rules


def append_to_roorules(rules: list[str], project_root: str, dry_run: bool) -> int:
    """Append promoted rules to .roorules under the Learned Rules section."""
    if not rules:
        return 0
    path = os.path.join(project_root, ROORULES)
    content = open(path).read() if os.path.exists(path) else ""

    # Find or create the Learned Rules section
    if LEARNED_HEADER not in content:
        content += f"\n\n{LEARNED_HEADER}\n"
        content += "Rules auto-promoted from observed error patterns. "
        content += "Review periodically and graduate to permanent sections above.\n"

    today = date.today().isoformat()
    additions = [f"- [{today}] {rule}" for rule in rules]
    content = content.rstrip() + "\n" + "\n".join(additions) + "\n"

    if dry_run:
        print("\n  [dry-run] Would add to .roorules:")
        for a in additions:
            print(f"    {a}")
        return len(additions)

    with open(path, "w") as f:
        f.write(content)
    return len(additions)


def mark_promoted(drafts: list[dict], promoted_rules: list[str], project_root: str):
    """Rewrite rule_drafts.jsonl marking promoted entries."""
    path = os.path.join(project_root, RULE_DRAFTS_LOG)
    promoted_set = {r[:60].lower() for r in promoted_rules}
    updated = []
    for d in drafts:
        if d.get("rule", "")[:60].lower() in promoted_set:
            d["promoted"] = True
        updated.append(d)
    with open(path, "w") as f:
        for d in updated:
            f.write(json.dumps(d) + "\n")


def print_error_summary(project_root: str):
    freq = load_error_freq(project_root)
    if not freq:
        return
    print("\n  Top error codes across all tasks:")
    for code, count in sorted(freq.items(), key=lambda x: -x[1])[:10]:
        print(f"    {count:3d}×  {code}")


def main():
    parser = argparse.ArgumentParser(description="Promote learned rules into .roorules")
    parser.add_argument("--project",   required=True, help="Path to project root")
    parser.add_argument("--threshold", type=int, default=3,
                        help="Min occurrences before a rule is promoted (default: 3)")
    parser.add_argument("--dry-run",   action="store_true",
                        help="Preview without writing")
    parser.add_argument("--summary",   action="store_true",
                        help="Print error frequency summary and exit")
    args = parser.parse_args()

    project_root = os.path.abspath(args.project)
    os.chdir(project_root)

    if args.summary:
        print_error_summary(project_root)
        return

    print(f"\n  Scanning rule drafts (threshold: {args.threshold} occurrences)...")
    drafts = load_drafts(project_root)
    print(f"  Found {len(drafts)} total draft entries")

    candidates = deduplicate_and_merge(drafts, args.threshold)
    print(f"  {len(candidates)} candidate rule(s) hit threshold")

    if not candidates:
        print("  Nothing to promote.")
        print_error_summary(project_root)
        return

    existing = open(os.path.join(project_root, ROORULES)).read() if os.path.exists(
        os.path.join(project_root, ROORULES)) else ""

    print(f"  Asking qwen to review and polish {len(candidates)} candidate(s)...")
    reviewed = qwen_review_rules(candidates, existing)
    print(f"  {len(reviewed)} rule(s) approved by qwen")

    promoted = append_to_roorules(reviewed, project_root, args.dry_run)
    if promoted and not args.dry_run:
        mark_promoted(drafts, reviewed, project_root)
        print(f"  ✓ {promoted} rule(s) promoted to .roorules")

    print_error_summary(project_root)


if __name__ == "__main__":
    main()
