"""Wait node - waits until next round hour before returning to scheduler."""

import time
from datetime import datetime, timedelta
from typing import Any
from state import VEPState
from services.utils import log


def _get_next_round_hour(now: datetime) -> datetime:
    """Get the next round hour (e.g., if now is 13:45, return 14:00)."""
    next_hour = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    return next_hour


def wait_node(state: VEPState) -> Any:
    """Wait until the next round hour (e.g., 13:00, 14:00, 15:00) or next interval.
    
    If immediate_start is enabled, waits until current time + minimum interval.
    Otherwise, waits until next round hour.
    After waiting, returns to scheduler which will check what needs to run.
    """
    # In one-cycle mode or test-sheets debug mode, if sheet update completed, exit immediately
    import os
    debug_mode = os.environ.get("DEBUG_MODE")
    should_exit = (
        (state.get("one_cycle", False) or debug_mode == "test-sheets") and 
        state.get("_exit_after_sheets", False)
    )
    if should_exit:
        mode_name = "test-sheets debug mode" if debug_mode == "test-sheets" else "one-cycle mode"
        log(f"{mode_name}: Exiting immediately after sheet update (skipping wait)", node="wait")
        import sys
        sys.exit(0)
    
    now = datetime.now()
    immediate_start = state.get("immediate_start", False)
    
    if immediate_start:
        # In immediate-start mode, wait until current time + minimum interval (1 hour)
        # This ensures we check again after the interval has passed
        wait_until = now + timedelta(hours=1)
        wait_seconds = (wait_until - now).total_seconds()
        wait_description = f"{wait_until.strftime('%H:%M:%S')} (current time + 1h)"
    else:
        # Normal mode: wait until next round hour
        wait_until = _get_next_round_hour(now)
        wait_seconds = (wait_until - now).total_seconds()
        wait_description = f"{wait_until.strftime('%H:%M')} (next round hour)"
    
    next_tasks = state.get("next_tasks", [])
    veps_count = len(state.get("veps", []))
    current_release = state.get("current_release", "unknown")
    sheets_need_update = state.get("sheets_need_update", False)
    
    log(
        f"Waiting until {wait_description} ({wait_seconds:.0f}s) | "
        f"Release: {current_release} | VEPs: {veps_count} | "
        f"Pending tasks: {len(next_tasks)} | Sheets need update: {sheets_need_update}",
        node="wait"
    )
    
    # Sleep until target time (with interruptible wait)
    try:
        time.sleep(wait_seconds)
    except KeyboardInterrupt:
        log("Wait interrupted by user", node="wait", level="INFO")
        raise
    
    # After waiting, return to scheduler (which will check what needs to run)
    return {}
