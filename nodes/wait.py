"""Wait node - waits until next round hour before returning to scheduler."""

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
    
    from services.shutdown import wait_for_shutdown
    if wait_for_shutdown(timeout=wait_seconds):
        log("Wait interrupted by shutdown signal", node="wait", level="INFO")

    return {}
