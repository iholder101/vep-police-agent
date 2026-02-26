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
from services.indexer import create_indexed_context
from nodes.alert_formatting import build_vep_summary_table, build_markdown_table


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

    # Get indexed context for phase-specific analysis
    index_cache_minutes = state.get("index_cache_minutes", 60)
    indexed_context = create_indexed_context(cache_max_age_minutes=index_cache_minutes)

    release_phase = indexed_context.get("release_phase", "unknown")
    release_deadlines = indexed_context.get("release_deadlines", {})
    board_veps = indexed_context.get("board_veps", {})

    # Extract phase_risks from VEPs for focused analysis
    phase_risks = []
    for vep in veps:
        risk_data = vep.context.phase_risks if hasattr(vep.context, 'phase_risks') else {}
        if risk_data and risk_data.get("has_risks"):
            phase_risks.append({
                "vep_id": vep.tracking_issue_id,
                "vep_name": vep.name,
                "vep_title": vep.title,
                **risk_data
            })

    # Build phase-specific context summary for prompt
    phase_summary = {
        "release_phase": release_phase,
        "release_deadlines": release_deadlines,
        "board_veps_count": len(board_veps),
        "phase_risks_count": len(phase_risks),
    }

    # Build system prompt for comprehensive analysis
    system_prompt = f"""You are a VEP governance analyst. Your job is to analyze VEP status using ALL available context and generate actionable insights.

RELEASE CONTEXT:
- Phase: {release_phase} (design=pre-EF proposal review, development=EF-CF implementation, stabilization=CF-GA testing, post_release=done)
- Deadlines: {json.dumps(release_deadlines, default=str)}
- Board VEPs: {len(board_veps)} VEPs tracked on project board
- Phase-specific risks detected: {len(phase_risks)}

INPUT: Each VEP has raw context data from fetch nodes:
- context.deadline: {{days_until_ef, days_until_cf, ef_passed, cf_passed, vep_merged, target_release}}
- context.activity: {{last_issue_update, last_pr_update, days_since_update, recent_comments, recent_commits}}
- context.compliance: {{pr_state, has_lgtm, has_approved_label, sig_labels, implementation_prs, template sections}}
- context.exceptions: {{exception_issue_number, exception_issue_state, has_post_ef_commits, has_post_cf_commits}}
- context.phase_risks: {{has_risks, phase, proposal_pr, stale_impl_prs, missing_impl_prs, days_to_deadline, risk_level}}

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

6. MERGE PROBABILITY AND REVIEWER SENTIMENT (NEW - PHASE-AWARE):
   For VEPs with context.phase_risks.has_risks = true:

   CRITICAL: RECENCY IS KEY - Only recent activity matters (last 7 days for design, 5 days for development)
   - Old approvals/reviews DON'T COUNT if PR is now stale
   - "Under-reviewed" means NO RECENT progress (no reviews/comments in past stale_days)
   - High days_since_update = stale = lower probability and more negative sentiment

   a) Estimate merge probability (0-100%) by phase deadline:
      - PRIMARY FACTOR: days_since_update (from phase_risks data)
        * 0-2 days inactive: Active work, higher probability (60-90%)
        * 3-7 days inactive: Moderate staleness, medium probability (30-60%)
        * 8-14 days inactive: Very stale, low probability (10-30%)
        * 15+ days inactive: Abandoned, very low probability (0-15%)
      - SECONDARY FACTORS: days_to_deadline, review_count, status on board
      - Design phase: Will proposal PR be reviewed and approved by EF?
      - Development phase: Will implementation PRs be merged by CF?
      - Be realistic: HIGH days_since_update = stale = LOW probability

   b) Summarize reviewer sentiment from RECENT PR activity (last 7/5 days):
      - positive: Recent approvals/LGTM (within stale_days), active discussion, feedback being addressed
      - concerned: Recent questions/change requests, some activity but slow progress
      - blocked: Recent explicit rejection/major blockers, or NO RECENT activity at all
      - neutral: No recent reviews (but also not stale yet), awaiting feedback
      - IMPORTANT: Old activity doesn't count - only consider RECENT timeline

   c) Assess recent progress:
      - Check days_since_update from phase_risks data
      - Determine if there's been recent activity (within stale_days threshold)
      - recent_progress: true if days_since_update <= 7 (design) or <= 5 (development)
      - recent_progress: false if days_since_update > threshold (stale)

   d) Step-by-step reasoning:
      - START with days_since_update (PRIMARY factor)
      - Analyze the specific risk (stale proposal, missing impl, etc.)
      - Factor in review_count, but ONLY if recent
      - Consider deadline proximity and urgency
      - Weight recent activity HEAVILY in probability estimate
      - Provide justification for probability estimate

   e) Escalation recommendation:
      - If probability < 50% OR sentiment = blocked OR no recent_progress → recommend escalation
      - Suggest specific actions: ping reviewers, request expedited review, consider exception
      - For stale PRs: emphasize urgency and need for immediate action

   f) Store in vep.analysis["risk_assessment"]:
      {{
        "merge_probability": <int 0-100>,
        "reviewer_sentiment": "<positive|concerned|blocked|neutral>",
        "recent_progress": <bool>,
        "days_inactive": <int from phase_risks>,
        "reasoning": "<step-by-step explanation emphasizing recency>",
        "recommend_escalation": <bool>,
        "escalation_actions": ["<specific action 1>", "<action 2>"]
      }}

7. OUTPUT FOR EACH VEP:
   Update vep.analysis with:
   - combined_insights: string summary of overall status
   - priority: "low", "medium", "high", or "critical"
   - risk_factors: list of identified risks
   - recommended_actions: list of suggested next steps
   - risk_assessment: dict (only for at-risk VEPs) with:
     * merge_probability: int 0-100
     * reviewer_sentiment: positive|concerned|blocked|neutral
     * recent_progress: bool (true if active within stale_days)
     * days_inactive: int (from phase_risks.days_since_update)
     * reasoning: step-by-step explanation emphasizing recency
     * recommend_escalation: bool
     * escalation_actions: list of specific actions

8. GENERAL INSIGHTS (release-wide):
   Return a list of strings covering:
   - Overall release health ("5 of 20 VEPs at risk")
   - Cross-VEP patterns ("Network SIG VEPs are behind")
   - Release-wide recommendations
   - Merge probability summary for at-risk VEPs

9. SHEETS UPDATE DECISION:
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

    # Log phase risks summary
    log(f"Phase risks count: {len(phase_risks)}, VEPs count: {len(veps)}", node="analyze_combined")

    # Process VEPs in batches to avoid LLM output token limits
    BATCH_SIZE = 7
    all_updated_veps = []
    all_alerts = []
    all_insights = []
    sheets_need_update_any = False

    vep_batches = [veps[i:i + BATCH_SIZE] for i in range(0, len(veps), BATCH_SIZE)]
    log(f"Processing {len(veps)} VEPs in {len(vep_batches)} batch(es) of up to {BATCH_SIZE}", node="analyze_combined")

    for batch_idx, batch_veps in enumerate(vep_batches):
        batch_num = batch_idx + 1
        log(f"Analyzing batch {batch_num}/{len(vep_batches)}: {len(batch_veps)} VEPs ({', '.join(v.name for v in batch_veps)})", node="analyze_combined")

        # Build batch-specific context
        batch_phase_risks = [r for r in phase_risks if r["vep_name"] in {v.name for v in batch_veps}]

        release_schedule = state.get("release_schedule")
        batch_context = {
            "veps": [vep.model_dump(mode='json') for vep in batch_veps],
            "release_schedule": release_schedule.model_dump(mode='json') if release_schedule else None,
            "current_release": state.get("current_release"),
            "today": datetime.now().strftime("%Y-%m-%d"),
            "phase_summary": phase_summary,
            "phase_risks": batch_phase_risks,
        }

        batch_user_prompt = f"""Analyze these VEPs using their combined context data:

PHASE-SPECIFIC RISKS DETECTED ({len(batch_phase_risks)} VEPs in this batch):
{json.dumps(batch_phase_risks, indent=2, default=str) if batch_phase_risks else "None"}

ALL VEPS WITH FULL CONTEXT (batch {batch_num}/{len(vep_batches)}):
{json.dumps(batch_context, indent=2, default=str)}

ANALYSIS INSTRUCTIONS:

For EACH VEP:
1. Review all context fields (deadline, activity, compliance, exceptions, phase_risks)
2. Perform cross-domain reasoning to identify risks
3. Update analysis with combined_insights, priority, risk_factors, recommended_actions

For VEPs with context.phase_risks.has_risks = true:
4. Estimate merge probability (0-100%) considering ALL factors holistically:
   - Weight staleness (days_since_update) heavily — a PR with no activity for weeks is much
     less likely to merge by deadline than one updated yesterday
   - Also consider: review status (approvals, LGTM), test results, unresolved review comments,
     number of merged vs open PRs, and deadline proximity
   - A stale PR with 5 approvals and passing tests is very different from a stale PR
     with unresolved review comments
5. Assess recent_progress: true if there has been meaningful activity recently, false if stale
6. Determine reviewer_sentiment based on the overall review trajectory
7. Provide step-by-step reasoning explaining your probability estimate
8. Recommend escalation if probability < 50% OR blocked OR no recent_progress
9. Store in vep.analysis["risk_assessment"] with recent_progress and days_inactive fields

For VEPs where ALL implementation PRs are merged:
- Set merge_probability to 90-100%
- Set reviewer_sentiment to "positive"
- Set recent_progress to true
- These VEPs are ON TRACK

REMEMBER: Recency matters. Stale PRs with no activity should generally have lower probability,
but use your judgment — a well-reviewed PR awaiting a routine merge is different from an abandoned one.

For ALL VEPs:
11. Generate alerts for issues needing attention

Return updated VEPs with complete analysis, general_insights list, and sheets_need_update decision."""

        try:
            result = invoke_llm_check("analyze_combined", batch_context, system_prompt, batch_user_prompt, AnalyzeCombinedResponse)

            batch_updated = result.updated_veps

            # Preserve VEPs that LLM might have dropped within this batch
            if len(batch_updated) < len(batch_veps):
                log(f"Batch {batch_num}: LLM returned {len(batch_updated)}/{len(batch_veps)} VEPs, preserving dropped", node="analyze_combined", level="WARNING")
                existing_names = {vep.name for vep in batch_updated}
                for vep in batch_veps:
                    if vep.name not in existing_names:
                        log(f"Preserving dropped VEP {vep.name}", node="analyze_combined", level="DEBUG")
                        batch_updated.append(vep)

            all_updated_veps.extend(batch_updated)
            all_alerts.extend(result.alerts)
            all_insights.extend(result.general_insights)
            if result.sheets_need_update:
                sheets_need_update_any = True

            log(f"Batch {batch_num} complete: {len(batch_updated)} VEPs analyzed, {len(result.alerts)} alerts", node="analyze_combined")

        except Exception as e:
            log(f"Batch {batch_num} failed: {e}. Preserving original VEPs.", node="analyze_combined", level="WARNING")
            all_updated_veps.extend(batch_veps)

    updated_veps = all_updated_veps

    # Generate fallback risk assessments for VEPs that LLM dropped or didn't analyze
    for vep in updated_veps:
        if not hasattr(vep, 'analysis') or not vep.analysis or not vep.analysis.get("risk_assessment"):
            # Build a basic risk assessment from available data
            if not hasattr(vep, 'analysis') or vep.analysis is None:
                vep.analysis = {}

            # Check if all implementation PRs are merged
            all_merged = False
            has_impl_prs = bool(vep.implementation_prs)
            if has_impl_prs:
                # Consider PRs as done if merged, closed, or unknown (unknown = too old to fetch, likely merged)
                all_merged = all(pr.state in ("merged", "closed", "unknown") for pr in vep.implementation_prs)
                # But require at least one PR with confirmed merged/closed state
                any_confirmed = any(pr.state in ("merged", "closed") for pr in vep.implementation_prs)
                all_merged = all_merged and any_confirmed

            if all_merged and has_impl_prs:
                vep.analysis["risk_assessment"] = {
                    "merge_probability": 95,
                    "reviewer_sentiment": "positive",
                    "recent_progress": True,
                    "days_inactive": 0,
                    "reasoning": "All implementation PRs are merged.",
                    "recommend_escalation": False,
                    "escalation_actions": [],
                }
            elif has_impl_prs:
                merged_count = sum(1 for pr in vep.implementation_prs if pr.state == "merged")
                total_count = len(vep.implementation_prs)
                open_count = total_count - merged_count
                prob = max(30, int(70 * merged_count / total_count)) if total_count > 0 else 50
                vep.analysis["risk_assessment"] = {
                    "merge_probability": prob,
                    "reviewer_sentiment": "neutral",
                    "recent_progress": True,
                    "days_inactive": vep.activity.days_since_update if vep.activity else 0,
                    "reasoning": f"{merged_count}/{total_count} implementation PRs merged, {open_count} still open.",
                    "recommend_escalation": prob < 50,
                    "escalation_actions": ["Review open PRs"] if prob < 50 else [],
                }
            else:
                days_inactive = vep.activity.days_since_update if vep.activity else 0
                vep.analysis["risk_assessment"] = {
                    "merge_probability": 50,
                    "reviewer_sentiment": "neutral",
                    "recent_progress": days_inactive < 14,
                    "days_inactive": days_inactive,
                    "reasoning": "No implementation PRs found. Status based on available data.",
                    "recommend_escalation": days_inactive > 14,
                    "escalation_actions": ["Identify and track implementation PRs"] if days_inactive > 14 else [],
                }
            log(f"Generated fallback risk assessment for {vep.name}: prob={vep.analysis['risk_assessment']['merge_probability']}%", node="analyze_combined", level="DEBUG")

    # Post-LLM correction: override risk assessment when actual PR states contradict LLM
    for vep in updated_veps:
        if not hasattr(vep, 'analysis') or not vep.analysis or not vep.analysis.get("risk_assessment"):
            continue
        has_impl_prs = bool(vep.implementation_prs)
        if not has_impl_prs:
            continue

        # Count PRs by state category
        done_states = ("merged", "closed")
        open_count = sum(1 for pr in vep.implementation_prs if pr.state == "open")
        done_count = sum(1 for pr in vep.implementation_prs if pr.state in done_states)
        unknown_count = sum(1 for pr in vep.implementation_prs if pr.state not in (*done_states, "open"))
        total_count = len(vep.implementation_prs)
        # No explicitly open PRs = all done (unknown state = old PR likely merged)
        all_merged = open_count == 0 and done_count > 0
        ra = vep.analysis["risk_assessment"]
        prob = ra.get("merge_probability", 100)

        # All PRs merged/closed (none open) but LLM gave low probability — correct it
        if all_merged and prob < 80:
            old_prob = prob
            ra["merge_probability"] = 95
            ra["reviewer_sentiment"] = "positive"
            ra["recent_progress"] = True
            ra["recommend_escalation"] = False
            ra["escalation_actions"] = []
            ra["reasoning"] = f"All {total_count} implementation PRs are done ({done_count} merged/closed, {unknown_count} unconfirmed). (LLM estimated {old_prob}%, corrected)"
            log(f"Corrected {vep.name}: no open impl PRs ({done_count} merged/closed, {unknown_count} unknown), probability {old_prob}% → 95%", node="analyze_combined")

        # No confirmed open PRs, but all are "unknown" state (old PRs outside prs_index window)
        # Treat as likely done — don't penalize VEPs for stale PR data
        elif open_count == 0 and done_count == 0 and unknown_count > 0 and prob < 80:
            old_prob = prob
            ra["merge_probability"] = 80
            ra["recent_progress"] = True
            ra["recommend_escalation"] = False
            ra["reasoning"] = f"All {total_count} impl PRs have unconfirmed state (none open). (LLM estimated {old_prob}%, adjusted)"
            log(f"Adjusted {vep.name}: no open or confirmed-done PRs ({unknown_count} unconfirmed), probability {old_prob}% → 80%", node="analyze_combined")

        # Some PRs still open — boost if most are done
        elif not all_merged:
            merged_count = done_count
            total_count = len(vep.implementation_prs)
            if merged_count > 0 and prob < 50:
                floor = max(50, int(80 * merged_count / total_count))
                if floor > prob:
                    ra["merge_probability"] = floor
                    ra["reasoning"] = f"{merged_count}/{total_count} impl PRs merged. (LLM estimated {prob}%, adjusted to {floor}%)"
                    log(f"Adjusted {vep.name}: {merged_count}/{total_count} impl PRs merged, probability {prob}% → {floor}%", node="analyze_combined")

    # Determine sheets update need
    skip_monitoring = state.get("skip_monitoring", False)
    sheets_need_update = True if skip_monitoring else sheets_need_update_any

    # Count VEPs with risk assessments
    risk_assessments = sum(
        1 for vep in updated_veps
        if hasattr(vep, 'analysis') and vep.analysis and vep.analysis.get("risk_assessment")
    )

    # Log results
    if result.alerts:
        log(f"Analysis generated {len(result.alerts)} alert(s)", node="analyze_combined")
    if result.general_insights:
        log(f"Generated {len(result.general_insights)} general insight(s)", node="analyze_combined")
    if risk_assessments > 0:
        log(f"Risk assessments with merge probability: {risk_assessments} VEP(s)", node="analyze_combined")

        # Log high-risk VEPs (low merge probability, blocked, or no recent progress)
        high_risk_veps = []
        stale_veps = []
        for vep in updated_veps:
            if hasattr(vep, 'analysis') and vep.analysis and vep.analysis.get("risk_assessment"):
                ra = vep.analysis["risk_assessment"]
                prob = ra.get("merge_probability", 100)
                sentiment = ra.get("reviewer_sentiment", "unknown")
                recent_progress = ra.get("recent_progress", True)
                days_inactive = ra.get("days_inactive", 0)

                # Track high-risk VEPs
                if prob < 50 or sentiment == "blocked":
                    high_risk_veps.append(f"{vep.name} ({prob}%, {sentiment})")

                # Track stale VEPs (no recent progress)
                if not recent_progress or days_inactive > 7:
                    stale_veps.append(f"{vep.name} ({days_inactive}d inactive)")

        if high_risk_veps:
            log(f"High-risk VEPs: {', '.join(high_risk_veps)}", node="analyze_combined", level="WARNING")

        if stale_veps:
            log(f"Stale VEPs (no recent progress): {', '.join(stale_veps)}", node="analyze_combined", level="WARNING")

        # Build and log VEP summary table for visibility
        # Also stored in state so downstream nodes (send_slack, send_email) reuse it
        vep_summary_table = []
        try:
            vep_summary_table = build_vep_summary_table(updated_veps, indexed_context)
            if vep_summary_table:
                log("=" * 80, node="analyze_combined")
                log("VEP SUMMARY TABLE (Post-Analysis)", node="analyze_combined")
                log("=" * 80, node="analyze_combined")

                table_text = build_markdown_table(vep_summary_table)
                for line in table_text.split("\n"):
                    log(line, node="analyze_combined")

                log("=" * 80, node="analyze_combined")

                # Log high-risk summary
                red_count = sum(1 for row in vep_summary_table if row["urgency"] == "RED")
                yellow_count = sum(1 for row in vep_summary_table if row["urgency"] == "YELLOW")
                green_count = sum(1 for row in vep_summary_table if row["urgency"] == "GREEN")
                log(f"Summary: {red_count} HIGH RISK (RED), {yellow_count} MEDIUM RISK (YELLOW), {green_count} ON TRACK (GREEN)", node="analyze_combined")
        except Exception as e:
            log(f"Failed to build VEP summary table: {e}", node="analyze_combined", level="WARNING")

    log(f"Sheets update needed: {sheets_need_update}", node="analyze_combined")

    return {
        "last_check_times": last_check_times,
        "veps": updated_veps,
        "alerts": all_alerts,  # Pass alerts to alert_summary for notification
        "general_insights": all_insights,
        "sheets_need_update": sheets_need_update,
        "vep_summary_table": vep_summary_table,
    }
