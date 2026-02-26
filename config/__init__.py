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
