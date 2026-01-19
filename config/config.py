"""Configuration values for VEP governance agent."""

from typing import Dict, List, Optional
from .util import GEMINI_3_PRO_PREVIEW, GEMINI_3_FLASH_PREVIEW, DEFAULT_MODEL

# Project board numbers by release version
# Each kubevirt release has its own GitHub Project V2 board
VERSION_PROJECT_BOARDS: Dict[str, int] = {
    "1.6": 15,
    "1.7": 18,
    "1.8": 19,
}


def get_project_board_for_version(version: Optional[str]) -> Optional[int]:
    """Get project board number for a version string like 'v1.8' or '1.8'.

    Args:
        version: Version string (e.g., "v1.8", "1.8", or None)

    Returns:
        Project board number if found, None otherwise
    """
    if not version:
        return None
    v = version.lstrip("v")
    return VERSION_PROJECT_BOARDS.get(v)

# Model configuration per node type
NODE_MODELS: Dict[str, str] = {
    # Deep reasoning nodes
    "analyze_combined": GEMINI_3_PRO_PREVIEW,
    "merge_vep_updates": GEMINI_3_PRO_PREVIEW,
    "update_sheets": GEMINI_3_PRO_PREVIEW,
    "alert_summary": GEMINI_3_PRO_PREVIEW,

    # Standard nodes - use fast model
    "fetch_veps": DEFAULT_MODEL,
    "check_activity": DEFAULT_MODEL,
    "check_compliance": DEFAULT_MODEL,
    "check_deadlines": DEFAULT_MODEL,
    "check_exceptions": DEFAULT_MODEL,
    "send_email": DEFAULT_MODEL,
    "send_slack": DEFAULT_MODEL,
    "send_notifications": DEFAULT_MODEL,
    "save_state_cache": DEFAULT_MODEL,
    "scheduler": DEFAULT_MODEL,
    "run_monitoring": DEFAULT_MODEL,
}

# Email notification configuration
EMAIL_RECIPIENTS: List[str] = [
    "iholder@redhat.com",
]

# Agent operation interval (in seconds)
# After each fetch, the full pipeline runs: fetch_veps -> run_monitoring -> analyze_combined -> update_sheets -> alert_summary
FETCH_VEPS_INTERVAL_SECONDS: int = 1800  # 30 minutes
