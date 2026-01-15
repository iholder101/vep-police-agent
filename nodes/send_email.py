"""Send email node - sends alerts via Gmail API."""

import json
import base64
from datetime import datetime
from typing import Any, List, Dict, Optional
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from state import VEPState
from services.utils import log, get_google_token
import os


def send_email_node(state: VEPState) -> Any:
    """Send alerts via email using Gmail API.
    
    This node:
    1. Reads alerts from state (composed by alert_summary)
    2. Formats email content (HTML or plain text)
    3. Sends email via Gmail API using service account credentials
    4. Handles errors gracefully (logs but doesn't fail the workflow)
    
    Email configuration:
    - Recipients: From EMAIL_RECIPIENTS env var (comma-separated) or config
    - From: Service account email (from GOOGLE_TOKEN)
    - Subject: "VEP Governance Alerts - [date]"
    - Body: Formatted alert summary
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
    
    # Get email recipients from environment or config
    recipients_str = os.environ.get("EMAIL_RECIPIENTS", "")
    if not recipients_str:
        log("EMAIL_RECIPIENTS not set, skipping email send", node="send_email", level="WARNING")
        return {
            "last_check_times": last_check_times,
        }
    
    recipients = [email.strip() for email in recipients_str.split(",") if email.strip()]
    if not recipients:
        log("No valid email recipients found, skipping email send", node="send_email", level="WARNING")
        return {
            "last_check_times": last_check_times,
        }
    
    # Get service account email from Google token
    try:
        google_token = get_google_token()
        if not google_token:
            log("GOOGLE_TOKEN not available, cannot send email", node="send_email", level="WARNING")
            return {
                "last_check_times": last_check_times,
            }
        
        # Parse service account JSON to get email
        import json as json_module
        service_account = json_module.loads(google_token)
        from_email = service_account.get("client_email")
        
        if not from_email:
            log("Service account email not found in GOOGLE_TOKEN, cannot send email", node="send_email", level="WARNING")
            return {
                "last_check_times": last_check_times,
            }
    except Exception as e:
        log(f"Error parsing Google token: {e}", node="send_email", level="ERROR")
        return {
            "last_check_times": last_check_times,
        }
    
    # Format email content
    subject = f"VEP Governance Alerts - {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    
    # Group alerts by subject and severity
    alerts_by_subject = {}
    for alert in alerts:
        subject_key = alert.get("subject", "other")
        if subject_key not in alerts_by_subject:
            alerts_by_subject[subject_key] = []
        alerts_by_subject[subject_key].append(alert)
    
    # Build email body (HTML)
    html_body = f"""<html>
<head><style>
  body {{ font-family: Arial, sans-serif; }}
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
    
    # Create email message
    message = MIMEMultipart("alternative")
    message["From"] = from_email
    message["To"] = ", ".join(recipients)
    message["Subject"] = subject
    
    # Add both plain text and HTML parts
    text_part = MIMEText(text_body, "plain")
    html_part = MIMEText(html_body, "html")
    message.attach(text_part)
    message.attach(html_part)
    
    # Encode message
    raw_message = base64.urlsafe_b64encode(message.as_bytes()).decode()
    
    # Send via Gmail API
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
        
        # Create credentials from service account JSON
        # Note: Gmail API with service accounts requires domain-wide delegation
        # If you get "Insufficient Permission" errors, you need to:
        # 1. Enable domain-wide delegation in Google Cloud Console
        # 2. Authorize the service account in Google Workspace Admin Console
        credentials = service_account.Credentials.from_service_account_info(
            json_module.loads(google_token),
            scopes=["https://www.googleapis.com/auth/gmail.send"]
        )
        
        # Build Gmail service
        service = build("gmail", "v1", credentials=credentials)
        
        # Send message
        send_message = service.users().messages().send(
            userId="me",
            body={"raw": raw_message}
        ).execute()
        
        message_id = send_message.get("id")
        log(f"Email sent successfully to {len(recipients)} recipient(s) (message ID: {message_id})", node="send_email")
        
        # Check if one-cycle mode is enabled - exit after email is sent
        if state.get("one_cycle", False):
            log("One-cycle mode: Email sent successfully, setting exit flag", node="send_email")
            # Set flag to signal main loop to exit
            return {
                "last_check_times": last_check_times,
                "_exit_after_sheets": True,  # Reuse this flag name for consistency
            }
        
    except Exception as e:
        error_msg = str(e)
        if "Insufficient Permission" in error_msg or "403" in error_msg:
            log(f"Gmail API permission error: Service account needs domain-wide delegation enabled. See: https://developers.google.com/identity/protocols/oauth2/service-account#delegatingauthority", node="send_email", level="ERROR")
        else:
            log(f"Error sending email via Gmail API: {e}", node="send_email", level="ERROR")
        # Don't fail the workflow - just log the error
        return {
            "last_check_times": last_check_times,
        }
    
    return {
        "last_check_times": last_check_times,
    }
