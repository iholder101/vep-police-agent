"""Send email node - sends alerts via Resend (easiest), system mail, or Ethereal Email (fallback)."""

import json
import smtplib
import subprocess
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
        # Try the Ethereal Email API endpoint
        # Note: The API may be rate-limited or require specific headers
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "VEP-Police-Agent/1.0",
        }
        
        # Try POST to create account with required parameters
        response = requests.post(
            "https://api.nodemailer.com/user",
            headers=headers,
            json={
                "requestor": "vep-police-agent",
                "version": "1.0"
            },
            timeout=10
        )
        
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
        log("Ethereal Email API may be temporarily unavailable. Email will not be sent.", node="send_email", level="WARNING")
        import traceback
        log(f"Traceback: {traceback.format_exc()}", node="send_email", level="DEBUG")
        return None


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
        log(f"Resend API error: {e}", node="send_email", level="DEBUG")
        if e.response is not None:
            try:
                error_data = e.response.json()
                log(f"Resend error details: {error_data}", node="send_email", level="DEBUG")
            except:
                log(f"Resend error response: {e.response.text}", node="send_email", level="DEBUG")
        return False
    except Exception as e:
        log(f"Error sending via Resend: {e}", node="send_email", level="DEBUG")
        return False


def _send_via_system_mail(recipients: List[str], subject: str, text_body: str) -> bool:
    """Try to send email using system's mail command (sends to real inboxes).
    
    This uses the local mail server (sendmail/postfix) if available.
    No configuration needed - uses system defaults.
    
    Returns:
        True if email was sent successfully, False otherwise
    """
    try:
        # Try sendmail first (more reliable)
        try:
            for recipient in recipients:
                process = subprocess.Popen(
                    ['sendmail', '-t', '-i'],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True
                )
                email_content = f"To: {recipient}\nSubject: {subject}\n\n{text_body}"
                stdout, stderr = process.communicate(input=email_content, timeout=10)
                
                if process.returncode == 0:
                    log(f"Email sent via sendmail to {recipient}", node="send_email", level="DEBUG")
                else:
                    log(f"sendmail failed for {recipient}: {stderr}", node="send_email", level="DEBUG")
                    return False
            
            return True
        except FileNotFoundError:
            # sendmail not available, try mail command
            pass
        
        # Fallback to mail command
        for recipient in recipients:
            process = subprocess.Popen(
                ['mail', '-s', subject, recipient],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True
            )
            stdout, stderr = process.communicate(input=text_body, timeout=10)
            
            if process.returncode == 0:
                log(f"Email sent via mail command to {recipient}", node="send_email", level="DEBUG")
            else:
                log(f"mail command failed for {recipient}: {stderr}", node="send_email", level="DEBUG")
                return False
        
        return True
    except FileNotFoundError:
        # Neither mail command available
        return False
    except Exception as e:
        log(f"Error using system mail: {e}", node="send_email", level="DEBUG")
        return False


def send_email_node(state: VEPState) -> Any:
    """Send alerts via email using Resend (easiest), system mail, or Ethereal Email (fallback).
    
    This node:
    1. Reads alerts from state (composed by alert_summary)
    2. Formats email content (HTML or plain text)
    3. Tries Resend first (easiest - just needs RESEND_API_KEY env var, sends to real inboxes!)
    4. Falls back to system mail if Resend not configured (delivers to real inboxes if configured)
    5. Falls back to Ethereal Email if both unavailable (testing sandbox - NOT real inboxes)
    6. Handles errors gracefully (logs but doesn't fail the workflow)
    
    Email configuration:
    - Recipients: From EMAIL_RECIPIENTS env var (comma-separated) or config
    - Resend API Key: Set RESEND_API_KEY env var (get free key at https://resend.com/api-keys)
    - From: Resend default domain (Resend) or System default (system mail) or Ethereal (fallback)
    - Subject: "VEP Governance Alerts - [date]"
    - Body: Formatted alert summary
    
    Priority order:
    1. Resend (easiest - 3,000 emails/month free, no domain verification needed!)
    2. System mail (works if sendmail/postfix configured)
    3. Ethereal Email (testing sandbox - emails don't reach real inboxes)
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
    
    # Format email content first (before checking credentials, so we can log it if sending fails)
    subject = f"VEP Governance Alerts - {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    
    # Group alerts by subject and severity
    alerts_by_subject = {}
    for alert in alerts:
        subject_key = alert.get("type", "other")
        if subject_key not in alerts_by_subject:
            alerts_by_subject[subject_key] = []
        alerts_by_subject[subject_key].append(alert)
    
    # Build email body (HTML) - without Ethereal URL for now
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
    
    # Try Resend first (easiest - sends to real inboxes, just needs RESEND_API_KEY env var)
    log("Attempting to send email via Resend (real inbox delivery)...", node="send_email", level="INFO")
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
    
    # Try system mail second (sends to real inboxes if sendmail/postfix is configured)
    log("Resend not configured, attempting system mail (real inbox delivery)...", node="send_email", level="INFO")
    if _send_via_system_mail(recipients, subject, text_body):
        log(f"✅ Email sent successfully via system mail to {len(recipients)} recipient(s) - check your inbox!", node="send_email")
        
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
    
    # System mail not available - fall back to Ethereal Email (testing sandbox)
    log("⚠️  Resend and system mail not available", node="send_email", level="WARNING")
    log("⚠️  Falling back to Ethereal Email (testing sandbox - emails will NOT arrive in your real inbox!)", node="send_email", level="WARNING")
    log("💡 To receive emails in your real inbox:", node="send_email", level="INFO")
    log("   EASIEST: Set RESEND_API_KEY env var (get free key at https://resend.com/api-keys)", node="send_email", level="INFO")
    log("   - Resend: 3,000 emails/month free, no domain verification needed!", node="send_email", level="INFO")
    log("   - Just sign up, get API key, set: export RESEND_API_KEY='re_...'", node="send_email", level="INFO")
    log("   Alternative: Install/configure sendmail/postfix (e.g., 'sudo dnf install postfix')", node="send_email", level="INFO")
    log("   For now, view emails at the Ethereal URL below", node="send_email", level="INFO")
    
    # Get Ethereal Email credentials (no registration needed!)
    ethereal = _get_ethereal_credentials()
    if not ethereal:
        # If Ethereal Email fails, log the email content so user can see what would have been sent
        log("Failed to get Ethereal Email credentials, cannot send email", node="send_email", level="ERROR")
        log("="*80, node="send_email", level="INFO")
        log("EMAIL CONTENT (would have been sent):", node="send_email", level="INFO")
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
    
    log(f"Using Ethereal Email: {ethereal['from_email']} (view at {ethereal['web_url']})", node="send_email", level="INFO")
    log("⚠️  WARNING: Ethereal Email is a testing sandbox - emails will NOT arrive in your real inbox!", node="send_email", level="WARNING")
    log("⚠️  View emails at: " + ethereal['web_url'], node="send_email", level="WARNING")
    
    # Add Ethereal Email note to bodies now that we have credentials
    html_body = html_body.replace("</body>", f"""
<div class="note" style="background-color: #fff3cd; border-left: 4px solid #ffc107; padding: 15px; margin-top: 20px;">
  <strong>⚠️ IMPORTANT:</strong> This email was sent via Ethereal Email (testing sandbox).
  <br><strong>This email will NOT appear in your real inbox!</strong>
  <br>View this email at: <a href="{ethereal['web_url']}">{ethereal['web_url']}</a>
  <br><br>To receive emails in your real inbox, configure sendmail/postfix on your system.
</div>
</body>""")
    text_body += f"\n\n{'='*60}\n"
    text_body += f"⚠️  IMPORTANT: This email was sent via Ethereal Email (testing sandbox).\n"
    text_body += f"⚠️  This email will NOT appear in your real inbox!\n"
    text_body += f"View this email at: {ethereal['web_url']}\n"
    text_body += f"To receive emails in your real inbox, configure sendmail/postfix on your system.\n"
    text_body += f"{'='*60}\n"
    
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
        
        # Log email content as fallback so user can see what would have been sent
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
    
    return {
        "last_check_times": last_check_times,
    }
