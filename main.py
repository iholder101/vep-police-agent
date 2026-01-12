#!/home/iholder/Work/Repos/AI-experiments/vep-police-agent/.venv/bin/python
"""Main entry point for VEP governance agent."""

from datetime import datetime
from langchain_core.messages import HumanMessage
from graph import create_graph
from services.utils import log, invoke_agent


def get_initial_state():
    """Create initial state for the agent."""
    return {
        "messages": [HumanMessage(content="Initialize VEP governance agent")],
        "current_release": None,
        "release_schedule": None,
        "veps": [],
        "last_check_times": {},
        "next_tasks": [],
        "alerts": [],
        "sheets_need_update": False,
        "errors": [],
        "config_cache": {},
        "vep_updates_by_check": {},
        "sheet_config": {
            "create_new": True,  # Will create a new sheet on first run
            "sheet_name": "VEP Status",  # Optional: name for the sheet/tab
            # sheet_id will be set by update_sheets node after creation
        },
    }


def main():
    """Run the VEP governance agent."""
    log("Starting VEP governance agent", node="main")
    
    # Create the graph
    agent = create_graph()
    log("Graph created successfully", node="main")
    
    # Initialize state
    initial_state = get_initial_state()
    log("Initial state prepared", node="main")
    log(f"Sheet config: {initial_state['sheet_config']}", node="main")
    
    # Run the agent
    try:
        log("Invoking agent...", node="main")
        response = agent.invoke(initial_state)
        
        log("Agent execution completed", node="main")
        log(f"Final state keys: {list(response.keys())}", node="main")
        
        # Check if sheet was created
        sheet_config = response.get("sheet_config", {})
        if sheet_config.get("sheet_id"):
            log(f"✓ Sheet created/updated! Sheet ID: {sheet_config['sheet_id']}", node="main")
            log(f"  View at: https://docs.google.com/spreadsheets/d/{sheet_config['sheet_id']}/edit", node="main")
        else:
            log("Sheet ID not yet set - will be created on first update_sheets run", node="main")
            
    except Exception as e:
        log(f"Error running agent: {e}", node="main", level="ERROR")
        import traceback
        log(f"Traceback: {traceback.format_exc()}", node="main", level="ERROR")
        raise


if __name__ == "__main__":
    main()
