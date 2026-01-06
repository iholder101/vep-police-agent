"""Analysis node - reasons about combined check results and generates alerts."""

from datetime import datetime
from typing import Any
from state import VEPState
from utils import log


def analyze_combined_node(state: VEPState) -> Any:
    """Analyze combined results from all monitoring checks.
    
    This node:
    1. Reads results from all check nodes (deadlines, activity, compliance, exceptions)
    2. Reasons about combinations (e.g., "low activity + close deadline = urgent")
    3. Uses LLM for complex reasoning when needed
    4. Generates alerts based on combined context
    5. Updates VEP analysis fields with insights
    
    Examples of cross-check reasoning:
    - Low activity + far deadline = OK (not urgent)
    - Low activity + close deadline = URGENT (needs attention)
    - Compliance issues + close deadline = CRITICAL
    - Multiple compliance flags failing = needs immediate review
    """
    veps = state.get("veps", [])
    log(f"Analyzing combined results for {len(veps)} VEP(s)", node="analyze_combined")
    
    # TODO: Implement holistic analysis logic
    # For now, just update last_check_times
    
    last_check_times = state.get("last_check_times", {})
    last_check_times["analyze_combined"] = datetime.now()
    
    alerts = state.get("alerts", [])
    # TODO: Generate alerts based on combined check results
    # Example: Check each VEP's combination of deadline proximity + activity + compliance
    
    # Note: This node is triggered by graph edges (from all check nodes), not by scheduler queue
    # So we don't need to manage next_tasks here
    
    # Analysis may have updated VEP data, mark sheets for update
    sheets_need_update = state.get("sheets_need_update", False)
    # TODO: Set sheets_need_update = True if analysis generated alerts or updated VEPs
    
    return {
        "last_check_times": last_check_times,
        "alerts": alerts,
        "sheets_need_update": sheets_need_update,
    }
