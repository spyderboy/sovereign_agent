"""Prompt construction and the implementer call: role specs per language
and implement_task.

Extracted from work.py (2026-09-28); behaviour unchanged.
"""
import import_fixer
import json
import os
import re
from sov.config import (
    CALL_TIMEOUT_S,
    TIER1_MODEL,
    TIER2_MODEL,
    TIER3_MODEL,
)
from sov import state
from sov.apistubs import _extract_api_stub
from sov.deps import _symbol_index, _target_ts_file
from sov.files import LOCKED_FILES, _DIFF_MODE_LINE_THRESHOLD
from sov.lazytools import _get_advisor
from sov.llm import _LARGE_MODEL_THRESHOLD_GB, _extract_json_candidate, _model_size_gb, _normalize_backtick_strings, ollama


# Per-language role-line specs for the coding-agent system prompt (2026-08-22).
# This used to be `if PROJECT_LANGUAGE == "go" else <Dart text, unconditionally>`
# inline in implement_task() — every language other than Go silently got told
# "You are a Dart/Flutter coding agent" and shown a .dart example path. Same
# bug already found and fixed in qwen_advisor.py's persona line (see that
# file's 2026-08-22 note) — this is the same fix applied to the ACTUAL coding
# prompt, which matters far more than the advisor hint does. go and dart
# entries are copied verbatim from the original inline code (regression-safe,
# see test/test_role_specs.py-style checks run before this shipped);
# typescript/python/swift are new; anything else falls through to
# _generic_role_spec(), which builds an honest persona from PROJECT_LANGUAGE
# itself instead of defaulting to Dart.
_ROLE_SPECS: dict[str, dict] = {
    "go": {
        "persona": "You are a Go coding agent. Implement the given task.\n",
        "example_path": "sim/mote_move.go",
        "file_count_line": "This task changes EXACTLY ONE file — the one named in the task. Return exactly one key.\n",
        "completeness_block": (
            "COMPLETENESS: the file must be complete and compilable — every opened brace closed, "
            "no trailing truncation. Before finishing, verify the final character of the file "
            "content is the closing brace of the last function.\n"
            "NESTING: keep nesting depth <= 3. Use early `continue`/`return` guard clauses "
            "instead of nested if-blocks — deep nesting causes brace-count errors.\n"
        ),
    },
    "dart": {
        "persona": "You are a Dart/Flutter coding agent. Implement the given task.\n",
        "example_path": "lib/game/game_core.dart",
        "file_count_line": "Typical task requires 1-3 files. If you find yourself changing more than 5, stop and reconsider.\n",
        "completeness_block": "",
    },
    "typescript": {
        "persona": "You are a TypeScript coding agent. Implement the given task.\n",
        "example_path": "lib/ops/spawn.ts",
        "file_count_line": "Typical task requires 1-3 files. If you find yourself changing more than 5, stop and reconsider.\n",
        "completeness_block": (
            "COMPLETENESS: the file must be complete and compilable — every opened brace closed, "
            "no trailing truncation. Before finishing, verify the final character of the file "
            "content is the closing brace of the last function.\n"
        ),
    },
    "python": {
        "persona": "You are a Python coding agent. Implement the given task.\n",
        "example_path": "lib/ops/spawn.py",
        "file_count_line": "Typical task requires 1-3 files. If you find yourself changing more than 5, stop and reconsider.\n",
        "completeness_block": "",
    },
    "swift": {
        "persona": "You are a Swift coding agent. Implement the given task.\n",
        "example_path": "Sources/App/GameCore.swift",
        "file_count_line": "Typical task requires 1-3 files. If you find yourself changing more than 5, stop and reconsider.\n",
        "completeness_block": (
            "COMPLETENESS: the file must be complete and compilable — every opened brace closed, "
            "no trailing truncation. Before finishing, verify the final character of the file "
            "content is the closing brace of the last function.\n"
        ),
    },
}

# Brace-delimited languages — decides whether an unlisted language's generic
# spec includes the brace-completeness reminder (wrong advice for
# indentation/END-delimited languages like Python, COBOL, VB).
_BRACE_LANGUAGES = {
    "go", "dart", "typescript", "java", "kotlin", "csharp", "cpp", "c",
    "php", "rust", "scala", "groovy", "swift", "objective-c", "javascript",
}


def _generic_role_spec(language: str) -> dict:
    """Role spec for a language with no hand-authored entry above — built
    from the language's own name so e.g. COBOL/VB/PHP/Java get an accurate
    persona instead of silently inheriting Dart's."""
    display = (
        language.replace("-", " ").replace("_", " ").title()
        if language and language != "generic" else "software"
    )
    completeness = (
        "COMPLETENESS: the file must be complete and compilable — every "
        "opened brace closed, no trailing truncation.\n"
        if language in _BRACE_LANGUAGES else ""
    )
    return {
        "persona": f"You are a {display} coding agent. Implement the given task.\n",
        "example_path": None,  # no fabricated example for a language we don't know real paths for
        "file_count_line": "Typical task requires 1-3 files. If you find yourself changing more than 5, stop and reconsider.\n",
        "completeness_block": completeness,
    }


def _role_spec_for(language: str) -> dict:
    return _ROLE_SPECS.get(language) or _generic_role_spec(language)


def implement_task(task: str, file_contents: dict[str, str], errors: str = "",
                   model: str | None = None,
                   is_test: bool = False,
                   project_root: str = ".") -> tuple[dict[str, str], dict]:
    """Ask Ollama to implement the task. Returns {rel_path: new_content}."""
    model = model or TIER1_MODEL
    # Split context: editable files get full content; locked files get a compact
    # read-only API stub so the model knows what classes/constructors exist without
    # being able to overwrite them.  Previously locked files were stripped entirely,
    # which caused the model to hallucinate APIs from training data.
    editable = {p: c for p, c in file_contents.items() if p not in LOCKED_FILES}
    locked_context = {p: c for p, c in file_contents.items() if p in LOCKED_FILES}

    context = "\n\n".join(
        f"=== {path} ===\n{content}"
        for path, content in editable.items()
    )

    # Inject compact stubs for locked files — class name, constructor, public API only.
    # Capped at 800 chars per file to keep tier-1 token budget manageable.
    if locked_context:
        stub_parts = []
        for path, content in locked_context.items():
            stub = _extract_api_stub(path, content, max_chars=1500)
            if stub:
                stub_parts.append(f"=== {path} [READ-ONLY — do not return this path] ===\n{stub}")
        if stub_parts:
            context += "\n\n// ── Locked file APIs (read-only context) ──\n" + "\n\n".join(stub_parts)
    vision = open("VISION.md").read() if os.path.exists("VISION.md") else ""
    _project_rules = open(".roorules").read() if os.path.exists(".roorules") else ""
    _global_rules_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "global_rules.md")
    _global_rules = open(_global_rules_path).read() if os.path.exists(_global_rules_path) else ""
    rules = (_global_rules + "\n\n---\n\n# Project-specific rules\n\n" + _project_rules).strip()

    # Inject API guide so the model knows what already exists before inventing new classes.
    # Skip for tier-1 (7B): the guide alone costs ~5 000 tokens and leaves too little room
    # for file context. Tier-2 and Tier-3 (MoE models) get a trimmed guide to save headroom.
    # Claude (called separately via _escalate_to_claude) always receives the full guide.
    _api_guide_path = os.path.join(project_root, "docs", "API_GUIDE.md")
    api_guide_block = ""
    _is_tier1 = model == TIER1_MODEL
    _is_moe_tier = model in (TIER2_MODEL, TIER3_MODEL)  # T2/T3 are MoE — trim guide to save headroom
    # T4 (32B dense) and Claude get the full guide; T1 gets none (too costly for 7B context)
    if os.path.exists(_api_guide_path) and not _is_tier1:
        _guide = open(_api_guide_path).read()
        _guide = _guide[:6000] if _is_moe_tier else _guide
        api_guide_block = f"\n\nEXISTING API REFERENCE — read before creating any new class or file:\n{_guide}"

    # Inject top recurring error patterns as a "known pitfalls" block
    pitfalls_block = ""
    try:
        adv = _get_advisor()
        top = adv.top_error_patterns(project_root, n=8)
        if top:
            lines = "\n".join(f"  - {p['code']} ({p['count']}×)" for p in top)
            pitfalls_block = (
                f"\n\nKNOWN PITFALLS (most frequent errors in this project — avoid these):\n"
                f"{lines}"
            )
    except Exception:
        pass

    # Compact locked list — show directories grouped to save tokens
    locked_dirs: dict[str, list[str]] = {}
    for f in sorted(LOCKED_FILES):
        d = f.rsplit("/", 1)[0] if "/" in f else "root"
        locked_dirs.setdefault(d, []).append(f.rsplit("/", 1)[-1])
    locked_list = "\n".join(
        f"  {d}/: {', '.join(names)}" for d, names in sorted(locked_dirs.items())
    )
    # ── Repair, don't rewrite ────────────────────────────────────────────────
    # The failed file stays on disk between attempts and is re-read into
    # `context` above, so the model can already SEE its previous attempt — it
    # was simply never told that is what it was looking at, and "implement the
    # task, return complete file contents" reads as an instruction to start
    # over. So it did, every time, and each attempt re-rolled the dice on
    # everything that was already right.
    #
    # The cost is visible in the logs as non-monotonic error counts:
    #   arena.dart      12 → 5 → 3 → 1 → 9      (one error away, then undone)
    #   hp_pill.dart     5 → 10 → 2 → 5 → 3 → 4  (ten attempts, no convergence)
    #   gem_painter      2 → 3                   (budget spent going backwards)
    # A model that is right 3 times in 4 per construct will essentially never
    # get twenty constructs right simultaneously, and one shot at a time is all
    # the loop was asking for. Naming the file as its own prior work and asking
    # for a minimal delta turns twenty independent coin flips into one.
    _tgt = re.search(r"\bIn ([\w./-]+\.\w+):", task)
    _target = _tgt.group(1) if _tgt else None
    _has_prior = bool(_target and _target in editable and editable[_target].strip())
    _target_lines = editable[_target].count("\n") + 1 if _has_prior else 0
    _use_diff_mode = _has_prior and _target_lines > _DIFF_MODE_LINE_THRESHOLD
    if errors and _has_prior:
        _return_instr = (
            f"Return SEARCH/REPLACE block(s) for `{_target}` per the format below — "
            f"do not return its complete content."
            if _use_diff_mode else
            f"Return the complete file, but as the previous file with the smallest "
            f"possible edit applied."
        )
        error_block = (
            f"\n\n=== THIS IS A REPAIR, NOT A REWRITE ===\n"
            f"`{_target}` in 'Current files' above is YOUR OWN PREVIOUS ATTEMPT. "
            f"It is mostly correct — it failed only on the errors below.\n\n"
            f"Start from that exact file and change ONLY what these errors "
            f"require. Keep every import, helper, constant, structure and name "
            f"that is not implicated. Do not restructure, do not rename, do not "
            f"re-derive the parts that already work. {_return_instr}\n\n"
            f"Errors to fix:\n{errors}"
        )
    else:
        error_block = (f"\n\nPrevious attempt failed with these errors — fix them:\n{errors}"
                       if errors else "")

    # Test tasks get an extra hard constraint injected into the system prompt
    # so the model understands scope BEFORE it generates any code.
    scope_block = (
        "\n\nTASK SCOPE — CRITICAL:\n"
        "This is a TEST-ONLY task. You may ONLY create or modify files under test/.\n"
        "NEVER write to any file under lib/ — not even to add a missing feature.\n"
        "If the feature you need to test does not exist or has the wrong API:\n"
        "  • Write a skip-marked placeholder test and stop:\n"
        "      test('...', () {}, skip: 'feature not implemented — implement task required first');\n"
        "  • Do NOT invent or add the implementation yourself.\n"
        "  • Do NOT modify source files to make your test compile.\n"
        "The implement task will follow — your job here is ONLY the test file.\n"
        if is_test else ""
    )

    # Large target file (2026-09-09): a full rewrite makes the model
    # regenerate everything, including the parts already correct, which is
    # slow enough to time out on this model/hardware for files this size.
    # Scoped to _target ONLY — any other file in this same response still
    # uses complete content as normal.
    diff_mode_block = (
        f"\n\n=== {_target} IS LARGE ({_target_lines} lines) — USE SEARCH/REPLACE ===\n"
        f"For `{_target}` ONLY, return one or more SEARCH/REPLACE blocks instead of "
        f"the full file, in this exact format:\n"
        f"<<<<<<< SEARCH\n"
        f"<the exact existing lines you are changing, copied verbatim from "
        f"'Current file contents' below — including whitespace/indentation>\n"
        f"=======\n"
        f"<the new lines that replace them>\n"
        f">>>>>>> REPLACE\n"
        f"Concatenate multiple such blocks in the same string if you need to change "
        f"more than one place in the file. The SEARCH text must match the current "
        f"file EXACTLY — copy it, do not retype it from memory. Every other file "
        f"you return (if any) still uses complete file content as normal.\n"
        if _use_diff_mode else ""
    )

    _role = _role_spec_for(state.PROJECT_LANGUAGE)
    _example_line = (
        f"  - Keys are file paths exactly as given (e.g. '{_role['example_path']}')\n"
        if _role["example_path"]
        else "  - Keys are file paths exactly as given (e.g. the path named in the task)\n"
    )
    system_prompt = (
        _role["persona"]
        + "Return ONLY a JSON object where:\n"
        + _example_line
        + "  - Values are the COMPLETE new file contents (not diffs, not snippets)\n"
        "Only include files that DIRECTLY change to implement this specific task.\n"
        "DO NOT touch files that are not required. DO NOT refactor or improve unrelated files.\n"
        "DO NOT create new files unless the task explicitly says to create one.\n"
        + _role["file_count_line"]
        + "Keep implementations MINIMAL — write the fewest lines that make the task work. "
        "Do not add elaborate systems, helpers, or abstractions unless required.\n"
        + _role["completeness_block"]
        + "No explanation or markdown outside the JSON object.\n\n"
        "CRITICAL — these files are LOCKED and will be silently ignored if returned:\n"
        f"{locked_list}\n\n"
        "Coding rules:\n"
        f"{rules}"
        f"{pitfalls_block}"
        f"{scope_block}"
        f"{diff_mode_block}"
    )
    # Import map (TS projects): tell the model EXACTLY which module exports each
    # symbol, with the relative specifier already computed for the target file.
    # This is the single biggest curb on the "invented import / wrong module"
    # loop — the dominant local-model failure. Harmless no-op for non-TS.
    import_map_block = ""
    _tgt = _target_ts_file(task)
    if _tgt:
        try:
            idx, exp, sigs = _symbol_index(project_root)
            _map = import_fixer.build_import_map(project_root, _tgt, idx, exp, sigs)
            if _map:
                import_map_block = f"\n\n{_map}\n"
        except Exception:
            import_map_block = ""

    user_prompt = (
        f"Task: {task}\n\n"
        f"Project vision summary:\n{vision[:1500]}\n"
        f"{api_guide_block}"
        f"{import_map_block}\n"
        f"Current file contents:\n{context}"
        f"{error_block}"
    )
    # Scale timeout with model size: large 32B models need up to 30 min on a
    # memory-constrained Mac (macOS must evict cached files + swap before the
    # model fits in unified memory — this can take 20+ min when RAM is tight).
    # (3-4 min to load + 10-12 min to generate a full file).
    size_gb = _model_size_gb(model)
    coding_timeout = CALL_TIMEOUT_S or (1800 if size_gb >= _LARGE_MODEL_THRESHOLD_GB else 600)
    raw = ollama(model, system_prompt, user_prompt, timeout=coding_timeout)
    call_info = {"system": system_prompt, "user": user_prompt, "raw": raw}

    json_text = _extract_json_candidate(raw)
    start, end = json_text.find("{"), json_text.rfind("}") + 1
    if start == -1:
        return {}, call_info
    candidate = json_text[start:end]
    try:
        return json.loads(candidate), call_info
    except Exception:
        pass
    try:
        return json.loads(_normalize_backtick_strings(candidate)), call_info
    except Exception:
        return {}, call_info
