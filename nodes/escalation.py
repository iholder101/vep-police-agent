"""Alert escalation logic - tracks persistence and escalates unresolved alerts.

Alerts that persist across multiple cycles get escalated in severity.
This creates pressure to resolve long-standing issues.
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from services.utils import log

# Escalation thresholds (cycles)
ESCALATION_THRESHOLDS = {
    "low": 5,        # low → medium after 5 cycles
    "medium": 3,     # medium → high after 3 cycles
    "high": 2,       # high → critical after 2 cycles
    "critical": 999, # critical stays critical
}

# Severity upgrade path
SEVERITY_UPGRADE = {
    "low": "medium",
    "medium": "high",
    "high": "critical",
    "critical": "critical",
}

# Persistence file location
PERSISTENCE_FILE = Path(__file__).parent.parent / "cache" / "alert_persistence.json"


def _load_persistence() -> Dict[str, Dict[str, Any]]:
    """Load alert persistence data from file."""
    if not PERSISTENCE_FILE.exists():
        return {}
    try:
        with open(PERSISTENCE_FILE, "r") as f:
            return json.load(f)
    except Exception as e:
        log(f"Failed to load persistence data: {e}", node="escalation", level="WARNING")
        return {}


def _save_persistence(data: Dict[str, Dict[str, Any]]) -> None:
    """Save alert persistence data to file."""
    try:
        PERSISTENCE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(PERSISTENCE_FILE, "w") as f:
            json.dump(data, f, indent=2, default=str)
    except Exception as e:
        log(f"Failed to save persistence data: {e}", node="escalation", level="WARNING")


def _make_alert_key(alert: Dict) -> str:
    """Create a unique key for an alert based on VEP and canonical issue type.

    Uses canonical types to ensure LLM text variations don't create duplicate
    persistence entries (e.g., "deadline" and "missed freeze" both map to
    "deadline_violation").
    """
    from nodes.alert_summary import _normalize_subject
    vep_id = alert.get("vep_id", 0)
    canonical = _normalize_subject(alert.get("subject", ""))
    return f"{vep_id}:{canonical}"


def escalate_alerts(alerts: List[Dict]) -> Tuple[List[Dict], Dict[str, Any]]:
    """Apply escalation logic to alerts based on persistence.

    Args:
        alerts: List of current alerts

    Returns:
        Tuple of (escalated_alerts, escalation_stats)
        - escalated_alerts: Alerts with potentially upgraded severity
        - escalation_stats: Summary of escalations applied
    """
    if not alerts:
        return [], {"escalated_count": 0, "new_count": 0, "resolved_count": 0}

    persistence = _load_persistence()
    current_keys = set()
    escalated_alerts = []
    escalation_count = 0
    new_count = 0

    now = datetime.now().isoformat()

    for alert in alerts:
        key = _make_alert_key(alert)
        current_keys.add(key)

        if key in persistence:
            # Existing alert - increment cycle count
            entry = persistence[key]
            entry["cycle_count"] = entry.get("cycle_count", 1) + 1
            entry["last_seen"] = now
            entry["last_severity"] = alert.get("severity", "low")

            # Check if escalation needed
            original_severity = alert.get("severity", "low")
            threshold = ESCALATION_THRESHOLDS.get(original_severity, 999)

            if entry["cycle_count"] >= threshold:
                new_severity = SEVERITY_UPGRADE.get(original_severity, original_severity)
                if new_severity != original_severity:
                    alert = alert.copy()
                    alert["severity"] = new_severity
                    alert["metadata"] = alert.get("metadata", {})
                    alert["metadata"]["escalated_from"] = original_severity
                    alert["metadata"]["cycles_persisted"] = entry["cycle_count"]
                    escalation_count += 1

                    log(
                        f"Escalated {key}: {original_severity} → {new_severity} "
                        f"(persisted {entry['cycle_count']} cycles)",
                        node="escalation"
                    )

                    # Reset counter after escalation to avoid repeated escalation
                    entry["cycle_count"] = 0
                    entry["escalated_at"] = now

            persistence[key] = entry
        else:
            # New alert - create persistence entry
            persistence[key] = {
                "first_seen": now,
                "last_seen": now,
                "cycle_count": 1,
                "original_severity": alert.get("severity", "low"),
                "last_severity": alert.get("severity", "low"),
            }
            new_count += 1

        escalated_alerts.append(alert)

    # Count resolved alerts (in persistence but not in current)
    resolved_keys = set(persistence.keys()) - current_keys
    resolved_count = len(resolved_keys)

    # Remove resolved alerts from persistence (they're no longer active)
    for key in resolved_keys:
        del persistence[key]

    if resolved_count > 0:
        log(f"Resolved {resolved_count} alert(s) (removed from persistence)", node="escalation")

    # Save updated persistence
    _save_persistence(persistence)

    stats = {
        "escalated_count": escalation_count,
        "new_count": new_count,
        "resolved_count": resolved_count,
        "total_tracked": len(persistence),
    }

    if escalation_count > 0:
        log(f"Escalation summary: {escalation_count} escalated, {new_count} new, {resolved_count} resolved", node="escalation")

    return escalated_alerts, stats


def clear_persistence() -> int:
    """Clear all persistence data.

    Returns number of entries cleared.
    """
    persistence = _load_persistence()
    count = len(persistence)
    if PERSISTENCE_FILE.exists():
        PERSISTENCE_FILE.unlink()
    log(f"Cleared {count} entries from alert persistence", node="escalation")
    return count


def get_persistence_summary() -> Dict[str, Any]:
    """Get summary of current persistence state."""
    persistence = _load_persistence()

    if not persistence:
        return {"total": 0, "by_severity": {}, "oldest_days": 0}

    # Group by last severity
    by_severity = {}
    oldest_date = None
    for key, entry in persistence.items():
        severity = entry.get("last_severity", "low")
        by_severity[severity] = by_severity.get(severity, 0) + 1

        first_seen = entry.get("first_seen")
        if first_seen:
            try:
                dt = datetime.fromisoformat(first_seen.replace("Z", "+00:00"))
                if oldest_date is None or dt < oldest_date:
                    oldest_date = dt
            except:
                pass

    oldest_days = 0
    if oldest_date:
        oldest_days = (datetime.now() - oldest_date.replace(tzinfo=None)).days

    return {
        "total": len(persistence),
        "by_severity": by_severity,
        "oldest_days": oldest_days,
    }
