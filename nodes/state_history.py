"""State history management for tracking changes over time.

Provides functions to save timestamped snapshots and load previous states
for comparison, enabling real "changes since last" analysis.
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from services.utils import log

# History directory location
HISTORY_DIR = Path(__file__).parent.parent / "cache" / "history"

# Keep last N snapshots (24 = 24 hours at 1h intervals)
MAX_SNAPSHOTS = 24


def ensure_history_dir() -> Path:
    """Ensure history directory exists."""
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    return HISTORY_DIR


def get_snapshot_path(timestamp: Optional[datetime] = None) -> Path:
    """Get path for a snapshot file."""
    if timestamp is None:
        timestamp = datetime.now()
    filename = f"state_{timestamp.strftime('%Y%m%d_%H%M')}.json"
    return HISTORY_DIR / filename


def list_snapshots() -> List[Path]:
    """List all snapshot files sorted by date (oldest first)."""
    ensure_history_dir()
    snapshots = sorted(HISTORY_DIR.glob("state_*.json"))
    return snapshots


def get_latest_snapshot() -> Optional[Path]:
    """Get the most recent snapshot file."""
    snapshots = list_snapshots()
    if not snapshots:
        return None
    return snapshots[-1]


def get_previous_snapshot() -> Optional[Path]:
    """Get the second-most-recent snapshot (previous run).

    This is used for comparing current state to previous state.
    """
    snapshots = list_snapshots()
    if len(snapshots) < 2:
        return None
    return snapshots[-2]


def cleanup_old_snapshots() -> int:
    """Remove old snapshots keeping only MAX_SNAPSHOTS most recent.

    Returns number of snapshots removed.
    """
    snapshots = list_snapshots()
    if len(snapshots) <= MAX_SNAPSHOTS:
        return 0

    to_remove = snapshots[:-MAX_SNAPSHOTS]
    for snapshot in to_remove:
        try:
            snapshot.unlink()
            log(f"Removed old snapshot: {snapshot.name}", node="state_history")
        except Exception as e:
            log(f"Failed to remove {snapshot.name}: {e}", node="state_history", level="WARNING")

    return len(to_remove)


def save_snapshot(cache_data: Dict[str, Any]) -> Optional[Path]:
    """Save a timestamped snapshot of the state.

    Args:
        cache_data: The state data to save (same format as state_cache.json)

    Returns:
        Path to saved snapshot, or None on failure
    """
    ensure_history_dir()
    snapshot_path = get_snapshot_path()

    try:
        with open(snapshot_path, "w") as f:
            json.dump(cache_data, f, indent=2, default=str)

        log(f"Snapshot saved: {snapshot_path.name}", node="state_history")

        # Cleanup old snapshots
        removed = cleanup_old_snapshots()
        if removed > 0:
            log(f"Cleaned up {removed} old snapshots", node="state_history")

        return snapshot_path

    except Exception as e:
        log(f"Failed to save snapshot: {e}", node="state_history", level="ERROR")
        return None


def load_snapshot(snapshot_path: Path) -> Optional[Dict[str, Any]]:
    """Load a snapshot from disk.

    Args:
        snapshot_path: Path to the snapshot file

    Returns:
        Parsed snapshot data, or None on failure
    """
    try:
        with open(snapshot_path, "r") as f:
            return json.load(f)
    except Exception as e:
        log(f"Failed to load snapshot {snapshot_path}: {e}", node="state_history", level="ERROR")
        return None


def load_previous_snapshot() -> Optional[Dict[str, Any]]:
    """Load the previous snapshot for comparison.

    Returns:
        Previous snapshot data, or None if unavailable
    """
    prev_path = get_previous_snapshot()
    if prev_path is None:
        log("No previous snapshot available for comparison", node="state_history")
        return None

    data = load_snapshot(prev_path)
    if data:
        log(f"Loaded previous snapshot: {prev_path.name}", node="state_history")
    return data


def clear_all_history() -> int:
    """Clear all history snapshots.

    Returns number of files removed.
    """
    snapshots = list_snapshots()
    removed = 0
    for snapshot in snapshots:
        try:
            snapshot.unlink()
            removed += 1
        except Exception as e:
            log(f"Failed to remove {snapshot.name}: {e}", node="state_history", level="WARNING")

    if removed > 0:
        log(f"Cleared {removed} history snapshots", node="state_history")

    return removed


def extract_vep_summary(vep_data: Dict[str, Any]) -> Dict[str, Any]:
    """Extract key fields from VEP for comparison.

    Creates a normalized summary suitable for detecting meaningful changes.
    """
    return {
        "tracking_issue_id": vep_data.get("tracking_issue_id"),
        "name": vep_data.get("name"),
        "title": vep_data.get("title"),
        "status": vep_data.get("status"),
        "target_release": vep_data.get("target_release"),
        "milestone_status": vep_data.get("current_milestone", {}).get("status"),
        "exception_phase": vep_data.get("current_milestone", {}).get("exception_phase"),
        "compliance": {
            "vep_merged": vep_data.get("compliance", {}).get("vep_merged"),
            "all_sigs_signed_off": vep_data.get("compliance", {}).get("all_sigs_signed_off"),
            "labels_valid": vep_data.get("compliance", {}).get("labels_valid"),
        },
        "analysis_risk_level": vep_data.get("analysis", {}).get("risk_level"),
        "analysis_priority": vep_data.get("analysis", {}).get("priority"),
    }


def compare_snapshots(
    current: Dict[str, Any],
    previous: Dict[str, Any]
) -> Dict[str, Any]:
    """Compare two snapshots and identify changes.

    Returns:
        Dict with:
        - new_veps: VEPs in current but not previous
        - removed_veps: VEPs in previous but not current
        - changed_veps: VEPs with status/field changes
        - resolved_alerts: Alert types no longer present
        - new_alerts: Alert types newly present
        - unchanged_alerts: Alert types persisting
    """
    curr_veps = {v.get("tracking_issue_id"): v for v in current.get("veps", [])}
    prev_veps = {v.get("tracking_issue_id"): v for v in previous.get("veps", [])}

    curr_ids = set(curr_veps.keys())
    prev_ids = set(prev_veps.keys())

    # Identify new and removed VEPs
    new_vep_ids = curr_ids - prev_ids
    removed_vep_ids = prev_ids - curr_ids
    common_ids = curr_ids & prev_ids

    # Detect changes in common VEPs
    changed_veps = []
    for vep_id in common_ids:
        curr_summary = extract_vep_summary(curr_veps[vep_id])
        prev_summary = extract_vep_summary(prev_veps[vep_id])

        changes = {}
        for key in curr_summary:
            if curr_summary.get(key) != prev_summary.get(key):
                changes[key] = {
                    "from": prev_summary.get(key),
                    "to": curr_summary.get(key),
                }

        if changes:
            changed_veps.append({
                "vep_id": vep_id,
                "vep_name": curr_veps[vep_id].get("name"),
                "changes": changes,
            })

    # Compare alerts by (vep_id, canonical_type)
    def alert_key(alert: Dict) -> str:
        vep_id = alert.get("vep_id", 0)
        subject = alert.get("subject", "general")
        return f"{vep_id}:{subject}"

    curr_alerts = {alert_key(a): a for a in current.get("alerts", [])}
    prev_alerts = {alert_key(a): a for a in previous.get("alerts", [])}

    curr_alert_keys = set(curr_alerts.keys())
    prev_alert_keys = set(prev_alerts.keys())

    return {
        "new_veps": [curr_veps[vid] for vid in new_vep_ids],
        "removed_veps": [prev_veps[vid] for vid in removed_vep_ids],
        "changed_veps": changed_veps,
        "resolved_alerts": [prev_alerts[k] for k in (prev_alert_keys - curr_alert_keys)],
        "new_alerts": [curr_alerts[k] for k in (curr_alert_keys - prev_alert_keys)],
        "unchanged_alerts": [curr_alerts[k] for k in (curr_alert_keys & prev_alert_keys)],
        "previous_timestamp": previous.get("timestamp"),
        "current_timestamp": current.get("timestamp"),
    }
