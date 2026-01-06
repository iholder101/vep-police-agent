"""Update sheets node - syncs state to Google Sheets."""

from datetime import datetime
from typing import Any
from state import VEPState
from utils import log


def update_sheets_node(state: VEPState) -> Any:
    """Update Google Sheets with current VEP state.
    
    This node:
    1. Reads current Google Sheets state
    2. Compares with graph state
    3. Updates sheets with new/changed VEP data
    4. Maintains sync between graph state and sheets
    
    Also removes the current task from next_tasks queue before returning to scheduler.
    """
    veps_count = len(state.get("veps", []))
    sheets_need_update = state.get("sheets_need_update", False)
    log(f"Updating Google Sheets | VEPs: {veps_count} | Need update: {sheets_need_update}", node="update_sheets")
    
    # TODO: Implement Google Sheets sync using MCP tools
    # For now, just update last_check_times and clear sheets_need_update flag
    
    last_check_times = state.get("last_check_times", {})
    last_check_times["update_sheets"] = datetime.now()
    
    # Remove current task from queue (it was just completed)
    next_tasks = state.get("next_tasks", [])
    if next_tasks and next_tasks[0] == "update_sheets":
        next_tasks = next_tasks[1:]
    
    return {
        "last_check_times": last_check_times,
        "sheets_need_update": False,  # Clear flag after update
        "next_tasks": next_tasks,  # Update queue
    }
