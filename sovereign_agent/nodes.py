import subprocess
import os
import json
import requests
from dotenv import load_dotenv
from state import AgentState

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"))

# ─── Model config ─────────────────────────────────────────────────────────────
OLLAMA_URL      = os.getenv("LOCAL_MODEL_URL",  "http://localhost:11434")
ARCHITECT_MODEL = os.getenv("ARCHITECT_MODEL", "qwen2.5-coder:32b")  # planning / structured JSON
EXECUTOR_MODEL  = os.getenv("EXECUTOR_MODEL",  "qwen2.5-coder:32b")  # code generation
VALIDATOR_MODEL = os.getenv("VALIDATOR_MODEL", "qwen3:4b")                       # fast pass/fail checks


def _ollama_chat(model: str, system: str, user: str) -> str:
    """Call Ollama chat endpoint, return the assistant message content."""
    response = requests.post(
        f"{OLLAMA_URL}/api/chat",
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user",   "content": user},
            ],
            "stream": False,
        },
        timeout=300,
    )
    response.raise_for_status()
    return response.json()["message"]["content"]


def _ollama_chat_json(model: str, system: str, user: str) -> dict:
    """Call Ollama and parse JSON from the response text."""
    raw = _ollama_chat(model, system, user)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # Extract first {...} block from the response
        start = raw.find("{")
        end   = raw.rfind("}") + 1
        if start != -1:
            try:
                return json.loads(raw[start:end])
            except json.JSONDecodeError:
                pass
        return {"raw": raw}


# ─── ARCHITECT ────────────────────────────────────────────────────────────────
def architect(state: AgentState) -> dict:
    """
    Reads the backlog, reasons about the project state, and picks the
    highest-priority task. Outputs a structured task brief for the executor.
    """
    print("--- ARCHITECT: Planning next task ---")

    project_root = state.get("project_root", os.getcwd())

    # Prefer today_approved.md (written by standup.py) over the raw backlog
    approved_path = os.path.join(project_root, "today_approved.md")
    default_backlog = os.path.join(project_root, state.get("backlog_path", "backlog.md"))
    backlog_path = approved_path if os.path.exists(approved_path) else default_backlog
    backlog = open(backlog_path).read() if os.path.exists(backlog_path) else "No backlog found."

    prev_status = state.get("build_status", "idle")
    prev_task   = state.get("current_task", "none")

    result = _ollama_chat_json(
        model=ARCHITECT_MODEL,
        system=(
            "You are the Architect agent for a software project. "
            "Your job is to read the project backlog and decide the single most important "
            "task to work on next. Return ONLY valid JSON with these keys:\n"
            "  task        (string)  — one-sentence description of the task\n"
            "  file_hint   (string)  — the most relevant file to edit, or empty string\n"
            "  acceptance  (string)  — one-sentence definition of done\n"
            "  complexity  (string)  — 'small' | 'medium' | 'large'\n"
            "Do not include any explanation outside the JSON object."
        ),
        user=(
            f"Project backlog:\n{backlog}\n\n"
            f"Last task attempted: {prev_task}\n"
            f"Last build status:   {prev_status}\n\n"
            "What is the next task?"
        ),
    )

    task_brief = result.get("task", str(result))
    print(f"  → Task: {task_brief}")
    print(f"  → File: {result.get('file_hint', '?')}")
    print(f"  → Done when: {result.get('acceptance', '?')}")

    return {
        "current_task": task_brief,
        "task_brief":   result,
        "build_status": "planning",
    }


# ─── EXECUTOR ─────────────────────────────────────────────────────────────────
def executor(state: AgentState) -> dict:
    """
    Writes the task brief to .roo-mission.md so Roo Code picks it up,
    then opens VSCode in the project directory.
    """
    print("--- EXECUTOR: Handing off to Roo ---")

    project_root = state.get("project_root", os.getcwd())
    brief = state.get("task_brief", {"task": state.get("current_task", "no task")})

    mission = (
        "# Roo Mission Brief\n\n"
        f"**Task:** {brief.get('task', state.get('current_task'))}\n\n"
        f"**File to focus on:** {brief.get('file_hint', 'see backlog')}\n\n"
        f"**Definition of done:** {brief.get('acceptance', 'task complete')}\n\n"
        f"**Complexity:** {brief.get('complexity', 'unknown')}\n\n"
        "---\n"
        "_Written by Architect agent. Open in Roo Code and run the task._\n"
    )

    mission_path = os.path.join(project_root, ".roo-mission.md")
    with open(mission_path, "w") as f:
        f.write(mission)

    print(f"  → Wrote {mission_path}")

    try:
        subprocess.run(["code", project_root], check=False, timeout=5)
        print(f"  → Opened VSCode at {project_root}")
    except (FileNotFoundError, subprocess.TimeoutExpired):
        print("  → Open VSCode manually")

    return {"build_status": "coding"}


# ─── VALIDATOR ────────────────────────────────────────────────────────────────
def validator(state: AgentState) -> dict:
    """
    Runs available tests. Returns no_tests if no runner is found,
    passed/failed based on exit code, without calling the LLM for simple cases.
    """
    print("--- VALIDATOR: Checking quality ---")

    project_root = state.get("project_root", os.getcwd())
    test_output = None
    exit_code   = None

    for cmd in [["python", "-m", "pytest", "--tb=short", "-q"],
                ["flutter", "test"],
                ["npm", "test", "--", "--watchAll=false"]]:
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=60, cwd=project_root)
            test_output = result.stdout + result.stderr
            exit_code   = result.returncode
            break
        except FileNotFoundError:
            continue
        except subprocess.TimeoutExpired:
            test_output = "Test run timed out."
            exit_code   = 1
            break

    # No test runner installed — don't block the pipeline
    if test_output is None:
        verdict = {"status": "no_tests", "summary": "No test runner found.", "issues": []}
        print(f"  → Status:  no_tests")
        print(f"  → Summary: No test runner found — skipping validation.")
        return {
            "build_status":      "no_tests",
            "test_logs":         "",
            "validator_verdict": verdict,
            "iteration_count":   state.get("iteration_count", 0) + 1,
        }

    # Simple exit-code check — no LLM needed for clear pass/fail
    if exit_code == 0:
        verdict = {"status": "passed", "summary": "All tests passed.", "issues": []}
    else:
        # Use small model only when tests actually ran and failed
        verdict = _ollama_chat_json(
            model=VALIDATOR_MODEL,
            system=(
                "You classify test run output. Return JSON with:\n"
                "  status  (string) — 'passed' | 'failed' | 'no_tests'\n"
                "  summary (string) — one sentence\n"
                "  issues  (array of strings) — failure messages, empty if passed"
            ),
            user=f"Test output:\n{test_output[:3000]}",
        )

    print(f"  → Status:  {verdict.get('status')}")
    print(f"  → Summary: {verdict.get('summary')}")

    return {
        "build_status":      verdict.get("status", "unknown"),
        "test_logs":         test_output,
        "validator_verdict": verdict,
        "iteration_count":   state.get("iteration_count", 0) + 1,
    }


# ─── PUBLISHER ────────────────────────────────────────────────────────────────
def publisher(state: AgentState) -> dict:
    """Deploys the release to Firebase."""
    print("--- PUBLISHER: Deploying ---")

    project_root = state.get("project_root", os.getcwd())

    # Read firebase project ID from sovereign.json if present, else fall back to env
    sovereign_cfg = os.path.join(project_root, "sovereign.json")
    firebase_project = os.getenv("FIREBASE_PROJECT_ID")
    if not firebase_project and os.path.exists(sovereign_cfg):
        with open(sovereign_cfg) as f:
            cfg = json.load(f)
        firebase_project = cfg.get("firebase_project_id")
    firebase_project = firebase_project or "my-firebase-project"

    for cmd in [
        ["flutter", "build", "web", "--release"],
        ["firebase", "deploy", f"--project={firebase_project}"],
    ]:
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=300, cwd=project_root)
            if result.returncode != 0:
                print(f"  ✗ {' '.join(cmd)}: {result.stderr[:200]}")
                return {"build_status": "deploy_failed"}
            print(f"  ✓ {' '.join(cmd)}")
        except FileNotFoundError:
            print(f"  (skipped — {cmd[0]} not installed)")

    return {"build_status": "deployed"}
