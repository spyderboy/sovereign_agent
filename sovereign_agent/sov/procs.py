"""Which script a process is actually running — for finding run processes safely.

Matching "the args mention work.py and the project path" is not enough: a
shell command, an editor with monitor.py open, or `tail …/work.py` all match,
and whatever uses the match may kill it (2026-09-29: runctl.stop killed the
very shell that called it). A process only counts when its program is a
shell or Python and the script it runs — its first non-option argument — is
the one we mean. `-c` command strings never count.
"""
import os

_SHELLS = {"bash", "sh", "zsh", "dash"}
_TAKES_ARG = {"-W", "-X", "-O", "-Q"}          # python options followed by a value


def script_of(args: str) -> tuple[str | None, list[str]]:
    """(basename of the script being run, the remaining arguments) or (None, [])."""
    tokens = args.split()
    if not tokens:
        return None, []
    exe = os.path.basename(tokens[0]).lower()
    is_py = exe.startswith("python")
    if exe not in _SHELLS and not is_py:
        return None, []
    i = 1
    while i < len(tokens):
        t = tokens[i]
        if t == "-c" or (is_py and t == "-m"):
            return None, []                      # inline code or a module, not our script
        if t in _TAKES_ARG and is_py:
            i += 2
            continue
        if t.startswith("-"):
            i += 1
            continue
        return os.path.basename(t), tokens[i + 1:]
    return None, []


def mentions(tokens: list[str], real_root: str) -> bool:
    """True when any argument is a path that resolves to the project."""
    return any(os.path.realpath(os.path.expanduser(t)) == real_root
               for t in tokens if t.startswith(("/", "~")))
