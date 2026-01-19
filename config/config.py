"""Configuration values for VEP governance agent."""

from typing import Dict, List
from .util import GEMINI_3_PRO_PREVIEW, GEMINI_3_FLASH_PREVIEW, DEFAULT_MODEL

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
