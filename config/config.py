"""Configuration values for VEP governance agent."""

from typing import Dict, List
from .util import FAST_MODEL, HEAVY_MODEL

# Default release version (used when release cannot be auto-detected)
DEFAULT_RELEASE: str = "v1.8"

# Known SIGs in the kubevirt project
KNOWN_SIGS: List[str] = ["compute", "network", "storage"]

# Project board numbers by release version
# Each kubevirt release has its own GitHub Project V2 board
VERSION_PROJECT_BOARDS: Dict[str, int] = {
    "1.6": 15,
    "1.7": 18,
    "1.8": 19,
}

# Model configuration per node type
# Architecture: fetch nodes use lightweight LLM to gather data, analysis nodes use powerful LLM to reason
NODE_MODELS: Dict[str, str] = {
    # Deep reasoning nodes - use powerful model for analysis
    "analyze_combined": HEAVY_MODEL,  # Single node that does ALL analysis with full context
    "update_sheets": HEAVY_MODEL,
    "alert_summary": HEAVY_MODEL,  # Generates summary-style notifications

    # Context fetch nodes - use fast model (Flash) to fetch data via GitHub MCP
    # These nodes ONLY fetch raw data, NO analysis - analysis is done by analyze_combined
    "fetch_veps": FAST_MODEL,
    "check_activity": FAST_MODEL,
    "check_compliance": FAST_MODEL,
    "check_deadlines": FAST_MODEL,
    "check_exceptions": FAST_MODEL,
    "merge_vep_updates": FAST_MODEL,  # Simple context merge, no deep reasoning

    # Utility/orchestration nodes omitted - they don't invoke LLMs.
    # (send_email, send_slack, send_notifications, save_state_cache, scheduler, run_monitoring)
    # If added here they'd just fall back to DEFAULT_MODEL via get_model_for_node(), which is unused.
}

# Email notification configuration
EMAIL_RECIPIENTS: List[str] = [
    "iholder@redhat.com",
]

# Agent operation interval (in seconds)
# After each fetch, the full pipeline runs: fetch_veps -> run_monitoring -> analyze_combined -> update_sheets -> alert_summary
FETCH_VEPS_INTERVAL_SECONDS: int = 4 * 60 * 60  # 4 hours

# LLM rate limit retry configuration
LLM_MAX_RETRIES: int = 3
LLM_INITIAL_DELAY: int = 2  # seconds
LLM_MAX_TIMEOUT: int = 180  # seconds - abort if exceeded

# Tool result truncation - prevents context window exhaustion from large GitHub API responses
# accumulating across iterations in the tool-calling loop
TOOL_RESULT_MAX_CHARS: int = 8000
