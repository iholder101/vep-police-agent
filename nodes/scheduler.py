"""Scheduler node - determines which tasks to run based on timing and state."""

import os
import math
from datetime import datetime, timedelta
from typing import Any, List, Literal
from state import VEPState
from services.utils import log
import config


def _get_next_round_hour(now: datetime) -> datetime:
    """Get the next round hour (e.g., if now is 13:45, return 14:00).
    
    Args:
        now: Current datetime
        
    Returns:
        Next round hour datetime
    """
    # Round up to next hour
    next_hour = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    return next_hour


def _is_round_hour(now: datetime) -> bool:
    """Check if current time is at a round hour (e.g., 13:00, 14:00).
    
    Args:
        now: Current datetime
        
    Returns:
        True if current time is at a round hour (minute=0, second=0)
    """
    return now.minute == 0 and now.second == 0


def _should_run_operation(
    operation_name: str,
    last_check_times: dict,
    interval_seconds: int,
    now: datetime,
    is_first_run: bool = False,
    immediate_start: bool = False
) -> bool:
    """Check if an operation should run based on interval and round-hour timing.
    
    Args:
        operation_name: Name of the operation (e.g., "fetch_veps")
        last_check_times: Dictionary of last check times
        interval_seconds: Interval in seconds
        now: Current datetime
        is_first_run: If True, always return True (for first run)
        immediate_start: If True, use current time + interval instead of round hours
        
    Returns:
        True if operation should run
    """
    if is_first_run:
        return True
    
    last_check = last_check_times.get(operation_name)
    if last_check is None:
        # Never run before
        if immediate_start:
            return True  # Run immediately if immediate_start is enabled
        return _is_round_hour(now)  # Otherwise wait for round hour
    
    # Check if enough time has passed
    time_since = (now - last_check).total_seconds()
    if time_since < interval_seconds:
        return False
    
    # Enough time has passed
    if immediate_start:
        return True  # Run immediately if immediate_start is enabled
    return _is_round_hour(now)  # Otherwise check if we're at a round hour


def scheduler_node(state: VEPState) -> Any:
    """Determine which tasks need to run based on timing and state.
    
    Flow:
    1. When VEPs are fetched, they MUST go through the analysis pipeline:
       fetch_veps -> run_monitoring -> merge_vep_updates -> analyze_combined
    2. After analyze_combined completes, scheduler can schedule:
       - update_sheets (if needed)
       - alert_summary (to check for alerts)
    3. These can run in parallel after analysis is complete.
    
    The scheduler ensures the analysis pipeline runs before updating sheets or sending emails.
    """
    last_check_times = state.get("last_check_times", {})
    next_tasks: List[str] = []
    one_cycle = state.get("one_cycle", False)
    immediate_start = state.get("immediate_start", False)
    skip_monitoring = state.get("skip_monitoring", False)
    now = datetime.now()
    
    # In one-cycle mode or test-sheets debug mode, if we just completed update_sheets, don't schedule more tasks
    debug_mode = os.environ.get("DEBUG_MODE")
    if (one_cycle or debug_mode == "test-sheets") and state.get("_exit_after_sheets", False):
        mode_name = "test-sheets debug mode" if debug_mode == "test-sheets" else "one-cycle mode"
        log(f"{mode_name}: Sheet update completed, no more tasks scheduled", node="scheduler")
        return {
            "next_tasks": [],
        }
    
    # Check if this is the first run (no operations have run yet)
    is_first_run = len(last_check_times) == 0
    
    # Get intervals from config
    fetch_veps_interval = config.FETCH_VEPS_INTERVAL_SECONDS
    update_sheets_interval = config.UPDATE_SHEETS_INTERVAL_SECONDS
    alert_summary_interval = config.ALERT_SUMMARY_INTERVAL_SECONDS
    
    # Check if VEPs were just fetched (fetch_veps ran more recently than analyze_combined)
    fetch_veps_time = last_check_times.get("fetch_veps")
    analyze_combined_time = last_check_times.get("analyze_combined")
    veps_need_analysis = False
    if fetch_veps_time and analyze_combined_time:
        # If fetch_veps ran after analyze_combined, VEPs need analysis
        veps_need_analysis = fetch_veps_time > analyze_combined_time
    elif fetch_veps_time and not analyze_combined_time:
        # VEPs were fetched but never analyzed
        veps_need_analysis = True
    
    # First run: Fetch VEPs, run monitoring, then update sheets and check alerts
    if is_first_run:
        veps = state.get("veps", [])
        if not veps:
            log("First run: VEPs list is empty, scheduling fetch_veps", node="scheduler")
            next_tasks.append("fetch_veps")
        # After fetching, we need to run monitoring and analysis
        if not veps or veps_need_analysis:
            if not skip_monitoring:
                log("First run: Scheduling run_monitoring to analyze VEPs", node="scheduler")
                next_tasks.append("run_monitoring")
            else:
                log("First run: Skip-monitoring enabled, skipping analysis pipeline", node="scheduler")
        # Schedule update_sheets and alert_summary after analysis (or immediately if skip_monitoring)
        log("First run: Scheduling update_sheets and alert_summary", node="scheduler")
        next_tasks.append("update_sheets")
        next_tasks.append("alert_summary")
    else:
        # If immediate_start is enabled, don't check for round hour - use interval-based timing
        if not immediate_start:
            # Check if we're at a round hour
            if not _is_round_hour(now):
                next_round_hour = _get_next_round_hour(now)
                wait_seconds = (next_round_hour - now).total_seconds()
                log(f"Not at round hour. Next round hour: {next_round_hour.strftime('%H:%M')} (waiting {wait_seconds:.0f}s)", node="scheduler")
                return {
                    "next_tasks": ["wait"],  # Wait until next round hour
                }
            log(f"Round hour reached: {now.strftime('%H:%M')}", node="scheduler")
        else:
            log(f"Immediate-start mode: Using interval-based timing (current time: {now.strftime('%H:%M:%S')})", node="scheduler")
        
        # Priority 1: Check if fetch_veps is due
        should_fetch_veps = _should_run_operation("fetch_veps", last_check_times, fetch_veps_interval, now, immediate_start=immediate_start)
        if should_fetch_veps:
            log(f"fetch_veps is due (interval: {fetch_veps_interval}s)", node="scheduler")
            next_tasks.append("fetch_veps")
            # After fetching VEPs, we MUST run monitoring and analysis before updating sheets/emails
            if not skip_monitoring:
                log("Scheduling run_monitoring after fetch_veps to analyze VEPs", node="scheduler")
                next_tasks.append("run_monitoring")
            # Note: update_sheets and alert_summary will be scheduled after analyze_combined completes
        
        # Priority 2: Check if VEPs need analysis (were fetched but not analyzed)
        elif veps_need_analysis and not skip_monitoring:
            log("VEPs were fetched but not analyzed, scheduling fetch_veps to refresh and then analyze", node="scheduler")
            next_tasks.append("fetch_veps")
            # After fetching, run_monitoring will be automatically scheduled
        
        # Priority 3: Check if update_sheets or alert_summary are due
        # Only schedule these if VEPs have been analyzed (or if there are no VEPs to analyze)
        elif not veps_need_analysis:
            # Check update_sheets
            should_update_sheets = _should_run_operation("update_sheets", last_check_times, update_sheets_interval, now, immediate_start=immediate_start)
            if should_update_sheets:
                log(f"update_sheets is due (interval: {update_sheets_interval}s)", node="scheduler")
                next_tasks.append("update_sheets")
            
            # Check alert_summary
            should_alert_summary = _should_run_operation("alert_summary", last_check_times, alert_summary_interval, now, immediate_start=immediate_start)
            if should_alert_summary:
                log(f"alert_summary is due (interval: {alert_summary_interval}s)", node="scheduler")
                next_tasks.append("alert_summary")
    
    # Also check if sheets_need_update flag is set (from analyze_combined)
    # Only add if VEPs have been analyzed (or if skip_monitoring is enabled)
    sheets_need_update = state.get("sheets_need_update", False)
    if sheets_need_update and "update_sheets" not in next_tasks:
        if not veps_need_analysis or skip_monitoring:
            log("sheets_need_update flag is set, adding update_sheets to queue", node="scheduler")
            next_tasks.append("update_sheets")
        else:
            log("sheets_need_update flag is set, but VEPs need analysis first - will schedule after analyze_combined", node="scheduler")
    
    # Note: analyze_combined now routes directly to both update_sheets and alert_summary in parallel
    # So we don't need to schedule them here - they run automatically after analyze_combined
    
    # Log scheduling decision
    if next_tasks:
        log(f"Scheduled {len(next_tasks)} task(s): {', '.join(next_tasks)}", node="scheduler")
    else:
        log("No tasks scheduled", node="scheduler")
    
    return {
        "next_tasks": next_tasks,
    }
