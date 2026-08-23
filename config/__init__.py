"""Configuration package for VEP governance agent."""

# Re-export config values
from .config import (
    EMAIL_RECIPIENTS,
    FETCH_VEPS_INTERVAL_SECONDS,
    NODE_MODELS,
    VERSION_PROJECT_BOARDS,
)

# Re-export constants
# Re-export helpers
from .util import (
    AVAILABLE_MODELS,
    DEFAULT_MODEL,
    FAST_MODEL,
    HEAVY_MODEL,
    board_search_patterns,
    get_all_node_models,
    get_email_recipients,
    get_model_for_node,
    get_project_board_for_version,
    is_fastest_model_enabled,
    set_fastest_model,
    set_node_model,
)
