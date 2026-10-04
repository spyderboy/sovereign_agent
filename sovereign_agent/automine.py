#!/usr/bin/env python3
"""automine — between sweep passes, apply the guards Xanadu mined from its own failures.

    python3 automine.py <project> [--dry-run]

`api_surface.py --mine --apply` needed a person to run it, so "self-healing" carried an
asterisk (2026-10-04). sweep.sh now runs this before every pass, when the tree has just
been reset and no worker is running. Safe by construction:
  - a guard whose pattern doesn't compile, already exists, or MATCHES COMMITTED CODE is
    dropped (it would block valid code) — each drop is logged with the reason;
  - at most MAX_NEW_PER_PASS guards per pass;
  - a dirty tree is refused, never touched;
  - .sovereign_config.json is committed, so the next forced checkout keeps the guards;
  - guards are marked origin "mined" + mined_at, so reports count them as Xanadu's own;
  - opt out per project with "auto_mine_guards": false.
"""
import json
import os
import re
import subprocess
import sys
from datetime import date, datetime

MAX_NEW_PER_PASS = 10
CONFIG = ".sovereign_config.json"


def _git(project: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", project] + list(args), capture_output=True, text=True)


def _committed_texts(project: str):
    for path in _git(project, "ls-files").stdout.splitlines():
        if path == CONFIG or path.startswith("logs/"):
            continue
        try:
            with open(os.path.join(project, path), errors="strict") as f:
                yield path, f.read()
        except (OSError, UnicodeDecodeError):
            continue                                   # binary or unreadable: not code a guard could block


def _default_mine(project: str) -> list[dict]:
    import api_surface
    return api_surface.mine_python(project) if api_surface.language(project) == "python" else api_surface.mine(project)


def auto_mine(project: str, mine_fn=None, today: str | None = None, dry_run: bool = False) -> list[dict]:
    project = os.path.abspath(project)
    log_lines = []

    def log(msg):
        log_lines.append(f"{datetime.now().isoformat(timespec='seconds')} {msg}")

    try:
        cfg_path = os.path.join(project, CONFIG)
        try:
            cfg = json.load(open(cfg_path))
        except (OSError, ValueError):
            log("skipped: no readable .sovereign_config.json")
            return []
        if cfg.get("auto_mine_guards") is False:
            log("skipped: auto_mine_guards is false")
            return []
        if _git(project, "status", "--porcelain", "--untracked-files=no").stdout.strip():
            log("skipped: tracked files have uncommitted changes — never touched")
            return []

        existing = {g.get("pattern") for g in cfg.get("additional_bad_patterns", []) if isinstance(g, dict)}
        texts = None
        kept = []
        for g in (mine_fn or _default_mine)(project):
            pattern = g.get("pattern")
            if not pattern or pattern in existing:
                log(f"dropped {pattern}: already a guard")
                continue
            try:
                rx = re.compile(pattern, re.MULTILINE)
            except re.error as e:
                log(f"dropped {pattern}: does not compile ({e})")
                continue
            texts = texts if texts is not None else list(_committed_texts(project))
            hit = next((path for path, text in texts if rx.search(text)), None)
            if hit:
                log(f"dropped {pattern}: matches committed code ({hit})")
                continue
            if len(kept) >= MAX_NEW_PER_PASS:
                log(f"deferred {pattern}: {MAX_NEW_PER_PASS} per pass")
                continue
            kept.append({**g, "origin": "mined", "mined_at": today or date.today().isoformat()})
            existing.add(pattern)
            log(f"kept {pattern}")

        if kept and dry_run:
            log(f"dry run: would apply {len(kept)}")
        elif kept:
            cfg.setdefault("additional_bad_patterns", []).extend(kept)
            with open(cfg_path, "w") as f:
                json.dump(cfg, f, indent=2)
                f.write("\n")
            _git(project, "add", CONFIG)
            msg = f"guards: +{len(kept)} mined from run logs (automatic)"
            r = _git(project, "commit", "-m", msg, "--", CONFIG)
            if r.returncode != 0 and "user.email" in (r.stderr + r.stdout):
                r = _git(project, "-c", "user.name=Xanadu", "-c", "user.email=xanadu@localhost",
                         "commit", "-m", msg, "--", CONFIG)
            if r.returncode != 0:
                _git(project, "checkout", "--", CONFIG)      # never leave the tree dirty for the next pass
                log(f"commit failed, config restored: {(r.stdout + r.stderr).strip()[:200]}")
                return []
            log(msg)
        return kept
    finally:
        try:
            os.makedirs(os.path.join(project, "logs"), exist_ok=True)
            with open(os.path.join(project, "logs", "automine.log"), "a") as f:
                f.write("".join(line + "\n" for line in log_lines))
        except OSError:
            pass


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--dry-run"]
    if len(args) != 1:
        sys.exit(__doc__)
    dry = "--dry-run" in sys.argv
    added = auto_mine(args[0], dry_run=dry)
    print(f"automine: {len(added)} guard(s) {'would be applied' if dry else 'applied'}"
          + "".join(f"\n  {g['pattern']}" for g in added))
