"""Alert summary node - composes alert list from VEP analysis."""

import json
from datetime import datetime
from typing import Any, List, Dict
from state import VEPState
from services.utils import log
from services.llm_helper import invoke_llm_with_tools
from pydantic import BaseModel


class Alert(BaseModel):
    """Represents a single alert."""
    subject: str  # Alert subject/category (e.g., "deadline_approaching", "low_activity", "compliance_issue", "risk")
    severity: str  # "low", "medium", "high", "critical"
    vep_id: int  # VEP tracking issue ID
    vep_name: str  # VEP identifier (e.g., "vep-0156")
    title: str  # Alert title/headline
    message: str  # Detailed alert message
    metadata: Dict[str, Any] = {}  # Additional context (deadline dates, compliance flags, etc.)


class AlertSummaryResponse(BaseModel):
    """Response model for alert summary."""
    alerts: List[Alert] = []  # List of composed alerts
    summary_text: str = ""  # Human-readable summary text for email


def alert_summary_node(state: VEPState) -> Any:
    """Compose alert list from merged VEP analysis.
    
    This node:
    1. Takes the merged VEP analysis from analyze_combined
    2. Identifies alert-worthy situations:
       - Deadlines approaching (EF, CF)
       - Low activity (inactive VEPs)
       - Compliance issues (missing sign-offs, incomplete templates, etc.)
       - Risk indicators (at risk status, exception required, etc.)
       - Status changes (new VEPs, status updates)
    3. Composes structured alerts for each situation
    4. Generates a human-readable summary text for email
    
    Alerts are categorized by subject and severity to enable filtering and prioritization.
    """
    veps = state.get("veps", [])
    log(f"Composing alert summary for {len(veps)} VEP(s)", node="alert_summary")
    
    last_check_times = state.get("last_check_times", {})
    last_check_times["alert_summary"] = datetime.now()
    
    if not veps:
        return {
            "last_check_times": last_check_times,
            "alerts": [],
        }
    
    # Check if mock mode is enabled - skip LLM and create mocked alerts
    mock_mode = state.get("mock_alert_summary", False)
    if mock_mode:
        log("Mock alert-summary mode: Skipping LLM call, creating mocked alerts", node="alert_summary")
        
        # Create mocked alerts for first few VEPs
        mocked_alerts = []
        for i, vep in enumerate(veps[:3]):  # Create alerts for first 3 VEPs
            alert_types = [
                ("deadline_approaching", "high", f"VEP {vep.tracking_issue_id}: Deadline approaching in {i+2} days"),
                ("low_activity", "medium", f"VEP {vep.tracking_issue_id}: Low activity detected ({i+5} days since update)"),
                ("compliance_issue", "high", f"VEP {vep.tracking_issue_id}: Missing SIG sign-off"),
            ]
            
            subject, severity, title = alert_types[i % len(alert_types)]
            mocked_alerts.append({
                "subject": subject,
                "severity": severity,
                "vep_id": vep.tracking_issue_id,
                "vep_name": vep.name,
                "title": title,
                "message": f"Mock alert for {vep.name}: {title}",
                "metadata": {"mock": True, "vep_title": vep.title},
            })
        
        summary_text = f"Mock Alert Summary:\n\n"
        summary_text += f"Generated {len(mocked_alerts)} mock alert(s) for testing.\n\n"
        for alert in mocked_alerts:
            summary_text += f"- [{alert['severity'].upper()}] {alert['title']}\n"
        
        log(f"Created {len(mocked_alerts)} mocked alert(s)", node="alert_summary")
        
        return {
            "last_check_times": last_check_times,
            "alerts": mocked_alerts,
            "alert_summary_text": summary_text,
        }
    
    # Build system prompt
    system_prompt = """You are a VEP governance agent composing alerts from VEP analysis.

Your task is to identify alert-worthy situations and compose structured alerts.

ALERT CATEGORIES (subject field):
1. "deadline_approaching" - Deadlines (EF, CF) are approaching or have passed
2. "low_activity" - VEP has low/no activity (inactive, stale)
3. "compliance_issue" - VEP has compliance problems (missing sign-offs, incomplete template, etc.)
4. "risk" - VEP is at risk, requires exception, or has other risk indicators
5. "status_change" - VEP status changed (new VEP, status update, etc.)
6. "milestone_update" - VEP milestone status changed

SEVERITY LEVELS:
- "critical": Immediate action required (deadline passed, multiple compliance issues, at risk)
- "high": Urgent attention needed (deadline < 3 days, compliance issues, low activity + close deadline)
- "medium": Should be addressed soon (deadline < 7 days, single compliance issue, low activity)
- "low": Informational (status changes, milestone updates, minor issues)

ALERT COMPOSITION RULES:
1. For each VEP, analyze:
   - Deadline proximity (days until EF/CF, or if passed)
   - Activity levels (days since last update, review lag)
   - Compliance status (template, sign-offs, PRs linked, labels)
   - Milestone status (at risk, exception required, etc.)
   - Status changes (new VEP, status updates)

2. Create alerts for:
   - Deadlines approaching within 7 days (or passed) → "deadline_approaching"
   - No activity for >14 days → "low_activity"
   - Any compliance flags failing → "compliance_issue"
   - VEP marked "At risk" or "Exception Required" → "risk"
   - New VEPs or significant status changes → "status_change"
   - Milestone status changes → "milestone_update"

3. For each alert, provide:
   - subject: One of the categories above
   - severity: Based on urgency and impact
   - vep_id: The tracking issue ID
   - vep_name: VEP identifier (e.g., "vep-0156")
   - title: Brief alert headline (e.g., "VEP 156: Deadline approaching in 2 days")
   - message: Detailed message explaining the alert
   - metadata: Additional context (deadline dates, compliance flags, etc.)

4. Generate a summary_text that provides a human-readable overview of all alerts, organized by category.

Return all alerts and the summary text."""
    
    # Prepare context
    release_schedule = state.get("release_schedule")
    context = {
        "veps": [vep.model_dump(mode='json') for vep in veps],
        "release_schedule": release_schedule.model_dump(mode='json') if release_schedule else None,
        "current_release": state.get("current_release"),
    }
    
    user_prompt = f"""Here is the current VEP state with all analysis:

{json.dumps(context, indent=2, default=str)}

Analyze each VEP and compose alerts for alert-worthy situations. Return all alerts and a summary text."""
    
    # Invoke LLM with structured output
    result = invoke_llm_with_tools(
        "alert_summary",
        context,
        system_prompt,
        user_prompt,
        AlertSummaryResponse,
        mcp_names=("github",)  # May need GitHub tools for additional context
    )
    
    # Convert Alert objects to dicts for state storage
    alerts_dicts = [alert.model_dump() for alert in result.alerts]
    
    if alerts_dicts:
        log(f"Composed {len(alerts_dicts)} alert(s) - email will be sent", node="alert_summary")
        for alert in alerts_dicts:
            log(f"  - {alert['subject']} ({alert['severity']}): {alert['title']}", node="alert_summary", level="DEBUG")
    else:
        log("No alerts to send - skipping email", node="alert_summary")
    
    result = {
        "last_check_times": last_check_times,
        "alerts": alerts_dicts,  # Add new alerts to state (empty list if no alerts)
        "alert_summary_text": result.summary_text,  # Store summary text for email
    }
    
    # In one-cycle mode, if update_sheets already set _exit_after_sheets, clear next_tasks now
    # This ensures alert_summary runs before exit
    if state.get("one_cycle", False) and state.get("_exit_after_sheets", False):
        log("One-cycle mode: Alert summary completed, clearing queue for exit", node="alert_summary")
        next_tasks = state.get("next_tasks", [])
        if "alert_summary" in next_tasks:
            next_tasks.remove("alert_summary")
        result["next_tasks"] = next_tasks
    
    return result