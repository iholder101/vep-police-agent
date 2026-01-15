"""Configuration file for VEP governance agent.

This module provides centralized configuration for the agent, including:
- Model selection per node type
- General agent settings
- Feature flags
- Email notification settings
"""

from typing import Dict, Optional, List

# Gemini Model name constants
GEMINI_3_PRO_PREVIEW = "gemini-3-pro-preview"
GEMINI_3_FLASH_PREVIEW = "gemini-3-flash-preview"
GEMINI_2_5_FLASH = "gemini-2.5-flash"
GEMINI_2_5_FLASH_LITE = "gemini-2.5-flash-lite"
GEMINI_2_5_PRO = "gemini-2.5-pro"
GEMINI_2_0_FLASH = "gemini-2.0-flash"
GEMINI_2_0_FLASH_LITE = "gemini-2.0-flash-lite"


# Default model for all nodes
DEFAULT_MODEL = GEMINI_3_FLASH_PREVIEW

# Model configuration per node type
# Nodes that require deeper reasoning can use more powerful models
NODE_MODELS: Dict[str, str] = {
    # Deep reasoning nodes - consider more powerful models
    "analyze_combined": GEMINI_3_PRO_PREVIEW,  # Holistic analysis with cross-check reasoning
    "merge_vep_updates": GEMINI_3_PRO_PREVIEW,  # Intelligent merging of parallel updates
    "fetch_veps": DEFAULT_MODEL,  # Complex VEP discovery and extraction
    
    # Standard check nodes - use fast model
    "check_activity": DEFAULT_MODEL,
    "check_compliance": DEFAULT_MODEL,
    "check_deadlines": DEFAULT_MODEL,
    "check_exceptions": DEFAULT_MODEL,
    
    # Sheet operations - use fast model
    "update_sheets": GEMINI_3_PRO_PREVIEW,
    
    # Alert and notification nodes
    "alert_summary": GEMINI_3_PRO_PREVIEW,  # Composes structured alerts from VEP analysis
    "send_email": DEFAULT_MODEL,  # Email sending (doesn't use LLM, but included for completeness)
    
    # Other nodes - use default
    "scheduler": DEFAULT_MODEL,
    "run_monitoring": DEFAULT_MODEL,
}

# Available models (for reference and validation)
AVAILABLE_MODELS = [
    GEMINI_3_PRO_PREVIEW,    # Latest pro model (preview)
    GEMINI_3_FLASH_PREVIEW,  # Fast, efficient (default)
    GEMINI_2_5_FLASH,        # Fast, efficient
    GEMINI_2_5_FLASH_LITE,   # Very fast, lightweight
    GEMINI_2_5_PRO,          # Powerful reasoning
    GEMINI_2_0_FLASH,        # Fast with good reasoning
    GEMINI_2_0_FLASH_LITE,   # Very fast, lightweight
]

# Global flag to force all nodes to use the fastest model
_USE_FASTEST_MODEL = False


def set_fastest_model(enabled: bool = True) -> None:
    """Enable or disable fastest model mode.
    
    When enabled, all nodes will use GEMINI_3_FLASH_PREVIEW regardless of their
    configured model in NODE_MODELS.
    
    Args:
        enabled: If True, force all nodes to use fastest model
    """
    global _USE_FASTEST_MODEL
    _USE_FASTEST_MODEL = enabled


def is_fastest_model_enabled() -> bool:
    """Check if fastest model mode is enabled.
    
    Returns:
        True if fastest model mode is enabled
    """
    return _USE_FASTEST_MODEL


def get_model_for_node(node_name: str) -> str:
    """Get the model name for a specific node.
    
    Args:
        node_name: Name of the node (e.g., "analyze_combined", "check_activity")
    
    Returns:
        Model name to use for this node. If fastest model mode is enabled,
        always returns GEMINI_3_FLASH_PREVIEW.
    """
    if _USE_FASTEST_MODEL:
        return GEMINI_3_FLASH_PREVIEW
    return NODE_MODELS.get(node_name, DEFAULT_MODEL)


def set_node_model(node_name: str, model: str) -> None:
    """Set the model for a specific node.
    
    Args:
        node_name: Name of the node
        model: Model name to use
    """
    if model not in AVAILABLE_MODELS:
        import warnings
        warnings.warn(
            f"Model '{model}' not in AVAILABLE_MODELS list. "
            f"Available models: {AVAILABLE_MODELS}. "
            f"Proceeding anyway - model may not be valid."
        )
    NODE_MODELS[node_name] = model


def get_all_node_models() -> Dict[str, str]:
    """Get all node model configurations.
    
    Returns:
        Dictionary mapping node names to model names
    """
    return NODE_MODELS.copy()


# Email notification configuration
# List of email addresses to receive VEP governance alerts
# Can be overridden by EMAIL_RECIPIENTS environment variable (comma-separated string)
EMAIL_RECIPIENTS: List[str] = [
    "iholder@redhat.com",
]

# Email service configuration
# Set RESEND_API_KEY environment variable to use Resend (easiest email service)
# Resend: https://resend.com - 3,000 emails/month free, developer-friendly
# If not set, will try system mail, then fall back to Ethereal Email (sandbox)
RESEND_API_KEY: Optional[str] = None

# Agent operation intervals (in seconds)
# These control how often different operations run
# All operations run on round hours (e.g., 13:00, 14:00, 15:00)
FETCH_VEPS_INTERVAL_SECONDS: int = 3600  # 1 hour - how often to fetch/update VEPs from GitHub
UPDATE_SHEETS_INTERVAL_SECONDS: int = 3600  # 1 hour - how often to update Google Sheets
ALERT_SUMMARY_INTERVAL_SECONDS: int = 3600  # 1 hour - how often to check if alerts need to be sent


def get_email_recipients() -> List[str]:
    """Get email recipients for alerts.
    
    Checks environment variable first (takes precedence), then falls back to config.py setting.
    Environment variable should be comma-separated string, config.py uses a list.
    
    Returns:
        List of email addresses (empty list if not configured)
    """
    import os
    # Check environment variable first (takes precedence)
    env_recipients = os.environ.get("EMAIL_RECIPIENTS")
    if env_recipients:
        # Parse comma-separated string from environment variable
        return [email.strip() for email in env_recipients.split(",") if email.strip()]
    # Fall back to config.py setting (already a list)
    return EMAIL_RECIPIENTS.copy() if EMAIL_RECIPIENTS else []


def get_resend_api_key() -> Optional[str]:
    """Get Resend API key for email sending.
    
    Checks RESEND_API_KEY environment variable first (takes precedence),
    then falls back to config.py setting.
    
    Returns:
        Resend API key string, or None if not configured
    """
    import os
    # Check environment variable first (takes precedence)
    env_key = os.environ.get("RESEND_API_KEY")
    if env_key:
        return env_key.strip()
    # Fall back to config.py setting
    return RESEND_API_KEY
