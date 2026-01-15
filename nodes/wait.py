"""Wait node - sleeps or waits for events before returning to scheduler."""

import time
from typing import Any
from state import VEPState
from services.utils import log

# Configurable wait time (in seconds)
WAIT_INTERVAL = 60  # Default: 1 minute


def wait_node(state: VEPState) -> Any:
    """Wait for a period of time or for an event.
    
    Currently: Sleeps for WAIT_INTERVAL seconds
    Future: Can listen for GitHub webhooks/events, polling, etc.
    
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
    
    next_tasks = state.get("next_tasks", [])
    veps_count = len(state.get("veps", []))
    current_release = state.get("current_release", "unknown")
    sheets_need_update = state.get("sheets_need_update", False)
    
    log(
        f"Waiting {WAIT_INTERVAL}s | Release: {current_release} | "
        f"VEPs: {veps_count} | Pending tasks: {len(next_tasks)} | "
        f"Sheets need update: {sheets_need_update}",
        node="wait"
    )
    
    # TODO: Later, implement event-driven waiting (GitHub webhooks, polling, etc.)
    # For now: simple sleep with interruptible wait
    try:
        time.sleep(WAIT_INTERVAL)
    except KeyboardInterrupt:
        log("Wait interrupted by user", node="wait", level="INFO")
        raise
    
    # After waiting, return to scheduler (which will check what needs to run)
    return {}
