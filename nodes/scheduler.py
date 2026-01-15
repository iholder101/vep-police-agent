"""Scheduler node - determines which tasks to run based on timing and state."""

import os
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
    one_cycle = state.get("one_cycle", False)
    
    # In one-cycle mode or test-sheets debug mode, if we just completed update_sheets, don't schedule more tasks
    debug_mode = os.environ.get("DEBUG_MODE")
    if (one_cycle or debug_mode == "test-sheets") and state.get("_exit_after_sheets", False):
        mode_name = "test-sheets debug mode" if debug_mode == "test-sheets" else "one-cycle mode"
        log(f"{mode_name}: Sheet update completed, no more tasks scheduled", node="scheduler")
        return {
            "next_tasks": [],  # Clear tasks to exit
        }
    
    # Check if sheets need updating (priority check)
    sheets_need_update = state.get("sheets_need_update", False)
    if sheets_need_update:
        # Sheets have changes, prioritize update_sheets
        # But still check other tasks that are due
        next_tasks.append("update_sheets")
    
    # Default intervals (in seconds) - can be made configurable
    intervals = {
        "fetch_veps": 21600,  # 6 hours - discover/refresh VEPs from GitHub
        "run_monitoring": 3600,  # 1 hour - triggers all monitoring checks in parallel
        # Note: analyze_combined is NOT scheduled here - it's automatically triggered
        # by graph edges after all monitoring checks complete
        # Note: update_sheets is NOT scheduled by interval - only when sheets_need_update flag is True
    }
    
    now = datetime.now()
    
    # Check if VEPs list is empty - prioritize fetch_veps immediately
    veps = state.get("veps", [])
    if not veps:
        # VEPs list is empty, prioritize fetch_veps
        if "fetch_veps" not in next_tasks:
            next_tasks.insert(0, "fetch_veps")  # Add to front with highest priority
            log("VEPs list is empty, prioritizing fetch_veps", node="scheduler")
    
    # Check if fetch_veps is due (periodic refresh)
    last_check = last_check_times.get("fetch_veps")
    interval = intervals["fetch_veps"]
    
    if last_check is None:
        # Never run before - add to queue if not already there
        if "fetch_veps" not in next_tasks:
            next_tasks.append("fetch_veps")
    else:
        # Check if enough time has passed
        time_since = (now - last_check).total_seconds()
        if time_since >= interval:
            if "fetch_veps" not in next_tasks:
                next_tasks.append("fetch_veps")
    
    # Check if run_monitoring is due (skip in test-sheets debug mode or skip-monitoring mode)
    debug_mode = os.environ.get("DEBUG_MODE")
    skip_monitoring = state.get("skip_monitoring", False)
    if debug_mode != "test-sheets" and not skip_monitoring:
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
    else:
        if skip_monitoring:
            log("Skip-monitoring mode enabled - skipping run_monitoring", node="scheduler")
        elif debug_mode == "test-sheets":
            log("Debug mode 'test-sheets' enabled - skipping run_monitoring", node="scheduler", level="DEBUG")
    
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
    # Priority: fetch_veps (if VEPs empty or due), update_sheets (if needed), then run_monitoring
    # Note: analyze_combined is automatically triggered by graph edges, not scheduled
    if next_tasks:
        # Sort by priority: fetch_veps first (if VEPs empty or due), then update_sheets, then run_monitoring
        priority_order = ["fetch_veps", "update_sheets", "run_monitoring"]
        next_tasks = sorted(next_tasks, key=lambda x: priority_order.index(x) if x in priority_order else 999)
    
    # Log scheduling decision
    if next_tasks:
        log(f"Scheduled {len(next_tasks)} task(s): {', '.join(next_tasks[:3])}{'...' if len(next_tasks) > 3 else ''}", node="scheduler")
    else:
        log("No tasks scheduled, will wait", node="scheduler")
    
    return {
        "next_tasks": next_tasks,
    }
