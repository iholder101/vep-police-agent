"""Send email node - sends alerts via Ethereal Email (no config needed)."""

import json
import smtplib
import requests
from datetime import datetime
from typing import Any, List, Dict, Optional
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from state import VEPState
from services.utils import log
import config


def _get_ethereal_credentials() -> Optional[Dict[str, str]]:
    """Get temporary SMTP credentials from Ethereal Email API.
    
    Ethereal Email provides temporary SMTP credentials via API - no registration needed.
    Emails sent to these addresses are captured and can be viewed at https://ethereal.email
    
    Returns:
        Dictionary with SMTP credentials (host, port, user, pass, web_url) or None if failed
    """
    try:
        # Call Ethereal Email API to create a temporary account
        response = requests.post("https://api.nodemailer.com/user", timeout=10)
        response.raise_for_status()
        data = response.json()
        
        user = data.get("user")
        password = data.get("pass")
        
        if not user or not password:
            log("Ethereal Email API returned invalid credentials", node="send_email", level="ERROR")
            return None
        
        return {
            "host": "smtp.ethereal.email",
            "port": 587,
            "user": user,
            "pass": password,
            "from_email": user,  # Use the generated email as from address
            "web_url": f"https://ethereal.email/message/{user}",
        }
    except Exception as e:
        log(f"Failed to get Ethereal Email credentials: {e}", node="send_email", level="ERROR")
        import traceback
        log(f"Traceback: {traceback.format_exc()}", node="send_email", level="DEBUG")
        return None


def send_email_node(state: VEPState) -> Any:
    """Send alerts via email using Ethereal Email (zero configuration).
    
    This node:
    1. Reads alerts from state (composed by alert_summary)
    2. Formats email content (HTML or plain text)
    3. Gets temporary SMTP credentials from Ethereal Email API (no registration needed)
    4. Sends email via SMTP using those credentials
    5. Emails are captured at Ethereal Email and can be viewed online
    6. Handles errors gracefully (logs but doesn't fail the workflow)
    
    Email configuration:
    - Recipients: From EMAIL_RECIPIENTS env var (comma-separated) or config
    - From: Generated Ethereal Email address
    - Subject: "VEP Governance Alerts - [date]"
    - Body: Formatted alert summary
    
    Note: Emails sent via Ethereal Email are captured in a sandbox and can be viewed
    at https://ethereal.email. This is perfect for testing and development.
    No registration, tokens, or configuration needed!
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
    
    # Get Ethereal Email credentials (no registration needed!)
    ethereal = _get_ethereal_credentials()
    if not ethereal:
        log("Failed to get Ethereal Email credentials, cannot send email", node="send_email", level="ERROR")
        return {
            "last_check_times": last_check_times,
        }
    
    log(f"Using Ethereal Email: {ethereal['from_email']} (view at {ethereal['web_url']})", node="send_email", level="INFO")
    
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
  .note {{ margin-top: 20px; padding: 10px; background-color: #e3f2fd; border-left: 4px solid #2196f3; }}
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
    
    # Add note about Ethereal Email
    html_body += f"""
<div class="note">
  <strong>Note:</strong> This email was sent via Ethereal Email (testing sandbox).
  View all emails at: <a href="{ethereal['web_url']}">{ethereal['web_url']}</a>
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
    
    text_body += f"\nNote: This email was sent via Ethereal Email (testing sandbox).\nView all emails at: {ethereal['web_url']}\n"
    
    # Create email message
    message = MIMEMultipart("alternative")
    message["From"] = ethereal["from_email"]
    message["To"] = ", ".join(recipients)
    message["Subject"] = subject
    
    # Add both plain text and HTML parts
    text_part = MIMEText(text_body, "plain")
    html_part = MIMEText(html_body, "html")
    message.attach(text_part)
    message.attach(html_part)
    
    # Send via SMTP using Ethereal Email credentials
    try:
        log(f"Connecting to {ethereal['host']}:{ethereal['port']}", node="send_email", level="DEBUG")
        
        # Create SMTP connection with TLS
        server = smtplib.SMTP(ethereal["host"], ethereal["port"])
        server.starttls()  # Enable TLS
        
        # Login
        server.login(ethereal["user"], ethereal["pass"])
        
        # Send email
        server.send_message(message)
        server.quit()
        
        log(f"Email sent successfully via Ethereal Email to {len(recipients)} recipient(s)", node="send_email")
        log(f"View email at: {ethereal['web_url']}", node="send_email", level="INFO")
        
        # Check if one-cycle mode is enabled - exit after email is sent
        if state.get("one_cycle", False):
            log("One-cycle mode: Email sent successfully, setting exit flag", node="send_email")
            return {
                "last_check_times": last_check_times,
                "_exit_after_sheets": True,
            }
        
    except Exception as e:
        log(f"Error sending email via Ethereal Email: {e}", node="send_email", level="ERROR")
        import traceback
        log(f"Traceback: {traceback.format_exc()}", node="send_email", level="ERROR")
    
    return {
        "last_check_times": last_check_times,
    }
