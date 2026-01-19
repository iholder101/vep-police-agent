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


class Alert(BaseModel):
    """Represents a single alert."""
    subject: str  # Alert category (deadline, activity, compliance, exception, risk)
    severity: str  # "low", "medium", "high", "critical"
    vep_id: int = 0  # VEP tracking issue ID (0 for general alerts)
    vep_name: str  # VEP identifier or "general"
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
                "title": f"Mock alert for {vep.name}",
                "message": f"This is a mock alert for testing.",
                "metadata": {"mock": True},
            })
        return {
            "last_check_times": last_check_times,
            "alerts": mocked_alerts,
            "alert_summary_text": "Mock Alert Summary: Generated for testing.",
        }

    # Sort and limit alerts
    sorted_alerts = sorted(
        incoming_alerts,
        key=lambda a: (
            SEVERITY_ORDER.get(a.get("severity", "low"), 3),
            a.get("vep_name", "zzz")
        )
    )
    limited_alerts = sorted_alerts[:MAX_ALERTS]

    # Count by severity
    severity_counts = {}
    for alert in incoming_alerts:
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
   - New VEPs added
   - Status changes (merged, at-risk, etc.)
   - Resolved issues
   - If no changes, say "No significant changes"

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

    # Prepare context
    release_schedule = state.get("release_schedule")
    context = {
        "veps": [vep.model_dump(mode='json') for vep in veps],
        "alerts": limited_alerts,
        "general_insights": general_insights,
        "severity_counts": severity_counts,
        "total_alerts": len(incoming_alerts),
        "showing_alerts": len(limited_alerts),
        "release_schedule": release_schedule.model_dump(mode='json') if release_schedule else None,
        "current_release": state.get("current_release"),
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
        "alerts": formatted_alerts,
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
                    lines.append(f"  - {alert.get('vep_name', '?')}: {alert.get('title', alert.get('message', 'No details'))}")
    else:
        lines.append("No alerts - all VEPs on track.")

    return "\n".join(lines)
