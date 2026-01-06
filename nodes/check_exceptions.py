"""Exception tracking node - monitors exception requests."""

from datetime import datetime
from typing import Any
from state import VEPState
from utils import log


def check_exceptions_node(state: VEPState) -> Any:
    """Track exception requests and post-freeze work.
    
    This node:
    1. Fetches exception requests from issues (using GitHub MCP)
    2. Monitors for post-freeze work without exceptions
    3. Tracks exception requests (from mailing list or issues)
    4. Verifies exception completeness (justification, time period, impact)
    5. Updates VEP exception fields in state
    
    Note: This node fetches its own data from GitHub MCP - it's self-contained.
    """
    veps_count = len(state.get("veps", []))
    log(f"Checking exceptions for {veps_count} VEP(s)", node="check_exceptions")
    
    # TODO: Implement exception tracking logic
    # 1. Fetch exception requests from issues using GitHub MCP
    # 2. Check for post-freeze work without exceptions
    # 3. Verify exception completeness
    # 4. Update VEPInfo.exceptions fields
    # 5. Generate alerts for missing exceptions
    # For now, just update last_check_times
    
    last_check_times = state.get("last_check_times", {})
    last_check_times["check_exceptions"] = datetime.now()
    
    alerts = state.get("alerts", [])
    # TODO: Add exception alerts
    
    # Note: This node is triggered by graph edges (from run_monitoring), not by scheduler queue
    # So we don't need to manage next_tasks here
    
    # If we made changes (alerts, VEP updates), mark sheets for update
    sheets_need_update = state.get("sheets_need_update", False)
    # TODO: Set sheets_need_update = True if VEP data was modified
    
    return {
        "last_check_times": last_check_times,
        "alerts": alerts,
        "sheets_need_update": sheets_need_update,
    }
