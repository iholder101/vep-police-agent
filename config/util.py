"""Constants and helper functions for configuration."""

import os
from typing import Dict, List, Optional

# Gemini Model constants
GEMINI_3_PRO_PREVIEW = "gemini-3-pro-preview"
GEMINI_3_FLASH_PREVIEW = "gemini-3-flash-preview"
GEMINI_2_5_FLASH = "gemini-2.5-flash"
GEMINI_2_5_FLASH_LITE = "gemini-2.5-flash-lite"
GEMINI_2_5_PRO = "gemini-2.5-pro"
GEMINI_2_0_FLASH = "gemini-2.0-flash"
GEMINI_2_0_FLASH_LITE = "gemini-2.0-flash-lite"

# Model tier constants for node configuration
FAST_MODEL = GEMINI_3_FLASH_PREVIEW
HEAVY_MODEL = GEMINI_3_PRO_PREVIEW

DEFAULT_MODEL = GEMINI_3_FLASH_PREVIEW

AVAILABLE_MODELS = [
    GEMINI_3_PRO_PREVIEW,
    GEMINI_3_FLASH_PREVIEW,
    GEMINI_2_5_FLASH,
    GEMINI_2_5_FLASH_LITE,
    GEMINI_2_5_PRO,
    GEMINI_2_0_FLASH,
    GEMINI_2_0_FLASH_LITE,
]

# Global flag for fastest model mode
_USE_FASTEST_MODEL = False


def set_fastest_model(enabled: bool = True) -> None:
    """Force all nodes to use the fastest model."""
    global _USE_FASTEST_MODEL
    _USE_FASTEST_MODEL = enabled


def is_fastest_model_enabled() -> bool:
    """Check if fastest model mode is enabled."""
    return _USE_FASTEST_MODEL


def get_model_for_node(node_name: str) -> str:
    """Get the model name for a specific node."""
    from .config import NODE_MODELS
    if _USE_FASTEST_MODEL:
        return GEMINI_3_FLASH_PREVIEW
    return NODE_MODELS.get(node_name, DEFAULT_MODEL)


def set_node_model(node_name: str, model: str) -> None:
    """Set the model for a specific node."""
    from .config import NODE_MODELS
    if model not in AVAILABLE_MODELS:
        import warnings
        warnings.warn(f"Model '{model}' not in AVAILABLE_MODELS. Proceeding anyway.")
    NODE_MODELS[node_name] = model


def get_all_node_models() -> Dict[str, str]:
    """Get all node model configurations."""
    from .config import NODE_MODELS
    return NODE_MODELS.copy()


def get_email_recipients() -> List[str]:
    """Get email recipients (env var takes precedence over config)."""
    from .config import EMAIL_RECIPIENTS
    env_recipients = os.environ.get("EMAIL_RECIPIENTS")
    if env_recipients:
        return [email.strip() for email in env_recipients.split(",") if email.strip()]
    return EMAIL_RECIPIENTS.copy() if EMAIL_RECIPIENTS else []


def get_project_board_for_version(version: Optional[str]) -> Optional[int]:
    """Get project board number for a version string like 'v1.8' or '1.8'.

    Args:
        version: Version string (e.g., "v1.8", "1.8", or None)

    Returns:
        Project board number if found, None otherwise
    """
    from .config import VERSION_PROJECT_BOARDS
    if not version:
        return None
    v = version.lstrip("v")
    return VERSION_PROJECT_BOARDS.get(v)


