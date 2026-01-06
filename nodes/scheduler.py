"""Scheduler node - determines which tasks to run based on timing and state."""

from datetime import datetime
from typing import Any
from state import VEPState
from services.utils import log


def scheduler_node(state: VEPState) -> Any:
    """Determine which tasks need to run based on timing and state.
    
    Checks how long since each task last ran and adds tasks to next_tasks
    if they're due for execution. Also checks if sheets need updating.
    """
    last_check_times = state.get("last_check_times", {})
    next_tasks = []
    
    # Check if sheets need updating (priority check)
    sheets_need_update = state.get("sheets_need_update", False)
    if sheets_need_update:
        # Sheets have changes, prioritize update_sheets
        # But still check other tasks that are due
        next_tasks.append("update_sheets")
    
    # Default intervals (in seconds) - can be made configurable
    intervals = {
        "run_monitoring": 3600,  # 1 hour - triggers all monitoring checks in parallel
        # Note: analyze_combined is NOT scheduled here - it's automatically triggered
        # by graph edges after all monitoring checks complete
        # Note: update_sheets is NOT scheduled by interval - only when sheets_need_update flag is True
    }
    
    now = datetime.now()
    
    # Check if run_monitoring is due
    last_check = last_check_times.get("run_monitoring")
    interval = intervals["run_monitoring"]
    
    if last_check is None:
        # Never run before - add to queue
        if "run_monitoring" not in next_tasks:
            next_tasks.append("run_monitoring")
    else:
        # Check if enough time has passed
        time_since = (now - last_check).total_seconds()
        if time_since >= interval:
            if "run_monitoring" not in next_tasks:
                next_tasks.append("run_monitoring")
    
    # If next_tasks already has items from previous scheduler run, use those
    # (This handles cases where nodes might have queued tasks, though in current
    # architecture nodes just return to scheduler without queuing)
    existing_tasks = state.get("next_tasks", [])
    if existing_tasks:
        # Use existing tasks, but if sheets need updating, prioritize that
        if sheets_need_update and "update_sheets" not in existing_tasks:
            existing_tasks.insert(0, "update_sheets")  # Add to front
        return {
            "next_tasks": existing_tasks,
        }
    
    # No existing queue, determine what needs to run
    # Priority: update_sheets (if needed) first, then run_monitoring
    # Note: analyze_combined is automatically triggered by graph edges, not scheduled
    if next_tasks:
        # Sort by priority: update_sheets first (if present), then run_monitoring
        priority_order = ["update_sheets", "run_monitoring"]
        next_tasks = sorted(next_tasks, key=lambda x: priority_order.index(x) if x in priority_order else 999)
    
    # Log scheduling decision
    if next_tasks:
        log(f"Scheduled {len(next_tasks)} task(s): {', '.join(next_tasks[:3])}{'...' if len(next_tasks) > 3 else ''}", node="scheduler")
    else:
        log("No tasks scheduled, will wait", node="scheduler")
    
    return {
        "next_tasks": next_tasks,
    }
