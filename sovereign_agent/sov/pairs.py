"""Exact-pair edit tasks: apply them without a model.

A kit writes an edit to an existing file as REPLACE `old` WITH `new` pairs, copied
from a verified reference (`\\n` marks a line break inside a pair). The pairs are
the whole answer; the model's only job is to copy them. On 2026-10-26/27 the 27B
rewrote the whole file instead on every attempt for nine hours: EstateWise's last
five step-2 tasks were apartment_detail_screen.dart cut from 382 lines to 3,
building_detail_screen.dart from 315 to 121, invented imports (property.dart,
rent_detail.dart). Applied mechanically, all five passed their gates in minutes.

So the harness applies the pairs itself, then runs the normal validation. If any
pair does not match the file exactly once, or validation fails, nothing changes:
the file is restored and the task goes to the model as before.
"""
import os
import re

_PAIR = re.compile(r"REPLACE\s+`(.*?)`\s+WITH\s+`(.*?)`", re.S)
_TARGETS = (
    re.compile(r"\bIn `?([\w./-]+\.\w+)`?:"),
    re.compile(r"^(?:- \[[ x]\] )?Edit `?([\w./-]+\.\w+)`?"),
    re.compile(r"\b[Ii]n `?([\w.-]+(?:/[\w.-]+)+\.\w+)`?"),
)


def _unescape(text: str) -> str:
    return text.replace("\\n", "\n")


def exact_pairs(task: str) -> list[tuple[str, str]]:
    """Every REPLACE `old` WITH `new` pair in the task, line breaks restored, in order."""
    return [(_unescape(o), _unescape(n)) for o, n in _PAIR.findall(task)]


def pair_target(task: str, project_root: str) -> str | None:
    """The existing file an exact-pair task edits, or None when the task has no pairs or no such file."""
    if not _PAIR.search(task):
        return None
    for pattern in _TARGETS:
        m = pattern.search(task.strip())
        if m and os.path.isfile(os.path.join(project_root, m.group(1))):
            return m.group(1)
    return None


def apply_exact_pairs(task: str, project_root: str) -> tuple[str, str] | None:
    """(target, new file text) when every pair applies, in order, to text that occurs exactly once.

    None when the task is not an exact-pair edit, a REPLACE is malformed (so the parsed
    pairs might not be the whole task), or any old text is missing or ambiguous.
    Never writes anything.
    """
    target = pair_target(task, project_root)
    if target is None:
        return None
    pairs = exact_pairs(task)
    if not pairs or len(pairs) != len(re.findall(r"\bREPLACE\s+`", task)):
        return None
    with open(os.path.join(project_root, target), encoding="utf-8") as f:
        body = f.read()
    for old, new in pairs:
        if not old or body.count(old) != 1:
            return None
        body = body.replace(old, new, 1)
    return target, body
