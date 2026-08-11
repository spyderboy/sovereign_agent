from langgraph.graph import StateGraph, END
from state import AgentState
from nodes import architect, executor, validator, publisher


def route_after_validation(state: AgentState) -> str:
    """Route to publisher if tests pass, back to architect if they fail (max 3 retries)."""
    verdict   = state.get("validator_verdict", {})
    status    = verdict.get("status", state.get("build_status", "failed"))
    retries   = state.get("iteration_count", 0)

    if status in ("passed", "no_tests"):
        return "publisher"
    elif retries >= 3:
        print(f"  ⚠ Max retries reached — stopping. Check test output manually.")
        return END
    else:
        return "architect"


# ── Plan graph: architect → executor → END ────────────────────────────────────
# Run with: python app.py
# Picks the next task, writes .roo-mission.md, opens VSCode. You do the work in Roo.

plan_workflow = StateGraph(AgentState)
plan_workflow.add_node("architect", architect)
plan_workflow.add_node("executor",  executor)
plan_workflow.set_entry_point("architect")
plan_workflow.add_edge("architect", "executor")
plan_workflow.add_edge("executor", END)
plan_graph = plan_workflow.compile()


# ── Validate graph: validator → publisher/END ─────────────────────────────────
# Run with: python validate.py
# Checks tests and deploys if passing. Run after Roo has finished the task.

validate_workflow = StateGraph(AgentState)
validate_workflow.add_node("validator", validator)
validate_workflow.add_node("publisher", publisher)
validate_workflow.set_entry_point("validator")
validate_workflow.add_conditional_edges(
    "validator",
    route_after_validation,
    {"publisher": "publisher", "architect": END, END: END},
)
validate_workflow.add_edge("publisher", END)
validate_graph = validate_workflow.compile()


# Default export used by app.py
graph = plan_graph
