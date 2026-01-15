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
    """Wait until the next round hour (e.g., 13:00, 14:00, 15:00).
    
    Calculates time until next round hour and sleeps until then.
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
    next_round_hour = _get_next_round_hour(now)
    wait_seconds = (next_round_hour - now).total_seconds()
    
    next_tasks = state.get("next_tasks", [])
    veps_count = len(state.get("veps", []))
    current_release = state.get("current_release", "unknown")
    sheets_need_update = state.get("sheets_need_update", False)
    
    log(
        f"Waiting until {next_round_hour.strftime('%H:%M')} ({wait_seconds:.0f}s) | "
        f"Release: {current_release} | VEPs: {veps_count} | "
        f"Pending tasks: {len(next_tasks)} | Sheets need update: {sheets_need_update}",
        node="wait"
    )
    
    # Sleep until next round hour (with interruptible wait)
    try:
        time.sleep(wait_seconds)
    except KeyboardInterrupt:
        log("Wait interrupted by user", node="wait", level="INFO")
        raise
    
    # After waiting, return to scheduler (which will check what needs to run)
    return {}
