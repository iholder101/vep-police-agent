"""Alert summary node - formats alerts for Slack/email notifications.

This node receives alerts from analyze_combined and:
1. Prioritizes and limits to ~20 most significant alerts
2. Generates executive summary format for notifications
3. Highlights changes since last report
"""

import json
from datetime import datetime
from typing import Any, List, Dict
from state import VEPState
from services.utils import log
from services.llm_helper import invoke_llm_with_tools
from pydantic import BaseModel
from nodes.escalation import escalate_alerts


class Alert(BaseModel):
    """Represents a single alert."""
    subject: str  # Alert category (deadline, activity, compliance, exception, risk)
    severity: str  # "low", "medium", "high", "critical"
    vep_id: int = 0  # VEP tracking issue ID (0 for general alerts)
    vep_name: str  # VEP identifier or "general"
    vep_title: str = ""  # VEP title/description (truncated)
    title: str  # Alert headline
    message: str  # Detailed message
    metadata: Dict[str, Any] = {}


class AlertSummaryResponse(BaseModel):
    """Response model for alert summary formatting."""
    alerts: List[Alert] = []  # Prioritized/filtered alerts
    executive_summary: str = ""  # 2-3 sentence overview
    changes_since_last: str = ""  # What changed since last report
    summary_text: str = ""  # Full formatted summary for email


# Severity priority for sorting
SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}
MAX_ALERTS = 20  # Limit alerts for notifications

# Canonical alert types - map arbitrary LLM subjects to these
CANONICAL_TYPES = {
    # Deadline-related
    "deadline_violation": ["deadline", "freeze", "missed freeze", "enhancement freeze",
                           "code freeze", "post-freeze", "created post-freeze", "freeze violation",
                           "ef violation", "cf violation", "deadline approaching", "deadline risk"],
    # Activity-related
    "activity_issue": ["activity", "stale", "inactive", "no activity", "low activity",
                       "no updates", "dormant", "abandoned"],
    # Compliance-related
    "compliance_issue": ["compliance", "labels", "missing labels", "approval", "missing approval",
                         "lgtm", "sign-off", "template", "docs", "documentation"],
    # Exception-related
    "exception_required": ["exception", "exception pending", "exception required",
                           "post-freeze work", "needs exception"],
    # General/cross-domain
    "general_risk": ["risk", "blocker", "critical", "urgent", "cross-domain"],
}


def _normalize_subject(subject: str) -> str:
    """Map arbitrary subject to canonical type."""
    subject_lower = subject.lower().strip()

    for canonical, keywords in CANONICAL_TYPES.items():
        for keyword in keywords:
            if keyword in subject_lower:
                return canonical

    # Default to general_risk if no match
    return "general_risk"


def _consolidate_alerts(alerts: List[Dict]) -> List[Dict]:
    """Consolidate alerts by (vep_id, canonical_type).

    Merges multiple alerts about the same VEP and issue type into one,
    keeping the highest severity and combining messages.
    """
    if not alerts:
        return []

    # Group by (vep_id, canonical_type)
    grouped: Dict[str, List[Dict]] = {}
    for alert in alerts:
        vep_id = alert.get("vep_id", 0)
        canonical = _normalize_subject(alert.get("subject", ""))
        key = f"{vep_id}:{canonical}"

        if key not in grouped:
            grouped[key] = []
        grouped[key].append(alert)

    # Merge each group into a single alert
    consolidated = []
    for key, group in grouped.items():
        if len(group) == 1:
            # Single alert - just normalize the subject
            merged = group[0].copy()
            merged["subject"] = _normalize_subject(merged.get("subject", ""))
            consolidated.append(merged)
        else:
            # Multiple alerts - merge them
            merged = _merge_alert_group(group)
            consolidated.append(merged)

    log(f"Consolidated {len(alerts)} alerts to {len(consolidated)}", node="alert_summary")
    return consolidated


def _merge_alert_group(group: List[Dict]) -> Dict:
    """Merge a group of alerts about the same VEP/issue into one."""
    # Use highest severity
    severities = [a.get("severity", "low") for a in group]
    best_severity = min(severities, key=lambda s: SEVERITY_ORDER.get(s, 3))

    # Take first alert as base
    base = group[0].copy()
    base["severity"] = best_severity
    base["subject"] = _normalize_subject(base.get("subject", ""))

    # Combine unique titles/messages
    titles = list(dict.fromkeys(a.get("title", "") for a in group if a.get("title")))
    messages = list(dict.fromkeys(a.get("message", "") for a in group if a.get("message")))

    if len(titles) > 1:
        base["title"] = titles[0]  # Keep first, mention consolidation
        base["message"] = f"{messages[0]} (+ {len(group) - 1} related issues)"
    else:
        base["title"] = titles[0] if titles else ""
        base["message"] = messages[0] if messages else ""

    # Track original count
    base["metadata"] = base.get("metadata", {})
    base["metadata"]["consolidated_count"] = len(group)
    base["metadata"]["original_subjects"] = [a.get("subject", "") for a in group]

    return base


def alert_summary_node(state: VEPState) -> Any:
    """Format alerts for Slack/email notifications.

    This node:
    1. Takes alerts from analyze_combined
    2. Prioritizes by severity (critical > high > medium > low)
    3. Limits to ~20 most significant alerts
    4. Generates executive summary format:
       - Overall status (1-2 sentences)
       - Changes since last report
       - Top alerts by priority
       - Action items
    """
    veps = state.get("veps", [])
    incoming_alerts = state.get("alerts", [])
    general_insights = state.get("general_insights", [])

    log(f"Formatting alert summary: {len(incoming_alerts)} alert(s), {len(veps)} VEP(s)", node="alert_summary")

    last_check_times = state.get("last_check_times", {})
    last_check_times["alert_summary"] = datetime.now()

    if not veps:
        return {
            "last_check_times": last_check_times,
            "alerts": [],
            "alert_summary_text": "No VEPs to report on.",
        }

    # Check for mock mode
    mock_mode = state.get("mock_alert_summary", False)
    if mock_mode:
        log("Mock mode: Creating sample alerts", node="alert_summary")
        mocked_alerts = []
        for i, vep in enumerate(veps[:3]):
            mocked_alerts.append({
                "subject": ["deadline", "activity", "compliance"][i % 3],
                "severity": ["high", "medium", "high"][i % 3],
                "vep_id": vep.tracking_issue_id,
                "vep_name": vep.name,
                "vep_title": vep.title[:40] if vep.title else "",
                "title": f"Mock alert for {vep.name}",
                "message": "This is a mock alert for testing.",
                "metadata": {"mock": True},
            })
        return {
            "last_check_times": last_check_times,
            "alerts": mocked_alerts,
            "alert_summary_text": "Mock Alert Summary: Generated for testing.",
        }

    # Consolidate alerts by (vep_id, canonical_type) to remove duplicates
    consolidated_alerts = _consolidate_alerts(incoming_alerts)

    # Apply escalation logic for persistent alerts
    escalated_alerts, escalation_stats = escalate_alerts(consolidated_alerts)
    if escalation_stats["escalated_count"] > 0:
        log(f"Escalated {escalation_stats['escalated_count']} persistent alert(s)", node="alert_summary")

    # Sort and limit alerts
    sorted_alerts = sorted(
        escalated_alerts,
        key=lambda a: (
            SEVERITY_ORDER.get(a.get("severity", "low"), 3),
            a.get("vep_name", "zzz")
        )
    )
    limited_alerts = sorted_alerts[:MAX_ALERTS]

    # Count by severity (use escalated alerts for accurate counts)
    severity_counts = {}
    for alert in escalated_alerts:
        sev = alert.get("severity", "low")
        severity_counts[sev] = severity_counts.get(sev, 0) + 1

    # Build system prompt for summary formatting
    system_prompt = """You are formatting a VEP governance report for email/Slack.

Generate a concise, executive-style summary with these sections:

1. EXECUTIVE SUMMARY (2-3 sentences):
   - Overall release health status
   - Key risks or blockers
   - Immediate action needed (if any)

2. CHANGES SINCE LAST REPORT:
   IMPORTANT: Use ONLY the data in "detected_changes" field - DO NOT fabricate changes.
   - detected_changes.new_veps: VEPs added since last run
   - detected_changes.removed_veps: VEPs removed
   - detected_changes.changed_veps: VEPs with status/field changes
   - detected_changes.resolved_alerts: Alerts that are now resolved
   - detected_changes.new_alerts: New alerts this run
   - If detected_changes.is_first_run is true, say "First run - no prior data"
   - If all change lists are empty, say "No significant changes"

3. ALERT SUMMARY:
   Format alerts by severity (critical first):
   - Group by severity level
   - Show VEP name, issue, recommended action
   - Keep each alert to 1-2 lines

4. ACTION ITEMS:
   - List specific actions needed from maintainers
   - Prioritize by urgency

Keep the entire summary under 50 lines. Be concise and actionable.
Use plain text formatting suitable for both email and Slack."""

    # Prepare context with actual detected changes (not fabricated)
    release_schedule = state.get("release_schedule")
    detected_changes = state.get("detected_changes") or {}
    context = {
        "veps": [vep.model_dump(mode='json') for vep in veps],
        "alerts": limited_alerts,
        "general_insights": general_insights,
        "severity_counts": severity_counts,
        "raw_alert_count": len(incoming_alerts),  # Before consolidation
        "total_alerts": len(escalated_alerts),  # After consolidation and escalation
        "showing_alerts": len(limited_alerts),
        "escalation_stats": escalation_stats,  # How many alerts were escalated
        "release_schedule": release_schedule.model_dump(mode='json') if release_schedule else None,
        "current_release": state.get("current_release"),
        "detected_changes": detected_changes,  # Real changes from detect_changes node
    }

    user_prompt = f"""Format this VEP governance data into an executive summary:

{json.dumps(context, indent=2, default=str)}

Generate:
1. executive_summary: 2-3 sentence overview
2. changes_since_last: What's new/changed (or "No significant changes")
3. summary_text: Full formatted report for email/Slack

Keep alerts concise. Limit to the top {MAX_ALERTS} by severity."""

    # Use LLM to generate formatted summary
    result = invoke_llm_with_tools(
        "alert_summary",
        context,
        system_prompt,
        user_prompt,
        AlertSummaryResponse,
        mcp_names=()  # No MCP tools needed for formatting
    )

    # Convert to dicts for state
    formatted_alerts = [a.model_dump() for a in result.alerts] if result.alerts else limited_alerts

    log_msg = f"Summary: {len(formatted_alerts)} alert(s)"
    if severity_counts:
        counts_str = ", ".join(f"{k}: {v}" for k, v in sorted(severity_counts.items(), key=lambda x: SEVERITY_ORDER.get(x[0], 3)))
        log_msg += f" ({counts_str})"
    log(log_msg, node="alert_summary")

    output = {
        "last_check_times": last_check_times,
        "alert_summary_text": result.summary_text or _build_fallback_summary(formatted_alerts, veps, general_insights),
        "executive_summary": result.executive_summary,
        "changes_since_last": result.changes_since_last,
    }

    # Handle one-cycle mode exit
    if state.get("one_cycle", False) and state.get("_exit_after_sheets", False):
        log("One-cycle mode: Alert summary completed", node="alert_summary")
        next_tasks = state.get("next_tasks", [])
        if "alert_summary" in next_tasks:
            next_tasks.remove("alert_summary")
        output["next_tasks"] = next_tasks

    return output


def _build_fallback_summary(alerts: List[Dict], veps: list, insights: List[str]) -> str:
    """Build fallback summary if LLM doesn't return one."""
    lines = ["VEP Police Report", "=" * 40, ""]

    # Executive summary
    lines.append("SUMMARY")
    lines.append(f"Monitoring {len(veps)} VEP(s). {len(alerts)} alert(s) generated.")
    lines.append("")

    # Insights
    if insights:
        lines.append("KEY INSIGHTS")
        for insight in insights[:3]:
            lines.append(f"- {insight}")
        lines.append("")

    # Alerts by severity
    if alerts:
        lines.append("ALERTS")
        for sev in ["critical", "high", "medium", "low"]:
            sev_alerts = [a for a in alerts if a.get("severity") == sev]
            if sev_alerts:
                lines.append(f"\n{sev.upper()} ({len(sev_alerts)}):")
                for alert in sev_alerts[:5]:
                    vep_name = alert.get('vep_name', '?')
                    vep_title = alert.get('vep_title', '')
                    if vep_title and len(vep_title) > 30:
                        vep_title = vep_title[:27] + "..."
                    vep_display = f"{vep_name} ({vep_title})" if vep_title else vep_name
                    lines.append(f"  - {vep_display}: {alert.get('title', alert.get('message', 'No details'))}")
    else:
        lines.append("No alerts - all VEPs on track.")

    return "\n".join(lines)
