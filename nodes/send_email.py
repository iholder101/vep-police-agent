"""Send email node - sends alerts via Resend API (easiest email service for real inbox delivery)."""

import json
import requests
from datetime import datetime
from typing import Any, List
from state import VEPState
from services.utils import log
import config


def _send_via_resend(recipients: List[str], subject: str, html_body: str, text_body: str) -> bool:
    """Send email via Resend API (easiest email service - sends to real inboxes!).
    
    Resend: https://resend.com
    - 3,000 emails/month free
    - Developer-friendly API
    - No domain verification needed for basic sending
    - Just needs API key from environment: RESEND_API_KEY
    
    Returns:
        True if email was sent successfully, False otherwise
    """
    api_key = config.get_resend_api_key()
    if not api_key:
        return False
    
    try:
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        
        # Resend API endpoint
        payload = {
            "from": "VEP Governance Agent <onboarding@resend.dev>",  # Default Resend domain (no setup needed!)
            "to": recipients,
            "subject": subject,
            "html": html_body,
            "text": text_body,
        }
        
        response = requests.post(
            "https://api.resend.com/emails",
            headers=headers,
            json=payload,
            timeout=10
        )
        
        response.raise_for_status()
        result = response.json()
        
        log(f"Email sent via Resend to {len(recipients)} recipient(s) - check your inbox!", node="send_email")
        log(f"Resend email ID: {result.get('id', 'unknown')}", node="send_email", level="DEBUG")
        return True
        
    except requests.exceptions.HTTPError as e:
        log(f"Resend API error: {e}", node="send_email", level="ERROR")
        if e.response is not None:
            try:
                error_data = e.response.json()
                log(f"Resend error details: {error_data}", node="send_email", level="ERROR")
            except:
                log(f"Resend error response: {e.response.text}", node="send_email", level="ERROR")
        return False
    except Exception as e:
        log(f"Error sending via Resend: {e}", node="send_email", level="ERROR")
        import traceback
        log(f"Traceback: {traceback.format_exc()}", node="send_email", level="DEBUG")
        return False


def send_email_node(state: VEPState) -> Any:
    """Send alerts via email using Resend API.
    
    This node:
    1. Reads alerts from state (composed by alert_summary)
    2. Formats email content (HTML or plain text)
    3. Sends via Resend API (requires RESEND_API_KEY env var)
    4. Handles errors gracefully (logs but doesn't fail the workflow)
    
    Email configuration:
    - Recipients: From EMAIL_RECIPIENTS env var (comma-separated) or config
    - Resend API Key: Set RESEND_API_KEY env var (get free key at https://resend.com/api-keys)
    - From: Resend default domain (onboarding@resend.dev)
    - Subject: "VEP Governance Alerts - [date]"
    - Body: Formatted alert summary
    
    Setup:
    - Sign up at https://resend.com (free)
    - Get API key from https://resend.com/api-keys
    - Set: export RESEND_API_KEY='re_...'
    """
    alerts = state.get("alerts", [])
    alert_summary_text = state.get("alert_summary_text", "")
    
    log(f"Sending email alerts for {len(alerts)} alert(s)", node="send_email")
    
    last_check_times = state.get("last_check_times", {})
    last_check_times["send_email"] = datetime.now()
    
    if not alerts:
        log("No alerts to send, skipping email", node="send_email")
        return {
            "last_check_times": last_check_times,
        }
    
    # Get email recipients from config (with env var fallback)
    recipients = config.get_email_recipients()
    if not recipients:
        log("Email recipients not configured (set EMAIL_RECIPIENTS env var or config.EMAIL_RECIPIENTS), skipping email send", node="send_email", level="WARNING")
        return {
            "last_check_times": last_check_times,
        }
    
    # Check if Resend API key is configured
    api_key = config.get_resend_api_key()
    if not api_key:
        log("RESEND_API_KEY not configured - cannot send email", node="send_email", level="ERROR")
        log("To send emails:", node="send_email", level="INFO")
        log("  1. Sign up at https://resend.com (free)", node="send_email", level="INFO")
        log("  2. Get API key from https://resend.com/api-keys", node="send_email", level="INFO")
        log("  3. Set: export RESEND_API_KEY='re_...'", node="send_email", level="INFO")
        
        # Log email content as fallback so user can see what would have been sent
        log("="*80, node="send_email", level="INFO")
        log("EMAIL CONTENT (would have been sent if RESEND_API_KEY was configured):", node="send_email", level="INFO")
        log("="*80, node="send_email", level="INFO")
        log(f"To: {', '.join(recipients)}", node="send_email", level="INFO")
        log(f"Subject: VEP Governance Alerts - {datetime.now().strftime('%Y-%m-%d %H:%M')}", node="send_email", level="INFO")
        log(f"Body: {len(alerts)} alert(s) would have been sent", node="send_email", level="INFO")
        log("="*80, node="send_email", level="INFO")
        
        # In one-cycle mode, exit even if email sending failed
        if state.get("one_cycle", False):
            log("One-cycle mode: Email sending failed (no API key), but exiting anyway", node="send_email")
            return {
                "last_check_times": last_check_times,
                "_exit_after_sheets": True,
            }
        
        return {
            "last_check_times": last_check_times,
        }
    
    # Format email content
    subject = f"VEP Governance Alerts - {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    
    # Group alerts by subject and severity
    alerts_by_subject = {}
    for alert in alerts:
        subject_key = alert.get("type", "other")
        if subject_key not in alerts_by_subject:
            alerts_by_subject[subject_key] = []
        alerts_by_subject[subject_key].append(alert)
    
    # Build email body (HTML)
    html_body = f"""<html>
<head><style>
  body {{ font-family: Arial, sans-serif; margin: 20px; }}
  h1 {{ color: #333; }}
  h2 {{ color: #666; margin-top: 20px; }}
  .alert {{ margin: 10px 0; padding: 10px; border-left: 4px solid #ccc; }}
  .critical {{ border-left-color: #d32f2f; background-color: #ffebee; }}
  .high {{ border-left-color: #f57c00; background-color: #fff3e0; }}
  .medium {{ border-left-color: #fbc02d; background-color: #fffde7; }}
  .low {{ border-left-color: #388e3c; background-color: #e8f5e9; }}
  .vep-id {{ font-weight: bold; color: #1976d2; }}
  .metadata {{ font-size: 0.9em; color: #666; margin-top: 5px; }}
</style></head>
<body>
<h1>VEP Governance Alerts</h1>
<p>Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
"""
    
    if alert_summary_text:
        html_body += f"<h2>Summary</h2><p>{alert_summary_text.replace(chr(10), '<br>')}</p>"
    
    # Add alerts grouped by subject
    for subject_key, subject_alerts in alerts_by_subject.items():
        subject_title = subject_key.replace("_", " ").title()
        html_body += f"<h2>{subject_title} ({len(subject_alerts)} alert(s))</h2>"
        
        for alert in subject_alerts:
            severity = alert.get("severity", "low")
            vep_id = alert.get("vep_id", "?")
            vep_name = alert.get("vep_name", "?")
            title = alert.get("title", "")
            message = alert.get("message", "")
            metadata = alert.get("metadata", {})
            
            html_body += f"""
<div class="alert {severity}">
  <div class="vep-id">VEP {vep_id} ({vep_name})</div>
  <div><strong>{title}</strong></div>
  <div>{message}</div>
  {f'<div class="metadata">Metadata: {json.dumps(metadata)}</div>' if metadata else ''}
</div>
"""
    
    html_body += """
</body>
</html>
"""
    
    # Also create plain text version
    text_body = f"VEP Governance Alerts\n"
    text_body += f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
    
    if alert_summary_text:
        text_body += f"Summary:\n{alert_summary_text}\n\n"
    
    for subject_key, subject_alerts in alerts_by_subject.items():
        subject_title = subject_key.replace("_", " ").title()
        text_body += f"{subject_title} ({len(subject_alerts)} alert(s)):\n"
        for alert in subject_alerts:
            vep_id = alert.get("vep_id", "?")
            vep_name = alert.get("vep_name", "?")
            title = alert.get("title", "")
            message = alert.get("message", "")
            text_body += f"  - VEP {vep_id} ({vep_name}): {title}\n    {message}\n"
        text_body += "\n"
    
    # Send via Resend
    log("Sending email via Resend...", node="send_email", level="INFO")
    if _send_via_resend(recipients, subject, html_body, text_body):
        log(f"✅ Email sent successfully via Resend to {len(recipients)} recipient(s) - check your inbox!", node="send_email")
        
        # Check if one-cycle mode is enabled - exit after email is sent
        if state.get("one_cycle", False):
            log("One-cycle mode: Email sent successfully, setting exit flag", node="send_email")
            return {
                "last_check_times": last_check_times,
                "_exit_after_sheets": True,
            }
        
        return {
            "last_check_times": last_check_times,
        }
    else:
        # Resend failed - log email content as fallback
        log("Failed to send email via Resend", node="send_email", level="ERROR")
        log("="*80, node="send_email", level="INFO")
        log("EMAIL CONTENT (failed to send, but here's what would have been sent):", node="send_email", level="INFO")
        log("="*80, node="send_email", level="INFO")
        log(f"To: {', '.join(recipients)}", node="send_email", level="INFO")
        log(f"Subject: {subject}", node="send_email", level="INFO")
        log("Body (text preview):", node="send_email", level="INFO")
        # Log first 500 chars of text body
        text_preview = text_body[:500] + ("..." if len(text_body) > 500 else "")
        for line in text_preview.split("\n")[:20]:  # First 20 lines
            log(f"  {line}", node="send_email", level="INFO")
        if len(text_body) > 500:
            log(f"  ... (truncated, total length: {len(text_body)} chars)", node="send_email", level="INFO")
        log("="*80, node="send_email", level="INFO")
        
        # In one-cycle mode, exit even if email sending failed
        if state.get("one_cycle", False):
            log("One-cycle mode: Email sending failed, but exiting anyway", node="send_email")
            return {
                "last_check_times": last_check_times,
                "_exit_after_sheets": True,
            }
        
        return {
            "last_check_times": last_check_times,
        }
