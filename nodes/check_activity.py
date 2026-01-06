"""Activity monitoring node - checks for inactive VEPs and review lag."""

from datetime import datetime
from typing import Any
from state import VEPState
from utils import log


def check_activity_node(state: VEPState) -> Any:
    """Monitor VEP activity and flag inactive VEPs.
    
    This node:
    1. Fetches issue/PR updates from GitHub (using GitHub MCP)
    2. Fetches VEP tracking issues (using GitHub MCP)
    3. Checks last update time for each VEP
    4. Flags inactive VEPs (>2 weeks without updates)
    5. Monitors review lag times (>1 week without review)
    6. Tracks weekly SIG check-ins
    7. Updates VEP activity fields in state
    
    Note: This node fetches its own data from GitHub MCP - it's self-contained.
    """
    veps_count = len(state.get("veps", []))
    log(f"Checking activity for {veps_count} VEP(s)", node="check_activity")
    
    # TODO: Implement activity checking logic
    # 1. Fetch issue/PR updates from GitHub using GitHub MCP
    # 2. Fetch VEP tracking issues using GitHub MCP
    # 3. Calculate activity metrics (days since update, review lag)
    # 4. Update VEPInfo.activity fields
    # 5. Generate alerts for inactive VEPs
    # For now, just update last_check_times
    
    last_check_times = state.get("last_check_times", {})
    last_check_times["check_activity"] = datetime.now()
    
    alerts = state.get("alerts", [])
    # TODO: Add activity alerts
    
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
