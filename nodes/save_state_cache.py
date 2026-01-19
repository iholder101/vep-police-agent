"""Save state cache node - saves state to file for fast debug cycles."""

import json
from datetime import datetime
from pathlib import Path
from typing import Any
from state import VEPState
from services.utils import log

# Cache file location (in project root)
CACHE_FILE = Path(__file__).parent.parent / ".vep_state_cache.json"


def save_state_cache_node(state: VEPState) -> Any:
    """Save key state fields to cache file for fast debug/test cycles.

    This node runs after analyze_combined and saves state that can be
    reloaded with --use-state-cache to skip fetch/analyze on first cycle.

    Cached fields:
    - veps: List of VEPInfo objects
    - release_schedule: ReleaseSchedule object
    - current_release: Current release string
    - general_insights: List of insights from analysis
    - alerts: List of alert dicts
    """
    last_check_times = state.get("last_check_times", {})
    last_check_times["save_state_cache"] = datetime.now()

    try:
        veps = state.get("veps", [])
        release_schedule = state.get("release_schedule")

        # Serialize VEPs
        serialized_veps = []
        for vep in veps:
            if hasattr(vep, "model_dump"):
                serialized_veps.append(vep.model_dump(mode="json"))
            elif isinstance(vep, dict):
                serialized_veps.append(vep)
            else:
                log(f"Skipping non-serializable VEP: {type(vep)}", node="save_state_cache", level="WARNING")

        # Serialize release schedule
        serialized_schedule = None
        if release_schedule:
            if hasattr(release_schedule, "model_dump"):
                serialized_schedule = release_schedule.model_dump(mode="json")
            elif isinstance(release_schedule, dict):
                serialized_schedule = release_schedule

        cache_data = {
            "version": "1.0",
            "timestamp": datetime.now().isoformat(),
            "current_release": state.get("current_release"),
            "release_schedule": serialized_schedule,
            "veps": serialized_veps,
            "general_insights": state.get("general_insights", []),
            "alerts": state.get("alerts", []),
        }

        with open(CACHE_FILE, "w") as f:
            json.dump(cache_data, f, indent=2, default=str)

        log(f"State cache saved to {CACHE_FILE} ({len(serialized_veps)} VEPs)", node="save_state_cache")

    except Exception as e:
        log(f"Failed to save state cache: {e}", node="save_state_cache", level="ERROR")
        import traceback
        log(f"Traceback: {traceback.format_exc()}", node="save_state_cache", level="DEBUG")

    return {
        "last_check_times": last_check_times,
    }
