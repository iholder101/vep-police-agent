"""Send Slack node - sends alerts via Slack Incoming Webhook."""

import os
from datetime import UTC, datetime
from typing import Any

import requests

from nodes.alert_formatting import (
    build_slack_table,
    get_phase_context_summary,
)
from services.indexer import create_indexed_context
from services.utils import log
from state import VEPState

# Severity color mapping for Slack attachments
SEVERITY_COLORS = {
    "critical": "#d32f2f",  # Red
    "high": "#f57c00",      # Orange
    "medium": "#fbc02d",    # Yellow
    "low": "#388e3c",       # Green
}


def _format_slack_message(alerts: list[dict[str, Any]], alert_summary_text: str, table_rows: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Format alerts as a Slack Block Kit message with color-coded attachments.

    Args:
        alerts: List of alert dicts
        alert_summary_text: Summary text from alert_summary node
        table_rows: Pre-built VEP summary table rows from analyze_combined

    Returns a Slack message payload with attachments for severity-based coloring.
    """
    # Get phase context
    indexed_context = create_indexed_context(cache_max_age_minutes=60)
    phase_ctx = get_phase_context_summary(indexed_context)
    if table_rows is None:
        table_rows = []
    high_risk_count = sum(1 for row in table_rows if row["urgency"] == "RED")

    # Group alerts by severity to determine the overall color
    severity_priority = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    highest_severity = "low"
    for alert in alerts:
        severity = alert.get("severity", "low")
        if severity_priority.get(severity, 3) < severity_priority.get(highest_severity, 3):
            highest_severity = severity

    # Build alert text - group by subject (not "type" - that was a bug)
    alerts_by_subject = {}
    for alert in alerts:
        subject_key = alert.get("subject", "other")
        if subject_key not in alerts_by_subject:
            alerts_by_subject[subject_key] = []
        alerts_by_subject[subject_key].append(alert)

    # Format alert details
    alert_lines = []
    for subject_key, subject_alerts in alerts_by_subject.items():
        subject_title = subject_key.replace("_", " ").title()
        alert_lines.append(f"*{subject_title}* ({len(subject_alerts)} alert(s))")
        for alert in subject_alerts:
            vep_id = alert.get("vep_id", "?")
            vep_name = alert.get("vep_name", "?")
            vep_title = alert.get("vep_title", "")
            # Truncate title to 30 chars for readability
            if vep_title and len(vep_title) > 30:
                vep_title = vep_title[:27] + "..."
            title = alert.get("title", "")
            severity = alert.get("severity", "low")
            severity_emoji = {"critical": ":red_circle:", "high": ":large_orange_circle:", "medium": ":large_yellow_circle:", "low": ":white_circle:"}.get(severity, ":white_circle:")
            # Format: :emoji: <link|vep-0181> (Short Title): alert headline
            vep_url = f"https://github.com/kubevirt/enhancements/issues/{vep_id}"
            vep_link = f"<{vep_url}|{vep_name}>"
            vep_display = f"{vep_link} ({vep_title})" if vep_title else vep_link
            alert_lines.append(f"  {severity_emoji} {vep_display}: {title}")

    alert_text = "\n".join(alert_lines)

    # Build phase context text
    phase_text = f"*{phase_ctx['release']} {phase_ctx['phase_display']}*"
    if phase_ctx['deadline_text']:
        phase_text += f" | {phase_ctx['deadline_text']}"
    if high_risk_count > 0:
        phase_text += f" | :red_circle: {high_risk_count} VEPs at HIGH RISK"

    # Build the message with attachments for color
    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": "VEP Governance Alerts",
                "emoji": True
            }
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": phase_text
            }
        },
        {
            "type": "divider"
        },
    ]

    # Add VEP Summary Table
    if table_rows:
        vep_table_text = build_slack_table(table_rows)
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*VEP Status Summary ({len(table_rows)} VEPs):*\n\n{vep_table_text[:2900]}"  # Slack char limit
            }
        })
        blocks.append({"type": "divider"})

    # Add summary if provided
    if alert_summary_text:
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Summary:*\n{alert_summary_text[:2000]}"
            }
        })

    # Add detailed alerts
    blocks.append({
        "type": "section",
        "text": {
            "type": "mrkdwn",
            "text": f"*Detailed Alerts ({len(alerts)} alert(s)):*\n{alert_text[:2900]}"
        }
    })

    message = {
        "attachments": [{
            "color": SEVERITY_COLORS.get(highest_severity, "#388e3c"),
            "blocks": blocks
        }]
    }

    return message


def _send_via_webhook(webhook_url: str, message: dict[str, Any]) -> bool:
    """Send message to Slack via Incoming Webhook.

    Returns:
        True if message was sent successfully, False otherwise
    """
    try:
        response = requests.post(
            webhook_url,
            json=message,
            headers={"Content-Type": "application/json"},
            timeout=10
        )

        response.raise_for_status()
        log("Slack message sent successfully", node="send_slack")
        return True

    except requests.exceptions.HTTPError as e:
        log(f"Slack webhook error: {e}", node="send_slack", level="ERROR")
        if e.response is not None:
            log(f"Slack error response: {e.response.text}", node="send_slack", level="ERROR")
        return False
    except Exception as e:
        log(f"Error sending to Slack: {e}", node="send_slack", level="ERROR")
        import traceback
        log(f"Traceback: {traceback.format_exc()}", node="send_slack", level="DEBUG")
        return False


def send_slack_node(state: VEPState) -> Any:
    """Send alerts via Slack Incoming Webhook.

    This node:
    1. Reads alerts from state (composed by alert_summary)
    2. Formats message using Slack Block Kit with severity colors
    3. Sends via Slack Incoming Webhook (requires SLACK_WEBHOOK_URL env var)
    4. Handles errors gracefully (logs but doesn't fail the workflow)

    Slack configuration:
    - Webhook URL: Set SLACK_WEBHOOK_URL env var (from Slack app configuration)

    Setup:
    1. Create a Slack app at https://api.slack.com/apps
    2. Enable Incoming Webhooks for the app
    3. Create a webhook for your channel
    4. Set: export SLACK_WEBHOOK_URL='https://hooks.slack.com/services/...'
    """
    alerts = state.get("alerts", [])
    alert_summary_text = state.get("alert_summary_text", "")

    # Check if Slack sending is disabled
    skip_send_slack = state.get("skip_send_slack", False)
    if skip_send_slack:
        log("Skip-send-slack mode: Slack alerts are disabled, skipping", node="send_slack")
        last_check_times = state.get("last_check_times", {})
        last_check_times["send_slack"] = datetime.now(UTC)
        return {
            "last_check_times": last_check_times,
        }

    log(f"Sending Slack alerts for {len(alerts)} alert(s)", node="send_slack")

    last_check_times = state.get("last_check_times", {})
    last_check_times["send_slack"] = datetime.now(UTC)

    if not alerts:
        log("No alerts to send, skipping Slack", node="send_slack")
        return {
            "last_check_times": last_check_times,
        }

    # Check if Slack webhook URL is configured
    webhook_url = os.environ.get("SLACK_WEBHOOK_URL")
    if not webhook_url:
        log("SLACK_WEBHOOK_URL not configured - cannot send Slack message", node="send_slack", level="WARNING")
        log("To send Slack messages:", node="send_slack", level="INFO")
        log("  1. Create a Slack app at https://api.slack.com/apps", node="send_slack", level="INFO")
        log("  2. Enable Incoming Webhooks", node="send_slack", level="INFO")
        log("  3. Create a webhook for your channel", node="send_slack", level="INFO")
        log("  4. Set: export SLACK_WEBHOOK_URL='https://hooks.slack.com/services/...'", node="send_slack", level="INFO")
        return {
            "last_check_times": last_check_times,
        }

    # Get pre-built table from analyze_combined (avoids redundant computation)
    table_rows = state.get("vep_summary_table", [])
    high_risk_count = sum(1 for row in table_rows if row["urgency"] == "RED")

    # Log what we're sending
    log(f"Slack will include VEP summary table: {len(table_rows)} VEPs, {high_risk_count} at HIGH RISK", node="send_slack")

    # Format and send Slack message
    message = _format_slack_message(alerts, alert_summary_text, table_rows)

    log("Sending Slack message via webhook...", node="send_slack", level="INFO")
    if _send_via_webhook(webhook_url, message):
        log(f"Slack message sent successfully with {len(alerts)} alert(s)", node="send_slack")
    else:
        log("Failed to send Slack message", node="send_slack", level="ERROR")

    return {
        "last_check_times": last_check_times,
    }
