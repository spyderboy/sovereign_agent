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

    prompt = f"""You are reviewing candidate additions to a project's coding rules file (.roorules).
The project's language/framework is whatever the candidate rules themselves reference (e.g. Go,
Dart/Flutter, Python) — do not assume a specific language; judge each rule on its own terms.

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


# Grounding and foreign-vocabulary checks moved to prompt_artifacts.py
# (2026-07-25) so the advisor hint path and dream.py's drafted .roorules get
# the same gate. The 2026-07-14 post-mortem that motivated both checks is
# preserved in that module's docstring. Behaviour here is unchanged: a rule is
# rejected if it speaks another stack's dialect, or references identifiers
# found nowhere in the project, its stdlib, or its framework.
import prompt_artifacts


def append_to_roorules(rules: list[str], project_root: str, dry_run: bool) -> int:
    """Append promoted rules to .roorules under the Learned Rules section."""
    accepted, rejected = prompt_artifacts.partition(
        rules, project_root, kind="rule", mode="reject"
    )
    for rule, verdict in rejected:
        for reason in verdict.reasons:
            print(f"  ✗ REJECTED — {reason}: {rule[:90]}")
    rules = accepted
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


# ── ERROR_HINTS learning (2026-08-22) ───────────────────────────────────────
# Everything above this point only ever wrote free-text prose into .roorules'
# "## Learned Rules" section — genuinely useful (the model reads it as
# context) but never fires deterministically the way a hint_packs ERROR_HINTS
# entry does: an ERROR_HINTS regex is checked against the ACTUAL error output
# of a later attempt and, on match, prepends the hint automatically — no
# reliance on the model noticing a paragraph of prose.
#
# This closes that gap, but scoped conservatively:
#   - ERROR_HINTS only, never BAD_PATTERNS. An ERROR_HINTS entry is purely
#     additive (apply_error_hints() in work.py just prepends text) — a wrong
#     one is a mildly irrelevant hint. A BAD_PATTERNS entry actively BLOCKS a
#     file write; a wrong one silently rejects correct code forever, and
#     autofix.py's own _mechanical_rewrites docstring is explicit that the
#     bar for that kind of thing must be "correct in EVERY context, with no
#     judgement" — not something to auto-derive from 3 occurrences on one
#     project. BAD_PATTERNS stay human/Claude-curated only.
#   - Written into the CURRENT PROJECT's own .sovereign_config.json
#     (additional_error_hints), never into the shared hint_packs/ directory.
#     hint_packs/ is cross-project — dart_core vs galaxican already exists
#     specifically because a Galaxican-only trap was leaking into every Dart
#     project once, and one project's 3 occurrences is not evidence a pattern
#     is general. Graduating a project-local learned hint into a shared
#     <language>_core (or a <language>_learned pack — see hints.py's
#     _learned_pack_name) is a deliberate step, same as how typescript_core.py
#     itself was seeded: real project entries, promoted once actually general.
#   - Still gated by prompt_artifacts.verify_prompt_artifact — the same bar
#     applied to every other piece of model-generated text that reaches a
#     future prompt.
ADDITIONAL_ERROR_HINTS_KEY = "additional_error_hints"


def load_error_records(project_root: str) -> list[dict]:
    path = os.path.join(project_root, ERRORS_LOG)
    if not os.path.exists(path):
        return []
    records = []
    for line in open(path):
        try:
            records.append(json.loads(line))
        except Exception:
            pass
    return records


def _dominant_error_code(source_task, error_records: list[dict]) -> str | None:
    """The most common error_code logged for this task_idx, if any."""
    if source_task is None:
        return None
    from collections import Counter
    counts: Counter[str] = Counter()
    for rec in error_records:
        if rec.get("task_idx") == source_task:
            for code in rec.get("error_codes", []):
                counts[code] += 1
    if not counts:
        return None
    return counts.most_common(1)[0][0]


def candidate_error_hints(drafts: list[dict], error_records: list[dict],
                          threshold: int) -> list[tuple[str, str]]:
    """Group drafts by rule text (same grouping as deduplicate_and_merge),
    and for any group that (a) hits the threshold and (b) correlates to a
    single dominant error_code across its drafts' source tasks, propose an
    ERROR_HINTS entry: (regex-escaped error code, rule text).

    Returns [(pattern, hint), ...] — candidates only, not yet verified or
    written anywhere.
    """
    groups: dict[str, list[dict]] = {}
    for d in drafts:
        if d.get("promoted"):
            continue
        rule = d.get("rule", "").strip()
        if not rule or rule == "null":
            continue
        key = rule[:60].lower()
        groups.setdefault(key, []).append(d)

    candidates = []
    for group in groups.values():
        if len(group) < threshold:
            continue
        codes = [
            _dominant_error_code(d.get("source_task"), error_records)
            for d in group
        ]
        codes = [c for c in codes if c]
        if not codes:
            continue  # no linkable error code — .roorules prose only, no deterministic trigger
        from collections import Counter
        top_code, top_count = Counter(codes).most_common(1)[0]
        if top_count < threshold:
            continue  # the group hit threshold, but not consistently on the SAME code
        rule = group[0]["rule"].strip()
        candidates.append((re.escape(top_code), rule))
    return candidates


def append_to_project_config(entries: list[tuple[str, str]], project_root: str,
                             dry_run: bool) -> int:
    """Append verified (pattern, hint) pairs to this project's own
    .sovereign_config.json additional_error_hints. Skips any pattern already
    present (idempotent across repeated promote_rules.py runs)."""
    if not entries:
        return 0
    texts = [hint for _pattern, hint in entries]
    accepted_texts, rejected = prompt_artifacts.partition(
        texts, project_root, kind="error hint", mode="reject"
    )
    for text, verdict in rejected:
        for reason in verdict.reasons:
            print(f"  ✗ REJECTED (error hint) — {reason}: {text[:90]}")
    accepted = [(p, h) for p, h in entries if h in accepted_texts]
    if not accepted:
        return 0

    cfg_path = os.path.join(project_root, ".sovereign_config.json")
    try:
        with open(cfg_path) as f:
            cfg = json.load(f)
    except Exception as exc:
        print(f"  ⚠  could not read .sovereign_config.json: {exc}")
        return 0

    existing = cfg.get(ADDITIONAL_ERROR_HINTS_KEY, [])
    existing_patterns = {e.get("pattern") for e in existing if isinstance(e, dict)}
    new_entries = [
        {"pattern": p, "hint": h} for p, h in accepted if p not in existing_patterns
    ]
    if not new_entries:
        return 0

    if dry_run:
        print(f"\n  [dry-run] Would add {len(new_entries)} error hint(s) to "
              f".sovereign_config.json:")
        for e in new_entries:
            print(f"    {e['pattern']!r}: {e['hint'][:90]}")
        return len(new_entries)

    cfg[ADDITIONAL_ERROR_HINTS_KEY] = existing + new_entries
    with open(cfg_path, "w") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")
    return len(new_entries)


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

    # ── ERROR_HINTS learning ────────────────────────────────────────────────
    # Independent of the .roorules promotion above (it re-derives its own
    # groups from `drafts` directly) — a group that hit the .roorules
    # threshold but has no linkable error_code just yields zero candidates
    # here rather than blocking on the roorules step's own review/rejection.
    error_records = load_error_records(project_root)
    hint_candidates = candidate_error_hints(drafts, error_records, args.threshold)
    if hint_candidates:
        print(f"  {len(hint_candidates)} candidate error-hint(s) hit threshold "
              f"with a linkable error code...")
        hints_added = append_to_project_config(hint_candidates, project_root, args.dry_run)
        if hints_added:
            verb = "Would add" if args.dry_run else "Added"
            print(f"  ✓ {verb} {hints_added} error hint(s) to "
                  f".sovereign_config.json (additional_error_hints)")

    print_error_summary(project_root)


if __name__ == "__main__":
    main()
