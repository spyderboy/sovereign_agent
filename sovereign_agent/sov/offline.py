"""Run Flutter gates against the packages already on disk, never pub.dev.

`flutter test` and `flutter analyze` resolve packages online first unless told
`--no-pub`. On 2026-10-26 the Mac lost its connection at 05:05 and every task
after that failed with `Failed host lookup: 'pub.dev'` — twelve correct tasks
burned their attempts on a network error. A worker never changes pubspec.yaml
(it is locked), so the resolve step is never needed during a run.
"""
import re

_FLUTTER = re.compile(r"\bflutter\s+(test|analyze)\b")


def no_pub(cmd: str) -> str:
    """`cmd` with `--no-pub` added to every `flutter test` / `flutter analyze` that lacks it."""
    out, last = [], 0
    for m in _FLUTTER.finditer(cmd):
        seg_end = re.search(r"[;&|]|$", cmd[m.end():])
        segment = cmd[m.end(): m.end() + seg_end.start()]
        out.append(cmd[last:m.end()])
        if "--no-pub" not in segment:
            out.append(" --no-pub")
        last = m.end()
    out.append(cmd[last:])
    return "".join(out)
