"""Detect changes node - compares current state to previous run.

This node provides real "changes since last" data instead of LLM fabrication.
It loads the previous snapshot, compares it to the current state, and stores
the detected changes in state for alert_summary to use.
"""

from datetime import datetime
from typing import Any, Dict, List
from state import VEPState
from services.utils import log
from nodes.state_history import (
    load_previous_snapshot,
    compare_snapshots,
    extract_vep_summary,
)


def detect_changes_node(state: VEPState) -> Any:
    """Detect changes between current state and previous run.

    Compares:
    - New/removed VEPs
    - Status changes in existing VEPs
    - Resolved vs new vs persisting alerts

    Stores results in state.detected_changes for use by alert_summary.
    """
    last_check_times = state.get("last_check_times", {})
    last_check_times["detect_changes"] = datetime.now()

    # Build current state snapshot (same format as cache)
    veps = state.get("veps", [])
    release_schedule = state.get("release_schedule")

    current_veps = []
    for vep in veps:
        if hasattr(vep, "model_dump"):
            current_veps.append(vep.model_dump(mode="json"))
        elif isinstance(vep, dict):
            current_veps.append(vep)

    current_state = {
        "timestamp": datetime.now().isoformat(),
        "veps": current_veps,
        "alerts": state.get("alerts", []),
        "release_schedule": release_schedule.model_dump(mode="json") if release_schedule and hasattr(release_schedule, "model_dump") else release_schedule,
        "current_release": state.get("current_release"),
    }

    # Load previous snapshot
    previous_state = load_previous_snapshot()

    if previous_state is None:
        log("No previous snapshot - first run, no changes to detect", node="detect_changes")
        return {
            "last_check_times": last_check_times,
            "detected_changes": {
                "is_first_run": True,
                "new_veps": [],
                "removed_veps": [],
                "changed_veps": [],
                "resolved_alerts": [],
                "new_alerts": [],
                "unchanged_alerts": [],
                "previous_timestamp": None,
                "current_timestamp": current_state["timestamp"],
            },
        }

    # Compare snapshots
    changes = compare_snapshots(current_state, previous_state)
    changes["is_first_run"] = False

    # Log summary
    summary_parts = []
    if changes["new_veps"]:
        summary_parts.append(f"+{len(changes['new_veps'])} new VEPs")
    if changes["removed_veps"]:
        summary_parts.append(f"-{len(changes['removed_veps'])} removed")
    if changes["changed_veps"]:
        summary_parts.append(f"~{len(changes['changed_veps'])} changed")
    if changes["resolved_alerts"]:
        summary_parts.append(f"{len(changes['resolved_alerts'])} alerts resolved")
    if changes["new_alerts"]:
        summary_parts.append(f"{len(changes['new_alerts'])} new alerts")

    if summary_parts:
        log(f"Changes detected: {', '.join(summary_parts)}", node="detect_changes")
    else:
        log("No significant changes since last run", node="detect_changes")

    # Simplify for storage (don't store full VEP data in changes)
    simplified_changes = {
        "is_first_run": False,
        "new_veps": [{"id": v.get("tracking_issue_id"), "name": v.get("name"), "title": v.get("title")} for v in changes["new_veps"]],
        "removed_veps": [{"id": v.get("tracking_issue_id"), "name": v.get("name"), "title": v.get("title")} for v in changes["removed_veps"]],
        "changed_veps": changes["changed_veps"],  # Already simplified
        "resolved_alerts": _simplify_alerts(changes["resolved_alerts"]),
        "new_alerts": _simplify_alerts(changes["new_alerts"]),
        "unchanged_alert_count": len(changes["unchanged_alerts"]),
        "previous_timestamp": changes["previous_timestamp"],
        "current_timestamp": changes["current_timestamp"],
    }

    return {
        "last_check_times": last_check_times,
        "detected_changes": simplified_changes,
    }


def _simplify_alerts(alerts: List[Dict]) -> List[Dict]:
    """Extract key fields from alerts for change summary."""
    return [
        {
            "vep_id": a.get("vep_id"),
            "vep_name": a.get("vep_name"),
            "vep_title": a.get("vep_title"),
            "subject": a.get("subject"),
            "severity": a.get("severity"),
            "title": a.get("title"),
        }
        for a in alerts
    ]
