"""Configuration package for VEP governance agent."""

# Re-export config values
from .config import (
    NODE_MODELS,
    EMAIL_RECIPIENTS,
    FETCH_VEPS_INTERVAL_SECONDS,
    VERSION_PROJECT_BOARDS,
)

# Re-export constants
from .util import (
    GEMINI_3_PRO_PREVIEW,
    GEMINI_3_FLASH_PREVIEW,
    GEMINI_2_5_FLASH,
    GEMINI_2_5_FLASH_LITE,
    GEMINI_2_5_PRO,
    GEMINI_2_0_FLASH,
    GEMINI_2_0_FLASH_LITE,
    DEFAULT_MODEL,
    AVAILABLE_MODELS,
    FAST_MODEL,
    HEAVY_MODEL,
)

# Re-export helpers
from .util import (
    set_fastest_model,
    is_fastest_model_enabled,
    get_model_for_node,
    set_node_model,
    get_all_node_models,
    get_email_recipients,
    get_project_board_for_version,
)
