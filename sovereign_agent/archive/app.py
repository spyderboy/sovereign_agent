from graph import graph
from state import state_manager, state_lock
from typing import TypedDict
import argparse
import json
import os
import sys
import requests

# Configure Dapr Checkpointer to use our custom state server
class DaprWorkflowGraphRunner:
    def __init__(self, graph):
        self.graph = graph
        self.state_url = "http://localhost:50000"
    
    def invoke(self, input_data: dict) -> dict:
        """Invoke the graph with Dapr-backed persistence"""
        config = {
            'configurable': {
                'thread_id': 'default',
                'checkpoint_ns': 'default',
            }
        }
        
        # Get current state from Dapr
        try:
            response = requests.get(f"{self.state_url}/state")
            if response.status_code == 200:
                state = response.json()
                print(f"Current state: {state}")
        except Exception as e:
            print(f"Error getting state: {e}")
            state = {}
        
        # Run the graph
        result = self.graph.invoke(input_data, config)
        
        # Update state after execution
        try:
            requests.post(
                f"{self.state_url}/state/set",
                json={"build_status": "completed"},
                headers={"Content-Type": "application/json"}
            )
        except Exception as e:
            print(f"Error updating state: {e}")
        
        return result


# The Dapr runner makes the LangGraph "Durable"
runner = DaprWorkflowGraphRunner(graph)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Sovereign Agent — autonomous task executor")
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

    result = runner.invoke({
        "project_root":      project_root,
        "backlog_path":      "backlog.md",
        "current_task":      "",
        "task_brief":        {},
        "build_status":      "idle",
        "test_logs":         "",
        "validator_verdict": {},
        "iteration_count":   0,
    })
    print(f"Result: {result}")
