#!/usr/bin/env python3
"""xrun.py — run plans from the command line (the console uses the same code).

    python3 xrun.py show   ~/Code/<project>     # the plan (defaults filled in)
    python3 xrun.py launch ~/Code/<project>     # safe stop/clean/verify, then sweep + monitor
    python3 xrun.py stop   ~/Code/<project>     # stop everything, park the repo, stop pods
    python3 xrun.py status ~/Code/<project>
    python3 xrun.py reset-parking ~/Code/<project>   # after fixing parked tasks

Internal (called by sweep.sh and launch):
    python3 xrun.py hook  ~/Code/<project> <pass-label> [<profile>]
    python3 xrun.py guard ~/Code/<project>
"""
import json
import sys
from datetime import datetime

from sov import runctl


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print(__doc__)
        return 2
    cmd, project = argv[1], argv[2]
    plan = runctl.load_plan(project)
    if cmd == "show":
        print(json.dumps(plan, indent=2))
    elif cmd == "launch":
        r = runctl.launch(plan, log)
        print(json.dumps(r, indent=2))
        return 0 if r["ok"] else 1
    elif cmd == "stop":
        print(json.dumps(runctl.stop(plan), indent=2))
    elif cmd == "status":
        print(json.dumps(runctl.status(plan), indent=2))
    elif cmd == "reset-parking":
        import os
        from datetime import date
        root = plan["project"]
        for name in ("strong_failures.json", "parked.md"):
            p = os.path.join(root, "logs", name)
            if os.path.exists(p):
                os.replace(p, os.path.join(root, "logs", f"{date.today()}-{name}"))
        print("parking reset: strong-failure counts cleared (old files kept with a date prefix)")
    elif cmd == "hook":
        return runctl.hook(plan, argv[3] if len(argv) > 3 else "", log)
    elif cmd == "guard":
        runctl.guard(plan, log)
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
