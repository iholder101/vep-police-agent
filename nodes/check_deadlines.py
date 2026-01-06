"""Deadline monitoring node - checks VEP deadlines and generates alerts."""

from datetime import datetime
from typing import Any
from state import VEPState
from utils import log


def check_deadlines_node(state: VEPState) -> Any:
    """Check VEP deadlines and generate alerts for approaching deadlines.
    
    This node:
    1. Fetches release schedule from kubevirt/sig-release (using GitHub MCP)
    2. Fetches VEP documents to get target releases (using GitHub MCP)
    3. Computes days until EF and CF from release schedule (not stored, computed on-demand)
    4. Checks each VEP's target release against deadlines
    5. Generates alerts for approaching deadlines (7d, 3d, 1d warnings)
    6. Flags VEPs that won't make deadlines (stores decisions, not computed values)
    
    Note: This node fetches its own data from GitHub MCP - it's self-contained.
    Note: Days until EF/CF are computed on-demand, not stored in state.
    """
    veps_count = len(state.get("veps", []))
    log(f"Checking deadlines for {veps_count} VEP(s)", node="check_deadlines")
    
    # TODO: Implement deadline checking logic
    # 1. Fetch release schedule from kubevirt/sig-release using GitHub MCP
    # 2. Fetch VEP documents from kubevirt/enhancements using GitHub MCP
    # 3. Compute days until EF/CF for each VEP (from release_schedule, not stored)
    # 4. Make decisions: flag VEPs at risk, generate alerts
    # 5. Store decisions/classifications in VEPInfo (not computed values)
    # For now, just update last_check_times
    
    last_check_times = state.get("last_check_times", {})
    last_check_times["check_deadlines"] = datetime.now()
    
    alerts = state.get("alerts", [])
    # TODO: Add deadline alerts
    
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
