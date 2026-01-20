"""Send Slack node - sends alerts via Slack Incoming Webhook."""

import json
import os
import requests
from datetime import datetime
from typing import Any, List, Dict
from state import VEPState
from services.utils import log


# Severity color mapping for Slack attachments
SEVERITY_COLORS = {
    "critical": "#d32f2f",  # Red
    "high": "#f57c00",      # Orange
    "medium": "#fbc02d",    # Yellow
    "low": "#388e3c",       # Green
}


def _format_slack_message(alerts: List[Dict[str, Any]], alert_summary_text: str) -> Dict[str, Any]:
    """Format alerts as a Slack Block Kit message with color-coded attachments.

    Returns a Slack message payload with attachments for severity-based coloring.
    """
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

    # Build the message with attachments for color
    message = {
        "attachments": [{
            "color": SEVERITY_COLORS.get(highest_severity, "#388e3c"),
            "blocks": [
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
                        "text": f"*{len(alerts)} alert(s)* generated at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                    }
                },
                {
                    "type": "divider"
                },
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": alert_text[:3000]  # Slack has a 3000 char limit per text block
                    }
                }
            ]
        }]
    }

    # Add summary if provided
    if alert_summary_text:
        summary_block = {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Summary:*\n{alert_summary_text[:2000]}"  # Truncate if too long
            }
        }
        message["attachments"][0]["blocks"].insert(3, summary_block)

    return message


def _send_via_webhook(webhook_url: str, message: Dict[str, Any]) -> bool:
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
        last_check_times["send_slack"] = datetime.now()
        return {
            "last_check_times": last_check_times,
        }

    log(f"Sending Slack alerts for {len(alerts)} alert(s)", node="send_slack")

    last_check_times = state.get("last_check_times", {})
    last_check_times["send_slack"] = datetime.now()

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

    # Format and send Slack message
    message = _format_slack_message(alerts, alert_summary_text)

    log("Sending Slack message via webhook...", node="send_slack", level="INFO")
    if _send_via_webhook(webhook_url, message):
        log(f"Slack message sent successfully with {len(alerts)} alert(s)", node="send_slack")
    else:
        log("Failed to send Slack message", node="send_slack", level="ERROR")

    return {
        "last_check_times": last_check_times,
    }
