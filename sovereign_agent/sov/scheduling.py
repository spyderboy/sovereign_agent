"""Task scheduling for parallel workers: grouping same-file and
implement→test runs into chains, and assigning chains to workers by file
ownership.

Extracted from work.py (2026-09-28); behaviour unchanged.
"""
import re

from sov.roadmap import _is_test_task, task_text

# Two kinds of adjacency in ROADMAP.md's task order must never be split
# across parallel workers, or the tasks race each other with no
# coordination:
#   (a) consecutive tasks targeting the same primary file (one worker can
#       leave the file mid-edit for another to inherit)
#   (b) an "implement" task immediately followed by its test task (the
#       test would run against a possibly-missing implementation)
# Chains are built from the FULL, unfiltered task list first; workers are
# then assigned whole chains (see _assign_chains), so a chain always lands on
# exactly one worker.
MAX_CHAIN_LEN = 6  # cap to avoid giant prompts / one worker hoarding tasks


def _task_primary_file(t: str) -> str | None:
    # 2026-09-28: this only matched lib/ and test/ .dart paths, so on a
    # Python roadmap every task had no primary file, nothing chained, and
    # same-file tasks were dealt round-robin across workers. Prefer the
    # "In `path`:" target, then any path-like token with an extension.
    m = re.search(r'\bIn `([\w./-]+\.\w+)`', t) \
        or re.search(r'`([\w.-]+(?:/[\w.-]+)+\.\w+)`', t) \
        or re.search(r'\b(lib/[\w/]+\.dart|test/[\w/]+\.dart)\b', t)
    return m.group(1) if m else None


def _assign_chains(chains: list, stride: int, worker_id: int) -> list:
    # Assign by FILE OWNERSHIP, not chain index: MAX_CHAIN_LEN splits a
    # long same-file run into several chains, and a file can recur
    # non-adjacently later in the roadmap. Either way the pieces could
    # land on different workers and conflict at merge. A chain inherits
    # the owner of any file it touches that is already owned; otherwise
    # it takes the next worker round-robin, and claims all its files.
    owner_of: dict[str, int] = {}
    next_w = 0
    kept = []
    for c in chains:
        files = [f for f in (_task_primary_file(t) for _, t in c) if f]
        owner = next((owner_of[f] for f in files if f in owner_of), None)
        if owner is None:
            owner = next_w % stride
            next_w += 1
        for f in files:
            owner_of.setdefault(f, owner)
        if owner == worker_id:
            kept.append(c)
    return kept


def _feature_stem(path: str | None) -> str | None:
    """Basename with directory, .dart suffix, and a trailing _test/_test.dart
    stripped — e.g. 'lib/widgets/chrome_knob.dart' and
    'test/widgets/chrome_knob_test.dart' both stem to 'chrome_knob'. Used to
    confirm a test task actually pairs with the feature task it follows,
    rather than assuming any adjacent test task does."""
    if not path:
        return None
    stem = path.rsplit("/", 1)[-1]
    if stem.endswith(".dart"):
        stem = stem[:-5]
    if stem.endswith("_test"):
        stem = stem[:-5]
    return stem


def build_chains(tasks: list[str]) -> list[list[tuple[int, str]]]:
    """Group the full task list into (1-based index, task line) chains."""
    indexed_tasks_all = [(i + 1, t) for i, t in enumerate(tasks)]

    chains: list[list[tuple[int, str]]] = []
    i = 0
    while i < len(indexed_tasks_all):
        idx, task_line = indexed_tasks_all[i]
        pf = _task_primary_file(task_line)
        chain = [(idx, task_line)]
        j = i + 1
        while j < len(indexed_tasks_all) and j - i < MAX_CHAIN_LEN:
            nidx, ntask = indexed_tasks_all[j]
            same_file  = pf is not None and _task_primary_file(ntask) == pf
            is_test_of = _is_test_task(task_text(ntask)) and not _is_test_task(task_text(indexed_tasks_all[j - 1][1]))
            if same_file or is_test_of:
                chain.append((nidx, ntask))
                j += 1
            else:
                break
        chains.append(chain)
        i = j
    return chains


def tasks_for_worker(tasks: list[str], stride: int, worker_id: int) -> list[tuple[int, str]]:
    """This worker's (index, task line) pairs, in ROADMAP.md order. With
    stride 1 that is every task."""
    chains = build_chains(tasks)
    if stride > 1:
        chains = _assign_chains(chains, stride, worker_id)
    # Flatten assigned chains back into an ordered task list (same relative
    # order as ROADMAP.md, restricted to this worker's chains).
    return [item for chain in chains for item in chain]
