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
# Architecture: fetch nodes use lightweight LLM to gather data, analysis nodes use powerful LLM to reason
NODE_MODELS: Dict[str, str] = {
    # Deep reasoning nodes - use powerful model for analysis
    "analyze_combined": GEMINI_3_PRO_PREVIEW,  # Single node that does ALL analysis with full context
    "update_sheets": GEMINI_3_PRO_PREVIEW,
    "alert_summary": GEMINI_3_PRO_PREVIEW,  # Generates summary-style notifications

    # Context fetch nodes - use fast model (Flash) to fetch data via GitHub MCP
    # These nodes ONLY fetch raw data, NO analysis - analysis is done by analyze_combined
    "fetch_veps": GEMINI_3_FLASH_PREVIEW,
    "check_activity": GEMINI_3_FLASH_PREVIEW,
    "check_compliance": GEMINI_3_FLASH_PREVIEW,
    "check_deadlines": GEMINI_3_FLASH_PREVIEW,
    "check_exceptions": GEMINI_3_FLASH_PREVIEW,
    "merge_vep_updates": GEMINI_3_FLASH_PREVIEW,  # Simple context merge, no deep reasoning

    # Utility nodes
    "send_email": GEMINI_3_FLASH_PREVIEW,
    "send_slack": GEMINI_3_FLASH_PREVIEW,
    "send_notifications": GEMINI_3_FLASH_PREVIEW,
    "save_state_cache": GEMINI_3_FLASH_PREVIEW,
    "scheduler": GEMINI_3_FLASH_PREVIEW,
    "run_monitoring": GEMINI_3_FLASH_PREVIEW,
}

# Email notification configuration
EMAIL_RECIPIENTS: List[str] = [
    "iholder@redhat.com",
]

# Agent operation interval (in seconds)
# After each fetch, the full pipeline runs: fetch_veps -> run_monitoring -> analyze_combined -> update_sheets -> alert_summary
FETCH_VEPS_INTERVAL_SECONDS: int = 1800  # 30 minutes
