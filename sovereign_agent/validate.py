"""
Run this after Roo has finished the task written to .roo-mission.md.
Runs tests, and deploys if they pass.

Usage:
    python validate.py
    python validate.py --project ~/Code/my-app
"""
import argparse
import os
import sys
from graph import validate_graph

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Sovereign Agent — validator/deploy step")
    parser.add_argument("--project", help="Path to the target project folder (default: current dir)", default=None)
    args = parser.parse_args()

    if args.project:
        project_root = os.path.abspath(args.project)
        if not os.path.isdir(project_root):
            print(f"⚠  Project folder not found: {project_root}")
            sys.exit(1)
        print(f"Project: {project_root}")
    else:
        project_root = os.getcwd()

    result = validate_graph.invoke({
        "project_root":      project_root,
        "backlog_path":      "backlog.md",
        "current_task":      "",
        "task_brief":        {},
        "build_status":      "coding",
        "test_logs":         "",
        "validator_verdict": {},
        "iteration_count":   0,
    })
    print(f"\nFinal status: {result.get('build_status')}")
