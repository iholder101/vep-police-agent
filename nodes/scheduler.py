"""Scheduler node - determines which tasks to run based on timing and state."""

import os
from datetime import datetime, timedelta
from typing import Any, List
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
    from services.shutdown import is_shutdown_requested
    if is_shutdown_requested():
        return {"next_tasks": []}

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
    
    # Get interval from config
    fetch_veps_interval = config.FETCH_VEPS_INTERVAL_SECONDS
    
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
    
    # Check if using state cache (first cycle only)
    use_state_cache = state.get("use_state_cache", False)
    state_cache_used = state.get("_state_cache_used", False)

    # If using state cache and it hasn't been used yet, skip fetch/analyze
    if use_state_cache and not state_cache_used:
        veps = state.get("veps", [])
        if veps:
            log(f"Using cached state ({len(veps)} VEPs), skipping fetch/analysis pipeline", node="scheduler")
            if not state.get("skip_sheets", False):
                next_tasks.append("update_sheets")
            if not state.get("skip_update_board", False):
                next_tasks.append("update_project_board")
            next_tasks.append("alert_summary")
            # Set last_check_times for fetch_veps so the interval check doesn't
            # immediately schedule fetch_veps on the next scheduler call
            last_check_times["fetch_veps"] = now
            last_check_times["analyze_combined"] = now
            return {
                "next_tasks": next_tasks,
                "last_check_times": last_check_times,
                "_force_sheets_update": True,  # Force sheets update on first cache cycle
                "_state_cache_used": True,  # Mark cache as used
            }
        else:
            log("State cache requested but no VEPs in cache, falling back to normal flow", node="scheduler", level="WARNING")

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
        # Schedule update_sheets, update_project_board, and alert_summary after analysis
        log("First run: Scheduling update_sheets, update_project_board, and alert_summary", node="scheduler")
        next_tasks.append("update_sheets")
        if not state.get("skip_update_board", False):
            next_tasks.append("update_project_board")
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
        
        # Priority 1: Check if VEPs need analysis (were fetched but not analyzed)
        # This takes priority over scheduling fetch_veps again to avoid infinite loops
        if veps_need_analysis and not skip_monitoring:
            if fetch_veps_time:
                # VEPs were fetched but not analyzed - always schedule run_monitoring
                # (Don't re-fetch, as that creates an infinite loop)
                log("VEPs were fetched but not analyzed, scheduling run_monitoring", node="scheduler")
                if "run_monitoring" not in next_tasks:
                    next_tasks.append("run_monitoring")
            else:
                # No fetch_veps time recorded, schedule fetch_veps first
                log("VEPs need analysis but fetch_veps hasn't run, scheduling fetch_veps first", node="scheduler")
                next_tasks.append("fetch_veps")
        
        # Priority 2: Check if fetch_veps is due (only if VEPs don't need analysis)
        elif not veps_need_analysis:
            should_fetch_veps = _should_run_operation("fetch_veps", last_check_times, fetch_veps_interval, now, immediate_start=immediate_start)
            if should_fetch_veps:
                log(f"fetch_veps is due (interval: {fetch_veps_interval}s)", node="scheduler")
                next_tasks.append("fetch_veps")
                # After fetching VEPs, we MUST run monitoring and analysis before updating sheets/emails
                if not skip_monitoring:
                    log("Scheduling run_monitoring after fetch_veps to analyze VEPs", node="scheduler")
                    next_tasks.append("run_monitoring")
                # Note: update_sheets and alert_summary will be scheduled after analyze_combined completes
    
    # Also check if sheets_need_update flag is set (from analyze_combined)
    # Only add if VEPs have been analyzed (or if skip_monitoring is enabled)
    sheets_need_update = state.get("sheets_need_update", False)
    if sheets_need_update and "update_sheets" not in next_tasks:
        if not veps_need_analysis or skip_monitoring:
            log("sheets_need_update flag is set, adding update_sheets to queue", node="scheduler")
            next_tasks.append("update_sheets")
        else:
            log("sheets_need_update flag is set, but VEPs need analysis first - will schedule after analyze_combined", node="scheduler")
    
    # After analyze_combined completes, schedule update_sheets, update_project_board, and alert_summary
    # But only if they haven't run since analyze_combined completed
    analyze_combined_time = last_check_times.get("analyze_combined")
    if analyze_combined_time:
        update_sheets_time = last_check_times.get("update_sheets")
        update_board_time = last_check_times.get("update_project_board")
        alert_summary_time = last_check_times.get("alert_summary")

        # Schedule update_sheets if it hasn't run since analyze_combined
        if "update_sheets" not in next_tasks:
            if update_sheets_time is None or update_sheets_time < analyze_combined_time:
                log("analyze_combined completed, scheduling update_sheets", node="scheduler")
                next_tasks.append("update_sheets")

        # Schedule update_project_board if it hasn't run since analyze_combined
        if "update_project_board" not in next_tasks and not state.get("skip_update_board", False):
            if update_board_time is None or update_board_time < analyze_combined_time:
                log("analyze_combined completed, scheduling update_project_board", node="scheduler")
                next_tasks.append("update_project_board")

        # Schedule alert_summary if it hasn't run since analyze_combined
        if "alert_summary" not in next_tasks:
            if alert_summary_time is None or alert_summary_time < analyze_combined_time:
                log("analyze_combined completed, scheduling alert_summary", node="scheduler")
                next_tasks.append("alert_summary")
    
    # Log scheduling decision
    if next_tasks:
        log(f"Scheduled {len(next_tasks)} task(s): {', '.join(next_tasks)}", node="scheduler")
    else:
        log("No tasks scheduled", node="scheduler")
    
    return {
        "next_tasks": next_tasks,
    }
