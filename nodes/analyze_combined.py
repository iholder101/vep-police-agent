"""Analysis node - performs ALL reasoning about VEP status and generates alerts.

This is the ONLY analysis node. Fetch nodes (check_*) only gather raw data.
This node has access to ALL context at once and does holistic reasoning.
"""

import json
from datetime import datetime
from typing import Any, List
from state import VEPState
from services.utils import log
from services.llm_helper import invoke_llm_check
from services.response_models import CheckResponse


class AnalyzeCombinedResponse(CheckResponse):
    """Response model for combined analysis."""
    sheets_need_update: bool = False
    general_insights: List[str] = []


def analyze_combined_node(state: VEPState) -> Any:
    """Analyze all VEPs using their combined context data.

    This is the ONLY analysis node in the pipeline. It:
    1. Receives VEPs with raw context from all fetch nodes:
       - vep.context.deadline: days to EF/CF, freeze status, target release
       - vep.context.activity: last updates, recent events, staleness
       - vep.context.compliance: PR status, labels, template completeness
       - vep.context.exceptions: exception issues, post-freeze activity
    2. Performs holistic cross-domain reasoning:
       - Low activity + close deadline = URGENT
       - Compliance issues + close deadline = CRITICAL
       - Post-freeze commits without exception = ALERT
    3. Updates vep.analysis with combined insights and priority
    4. Generates alerts for issues that need attention
    5. Determines if sheets need updating
    """
    veps = state.get("veps", [])
    log(f"Analyzing {len(veps)} VEP(s) with combined context", node="analyze_combined")

    last_check_times = state.get("last_check_times", {})
    last_check_times["analyze_combined"] = datetime.now()

    if not veps:
        return {
            "last_check_times": last_check_times,
            "general_insights": [],
            "sheets_need_update": False,
        }

    # Check for mock mode - skip LLM and do naive analysis
    mock_mode = state.get("mock_analyzed_combined", False)
    if mock_mode:
        log("Mock mode: Skipping LLM, using naive analysis", node="analyze_combined")
        for vep in veps:
            if not hasattr(vep, 'analysis') or vep.analysis is None:
                vep.analysis = {}
            vep.analysis["combined_insights"] = "Mock analysis: Status reviewed."

        skip_monitoring = state.get("skip_monitoring", False)
        return {
            "last_check_times": last_check_times,
            "veps": veps,
            "general_insights": ["Mock analysis complete."],
            "sheets_need_update": skip_monitoring,
        }

    # Build system prompt for comprehensive analysis
    system_prompt = """You are a VEP governance analyst. Your job is to analyze VEP status using ALL available context and generate actionable insights.

INPUT: Each VEP has raw context data from fetch nodes:
- context.deadline: {days_until_ef, days_until_cf, ef_passed, cf_passed, vep_merged, target_release}
- context.activity: {last_issue_update, last_pr_update, days_since_update, recent_comments, recent_commits}
- context.compliance: {pr_state, has_lgtm, has_approved_label, sig_labels, implementation_prs, template sections}
- context.exceptions: {exception_issue_number, exception_issue_state, has_post_ef_commits, has_post_cf_commits}

YOUR ANALYSIS TASKS:

1. DEADLINE RISK ASSESSMENT:
   - Calculate risk level (low/medium/high/critical) based on days to freeze + current progress
   - Flag VEPs at risk of missing deadlines
   - Note if VEP is merged (safe) vs still pending

2. ACTIVITY ANALYSIS:
   - Identify stale VEPs (no activity for >7 days during active development)
   - Note unusual patterns (burst of activity, sudden silence)
   - Consider activity relative to deadline proximity

3. COMPLIANCE CHECK:
   - Flag missing approvals (no LGTM, no approved label)
   - Check for missing SIG labels
   - Identify incomplete template sections
   - Note implementation PR status

4. EXCEPTION HANDLING:
   - Flag post-freeze commits without approved exception
   - Check exception request status
   - Validate exception justification if present

5. CROSS-DOMAIN REASONING (CRITICAL):
   - Low activity + close deadline = URGENT priority
   - Compliance issues + close deadline = CRITICAL priority
   - Post-freeze work + no exception = BLOCKER
   - Multiple issues on same VEP = escalate priority

6. OUTPUT FOR EACH VEP:
   Update vep.analysis with:
   - combined_insights: string summary of overall status
   - priority: "low", "medium", "high", or "critical"
   - risk_factors: list of identified risks
   - recommended_actions: list of suggested next steps

7. GENERAL INSIGHTS (release-wide):
   Return a list of strings covering:
   - Overall release health ("5 of 20 VEPs at risk")
   - Cross-VEP patterns ("Network SIG VEPs are behind")
   - Release-wide recommendations

8. SHEETS UPDATE DECISION:
   Set sheets_need_update=True if:
   - Any VEP has critical/high priority
   - Significant status changes detected
   - New compliance or exception issues
   Set to False if only minor internal updates.

IMPORTANT: Generate alerts in the `alerts` field for issues needing attention. Each alert should have:
- subject: Use ONE of these canonical types:
  * "deadline_violation" - freeze deadlines missed/approaching
  * "activity_issue" - stale/inactive VEPs
  * "compliance_issue" - missing labels, approvals, docs
  * "exception_required" - needs exception for post-freeze work
  * "general_risk" - cross-domain or urgent blockers
- severity: "low", "medium", "high", "critical"
- vep_id: the tracking_issue_id (GitHub issue number, e.g. 181)
- vep_name: which VEP identifier (e.g. "vep-0181")
- vep_title: the VEP's title/description (e.g. "VPC Networking Support"), truncated to 40 chars if needed
- title: brief alert headline
- message: what's the issue and recommended action

Generate ONE alert per VEP per issue type. Do NOT create multiple alerts for the same VEP
and issue category - consolidate related issues into a single alert."""

    # Serialize VEPs with full context for LLM
    release_schedule = state.get("release_schedule")
    context = {
        "veps": [vep.model_dump(mode='json') for vep in veps],
        "release_schedule": release_schedule.model_dump(mode='json') if release_schedule else None,
        "current_release": state.get("current_release"),
        "today": datetime.now().strftime("%Y-%m-%d"),
    }

    user_prompt = f"""Analyze these VEPs using their combined context data:

{json.dumps(context, indent=2, default=str)}

For each VEP:
1. Review all context fields (deadline, activity, compliance, exceptions)
2. Perform cross-domain reasoning to identify risks
3. Update analysis with combined_insights, priority, risk_factors, recommended_actions
4. Generate alerts for issues needing attention

Return updated VEPs with complete analysis, general_insights list, and sheets_need_update decision."""

    # Invoke powerful LLM (Pro) for deep analysis
    result = invoke_llm_check("analyze_combined", context, system_prompt, user_prompt, AnalyzeCombinedResponse)

    updated_veps = result.updated_veps

    # Preserve VEPs that LLM might have dropped
    if len(updated_veps) < len(veps):
        log(f"Warning: LLM returned {len(updated_veps)} VEP(s), expected {len(veps)}. Preserving all.", node="analyze_combined", level="WARNING")
        existing_names = {vep.name for vep in updated_veps}
        for vep in veps:
            if vep.name not in existing_names:
                log(f"Preserving dropped VEP {vep.name}", node="analyze_combined", level="DEBUG")
                updated_veps.append(vep)

    # Determine sheets update need
    skip_monitoring = state.get("skip_monitoring", False)
    sheets_need_update = True if skip_monitoring else result.sheets_need_update

    # Log results
    if result.alerts:
        log(f"Analysis generated {len(result.alerts)} alert(s)", node="analyze_combined")
    if result.general_insights:
        log(f"Generated {len(result.general_insights)} general insight(s)", node="analyze_combined")

    log(f"Sheets update needed: {sheets_need_update}", node="analyze_combined")

    return {
        "last_check_times": last_check_times,
        "veps": updated_veps,
        "alerts": result.alerts,  # Pass alerts to alert_summary for notification
        "general_insights": result.general_insights,
        "sheets_need_update": sheets_need_update,
    }
