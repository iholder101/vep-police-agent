"""Analysis node - performs ALL reasoning about VEP status and generates alerts.

This is the ONLY analysis node. Fetch nodes (check_*) only gather raw data.
This node has access to ALL context at once and does holistic reasoning.
"""

import json
from datetime import datetime, date, time, timezone
from typing import Any, List, Optional, Tuple
from state import VEPState, PRInfo
from services.utils import log
from services.llm_helper import invoke_llm_check
from services.response_models import CheckResponse
from services.indexer import create_indexed_context
from nodes.alert_formatting import build_vep_summary_table, build_markdown_table


def classify_prs_by_release(impl_prs: List[PRInfo], cutoff: datetime) -> Tuple[List[PRInfo], List[PRInfo]]:
    """Classify implementation PRs as current-release vs previous-release work.

    The cutoff is the cycle start date (first date in the release schedule, or
    previous CF + 14d fallback). PRs merged before the cutoff belong to the
    previous release; PRs merged after or still open belong to the current release.

    Returns (current_release_prs, previous_release_prs).
    Closed-not-merged (abandoned) PRs are excluded from both lists.
    Unknown-state PRs are treated as current-release (conservative) to prevent
    false 10% assignments when PR data is incomplete.
    """
    current, previous = [], []
    for pr in impl_prs:
        if pr.state == "open":
            current.append(pr)
        elif pr.state == "merged" and pr.merged_at:
            if pr.merged_at > cutoff:
                current.append(pr)
            else:
                previous.append(pr)
        elif pr.state == "merged":
            # merged_at not available - can't classify, treat as current (conservative)
            current.append(pr)
        elif pr.state == "closed":
            pass  # closed-not-merged (abandoned): excluded from both lists
        else:
            # unknown or unrecognized state: treat as current (conservative)
            current.append(pr)
    return current, previous


def _build_previous_release_override(
    num_previous_prs: int, cutoff_str: str, phase_info: str, old_prob: Optional[int] = None
) -> dict:
    """Build risk assessment dict for VEPs with only previous-release PRs."""
    reasoning = (
        f"All {num_previous_prs} implementation PRs merged before cycle start "
        f"({cutoff_str}), no current-release activity. "
        f"Promotion phase: {phase_info}."
    )
    if old_prob is not None:
        reasoning += f" (LLM estimated {old_prob}%)"
    return {
        "merge_probability": 10,
        "reviewer_sentiment": "concerned",
        "recent_progress": False,
        "days_inactive": 0,  # no current-release PRs to measure staleness on
        "reasoning": reasoning,
        "recommend_escalation": True,
        "escalation_actions": ["Open implementation PRs for current release"],
    }


class AnalyzeCombinedResponse(CheckResponse):
    """Response model for combined analysis."""
    sheets_need_update: bool = False
    general_insights: List[str] = []


def _fallback_design_phase(vep) -> dict:
    """Build a fallback risk assessment appropriate for the design phase.

    During design phase the relevant question is whether the PROPOSAL PR
    will be reviewed and approved by VEP freeze, not whether implementation
    PRs are merged (open impl PRs are normal and even ahead-of-schedule).
    """
    days_inactive = vep.activity.days_since_update if vep.activity else 0
    has_proposal_pr = bool(vep.enhancement_prs)
    proposal_merged = any(pr.state == "merged" or pr.merged for pr in vep.enhancement_prs) if has_proposal_pr else False
    vep_merged = getattr(vep.compliance, 'vep_merged', False) or vep.context.deadline.get("vep_merged", False)
    has_impl_prs = bool(vep.implementation_prs)

    if vep_merged or proposal_merged:
        # Proposal already accepted — ahead of schedule
        bonus = 5 if has_impl_prs else 0
        prob = min(95 + bonus, 100)
        reasoning = "Proposal PR merged during design phase."
        if has_impl_prs:
            reasoning += " Implementation PRs already in progress (ahead of schedule)."
        return {
            "merge_probability": prob,
            "reviewer_sentiment": "positive",
            "recent_progress": True,
            "days_inactive": days_inactive,
            "reasoning": reasoning,
            "recommend_escalation": False,
            "escalation_actions": [],
        }

    if has_proposal_pr:
        # Proposal PR open — normal during design phase
        if days_inactive <= 7:
            prob = 75
            sentiment = "neutral"
            reasoning = "Proposal PR open with recent activity. Design phase — on track."
        elif days_inactive <= 14:
            prob = 60
            sentiment = "concerned"
            reasoning = f"Proposal PR open but {days_inactive} days inactive. May need reviewer attention."
        else:
            prob = 45
            sentiment = "concerned"
            reasoning = f"Proposal PR open and stale ({days_inactive} days inactive). Needs attention before VEP freeze."
        if has_impl_prs:
            prob = min(prob + 5, 100)
            reasoning += " Implementation PRs already in progress."
        return {
            "merge_probability": prob,
            "reviewer_sentiment": sentiment,
            "recent_progress": days_inactive <= 14,
            "days_inactive": days_inactive,
            "reasoning": reasoning,
            "recommend_escalation": prob < 50,
            "escalation_actions": ["Ping proposal reviewers"] if prob < 50 else [],
        }

    # No proposal PR at all
    if has_impl_prs:
        prob = 55
        reasoning = "No proposal PR found but implementation PRs exist. May need proposal PR before VEP freeze."
    else:
        prob = 50
        reasoning = "No proposal or implementation PRs found. Status based on available data."
    return {
        "merge_probability": prob,
        "reviewer_sentiment": "neutral",
        "recent_progress": days_inactive < 14,
        "days_inactive": days_inactive,
        "reasoning": reasoning,
        "recommend_escalation": days_inactive > 14,
        "escalation_actions": ["Create proposal PR for VEP freeze"] if not has_proposal_pr else [],
    }


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

    # Parse cycle start date for release-aware PR classification
    cycle_start_str = indexed_context.get("cycle_start_date")
    release_cutoff: Optional[datetime] = None
    if cycle_start_str:
        try:
            release_cutoff = datetime.combine(
                date.fromisoformat(cycle_start_str), time.min, tzinfo=timezone.utc
            )
            log(f"Release cutoff for PR classification: cycle_start_date={cycle_start_str}", node="analyze_combined")
        except (ValueError, TypeError):
            log(f"Could not parse cycle_start_date: {cycle_start_str}", node="analyze_combined", level="WARNING")
    else:
        log("Release-aware PR classification disabled: no cycle_start_date available", node="analyze_combined", level="WARNING")

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

6. MERGE PROBABILITY AND REVIEWER SENTIMENT (FOR EVERY VEP):
   You MUST generate a risk_assessment for EVERY VEP, not just flagged ones.
   Skip VEPs marked with "_deterministic_risk" in their analysis — those are pre-assessed.

   CRITICAL: RECENCY IS KEY - Only recent activity matters (last 7 days for design, 5 days for development)
   - Old approvals/reviews DON'T COUNT if PR is now stale
   - "Under-reviewed" means NO RECENT progress (no reviews/comments in past stale_days)
   - High days_since_update = stale = lower probability and more negative sentiment

   a) Estimate merge probability (0-100%) by phase deadline:
      - PRIMARY FACTOR: days_since_update
        * 0-2 days inactive: Active work, higher probability (60-90%)
        * 3-7 days inactive: Moderate staleness, medium probability (30-60%)
        * 8-14 days inactive: Very stale, low probability (10-30%)
        * 15+ days inactive: Abandoned, very low probability (0-15%)
      - SECONDARY FACTORS: days_to_deadline, review_count, status on board
      - PHASE-SPECIFIC SCORING:
        * Design phase: Score based on PROPOSAL PR status ONLY. Implementation PRs
          are irrelevant during design phase (not expected until after EF).
          Merged proposal = on track (90-95%). Open active proposal = moderate (60-75%).
        * Development phase: Will implementation PRs be merged by CF?
      - Be realistic: HIGH days_since_update = stale = LOW probability

   b) Summarize reviewer sentiment from RECENT PR activity (last 7/5 days):
      - positive: Recent approvals/LGTM (within stale_days), active discussion, feedback being addressed
      - concerned: Recent questions/change requests, some activity but slow progress
      - blocked: Recent explicit rejection/major blockers, or NO RECENT activity at all
      - neutral: No recent reviews (but also not stale yet), awaiting feedback
      - IMPORTANT: Old activity doesn't count - only consider RECENT timeline

   c) Assess recent progress:
      - Determine if there's been meaningful activity recently
      - recent_progress: true if days_since_update <= 7 (design) or <= 5 (development)
      - recent_progress: false if days_since_update > threshold (stale)

   d) Step-by-step reasoning:
      - START with days_since_update (PRIMARY factor)
      - Analyze PR state, review status, and activity
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
        "days_inactive": <int>,
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

    # Pre-fill deterministic RED risk_assessment for stale VEPs detected by check_phase_risks.
    # These are worst-case: stale (>7d) AND under-reviewed — no LLM needed.
    stale_vep_names = set()
    for risk in phase_risks:
        if not risk.get("has_risks"):
            continue
        vep_name = risk["vep_name"]
        vep_id = risk["vep_id"]
        stale_vep_names.add(vep_name)

        # Find the VEP object and pre-fill
        for vep in veps:
            if vep.tracking_issue_id == vep_id:
                if not hasattr(vep, 'analysis') or vep.analysis is None:
                    vep.analysis = {}
                phase = risk.get("phase", release_phase)
                proposal_pr = risk.get("proposal_pr", {})
                stale_impl_prs = risk.get("stale_impl_prs", [])
                days_inactive = (proposal_pr.get("days_since_update")
                                 if proposal_pr else
                                 max((p.get("days_since_update", 0) for p in stale_impl_prs), default=0))
                days_to_deadline = risk.get("days_to_deadline", 0)

                if phase == "design":
                    pr_num = proposal_pr.get("number", "?")
                    review_count = proposal_pr.get("review_count", 0)
                    reasoning = (f"Proposal PR #{pr_num} is stale ({days_inactive} days inactive) "
                                 f"with only {review_count} review(s). "
                                 f"EF deadline in {days_to_deadline} days. Needs immediate attention.")
                else:
                    stale_nums = [f"#{p.get('number', '?')}" for p in stale_impl_prs]
                    reasoning = (f"Implementation PR(s) {', '.join(stale_nums)} stale ({days_inactive}+ days inactive). "
                                 f"CF deadline in {days_to_deadline} days. Needs immediate attention.")

                vep.analysis["risk_assessment"] = {
                    "merge_probability": max(5, 30 - days_inactive),
                    "reviewer_sentiment": "blocked",
                    "recent_progress": False,
                    "days_inactive": days_inactive,
                    "reasoning": reasoning,
                    "recommend_escalation": True,
                    "escalation_actions": ["Ping reviewers immediately", "Request expedited review", "Consider exception if near deadline"],
                }
                vep.analysis["_deterministic_risk"] = True
                log(f"Deterministic RED for {vep_name}: stale {phase} phase risk, prob={vep.analysis['risk_assessment']['merge_probability']}%",
                    node="analyze_combined")
                break

    if stale_vep_names:
        log(f"Pre-filled {len(stale_vep_names)} stale VEP(s) with deterministic RED: {', '.join(sorted(stale_vep_names))}", node="analyze_combined")

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
        # Strip raw board impl_prs from LLM context to prevent backport PR leaks.
        # The filtered implementation_prs field is the authoritative source.
        veps_data = []
        for vep in batch_veps:
            d = vep.model_dump(mode='json')
            if release_phase == "design":
                d.pop("implementation_prs", None)
            board = d.get("board_fields")
            if isinstance(board, dict):
                board.pop("impl_prs", None)
            veps_data.append(d)

        batch_context = {
            "veps": veps_data,
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

For EVERY VEP (skip any with "_deterministic_risk" in analysis):
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

RELEASE-AWARENESS: PRs merged before the cycle start date ({cycle_start_str or 'unknown'})
are PREVIOUS-RELEASE work, not current-release progress. Ignore previous-release PRs when assessing
completeness - only current-release PRs matter.
During development/stabilization phases, a VEP with no current-release implementation PRs should
have LOW probability (around 10%).
During design phase, implementation PRs are IRRELEVANT - score based on proposal PR status only.
Note: implementation_prs are intentionally stripped from VEP data during design phase.

For VEPs where ALL CURRENT-RELEASE implementation PRs are merged:
- If the VEP proposal is also merged OR we are past the relevant freeze: set merge_probability to 100%
- Otherwise: set merge_probability to 95%
- Set reviewer_sentiment to "positive"
- Set recent_progress to true
- These VEPs are ON TRACK or FULLY LANDED

REMEMBER: Recency matters. Stale PRs with no activity should generally have lower probability,
but use your judgment — a well-reviewed PR awaiting a routine merge is different from an abandoned one.

For ALL VEPs:
11. Generate alerts for issues needing attention

Return updated VEPs with complete analysis, general_insights list, and sheets_need_update decision."""

        try:
            result = invoke_llm_check("analyze_combined", batch_context, system_prompt, batch_user_prompt, AnalyzeCombinedResponse)

            batch_updated = result.updated_veps

            # Carry over fields from original VEPs that the LLM can't produce.
            # The LLM returns fresh VEPInfo objects with analysis filled in,
            # but without implementation_prs, enhancement_prs, board_fields, etc.
            originals_by_id = {vep.tracking_issue_id: vep for vep in batch_veps}
            for updated_vep in batch_updated:
                original = originals_by_id.get(updated_vep.tracking_issue_id)
                if not original:
                    continue
                # Preserve deterministic risk_assessment — don't let LLM overwrite
                orig_analysis = original.analysis if hasattr(original, 'analysis') and original.analysis else {}
                if orig_analysis.get("_deterministic_risk"):
                    if not hasattr(updated_vep, 'analysis') or updated_vep.analysis is None:
                        updated_vep.analysis = {}
                    updated_vep.analysis["risk_assessment"] = orig_analysis["risk_assessment"]
                    updated_vep.analysis["_deterministic_risk"] = True
                if original.implementation_prs:
                    updated_vep.implementation_prs = original.implementation_prs
                if original.enhancement_prs:
                    updated_vep.enhancement_prs = original.enhancement_prs
                if original.board_fields:
                    updated_vep.board_fields = original.board_fields
                if not updated_vep.context.deadline and original.context.deadline:
                    updated_vep.context.deadline = original.context.deadline
                if not updated_vep.context.activity and original.context.activity:
                    updated_vep.context.activity = original.context.activity
                if not updated_vep.context.compliance and original.context.compliance:
                    updated_vep.context.compliance = original.context.compliance
                if not updated_vep.context.exceptions and original.context.exceptions:
                    updated_vep.context.exceptions = original.context.exceptions
                if not updated_vep.context.phase_risks and original.context.phase_risks:
                    updated_vep.context.phase_risks = original.context.phase_risks
                if updated_vep.tracking_issue is None and original.tracking_issue is not None:
                    updated_vep.tracking_issue = original.tracking_issue

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

    # Generate fallback risk assessments for VEPs that LLM genuinely dropped or failed.
    # This should be rare now — LLM generates risk_assessment for all VEPs,
    # and stale VEPs get deterministic RED pre-filled.
    fallback_count = 0
    for vep in updated_veps:
        if not hasattr(vep, 'analysis') or not vep.analysis or not vep.analysis.get("risk_assessment"):
            # Build a basic risk assessment from available data
            if not hasattr(vep, 'analysis') or vep.analysis is None:
                vep.analysis = {}
            fallback_count += 1

            # Design phase: impl PRs are irrelevant, score by proposal PR status
            if release_phase == "design":
                vep.analysis["risk_assessment"] = _fallback_design_phase(vep)
                log(f"Generated fallback risk assessment for {vep.name}: prob={vep.analysis['risk_assessment']['merge_probability']}%", node="analyze_combined", level="DEBUG")
                continue

            # Determine effective PRs: filter to current-release only when cutoff available
            all_merged = False
            has_impl_prs = bool(vep.implementation_prs)
            effective_prs = vep.implementation_prs
            if has_impl_prs and release_cutoff:
                current_prs, previous_prs = classify_prs_by_release(vep.implementation_prs, release_cutoff)
                if previous_prs and not current_prs:
                    phase_info = getattr(vep.current_milestone, 'promotion_phase', 'Net New')
                    vep.analysis["risk_assessment"] = _build_previous_release_override(
                        len(previous_prs), cycle_start_str or "unknown", phase_info
                    )
                    log(f"Fallback: {vep.name} has only previous-release PRs ({len(previous_prs)}), prob=10%", node="analyze_combined")
                    continue
                elif current_prs:
                    effective_prs = current_prs

            if has_impl_prs:
                # Consider PRs as done if merged, closed, or unknown (unknown = too old to fetch, likely merged)
                all_merged = all(pr.state in ("merged", "closed", "unknown") for pr in effective_prs)
                # But require at least one PR with confirmed merged/closed state
                any_confirmed = any(pr.state in ("merged", "closed") for pr in effective_prs)
                all_merged = all_merged and any_confirmed

            if all_merged and has_impl_prs:
                # Determine if VEP is definitively done (proposal merged or past freeze)
                vep_merged = getattr(vep.compliance, 'vep_merged', False) or vep.context.deadline.get("vep_merged", False)
                past_freeze = vep.context.deadline.get("ef_passed", False) or vep.context.deadline.get("cf_passed", False)
                prob = 100 if (vep_merged or past_freeze) else 95
                vep.analysis["risk_assessment"] = {
                    "merge_probability": prob,
                    "reviewer_sentiment": "positive",
                    "recent_progress": True,
                    "days_inactive": 0,
                    "reasoning": "All implementation PRs are merged." + (" VEP is fully landed." if prob == 100 else ""),
                    "recommend_escalation": False,
                    "escalation_actions": [],
                }
            elif has_impl_prs:
                merged_count = sum(1 for pr in effective_prs if pr.state == "merged")
                total_count = len(effective_prs)
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

    if fallback_count > 0:
        log(f"Fallback risk assessments generated for {fallback_count} VEP(s) (LLM did not produce risk_assessment for these)", node="analyze_combined", level="WARNING")

    # Post-LLM correction: override risk assessment when actual PR states contradict LLM
    for vep in updated_veps:
        if not hasattr(vep, 'analysis') or not vep.analysis or not vep.analysis.get("risk_assessment"):
            continue

        # Design phase: impl PRs are irrelevant, override with proposal-based scoring
        if release_phase == "design" and not vep.analysis.get("_deterministic_risk"):
            ra = vep.analysis["risk_assessment"]
            design_assessment = _fallback_design_phase(vep)
            old_prob = ra.get("merge_probability", 0)
            design_prob = design_assessment["merge_probability"]
            ra.update(design_assessment)
            if old_prob != design_prob:
                log(f"Design phase override for {vep.name}: {old_prob}% → {design_prob}%", node="analyze_combined")
            continue

        has_impl_prs = bool(vep.implementation_prs)
        if not has_impl_prs:
            continue

        # Determine effective PRs: filter to current-release only when cutoff available
        effective_prs = vep.implementation_prs
        if release_cutoff:
            current_prs, previous_prs = classify_prs_by_release(vep.implementation_prs, release_cutoff)
            if previous_prs and not current_prs:
                ra = vep.analysis["risk_assessment"]
                prob = ra.get("merge_probability", 100)
                if prob > 10:
                    old_prob = prob
                    phase_info = getattr(vep.current_milestone, 'promotion_phase', 'Net New')
                    override = _build_previous_release_override(
                        len(previous_prs), cycle_start_str or "unknown", phase_info, old_prob=old_prob
                    )
                    ra.update(override)
                    log(f"Corrected {vep.name}: only previous-release PRs ({len(previous_prs)}), probability {old_prob}% → 10%", node="analyze_combined")
                continue
            elif current_prs:
                effective_prs = current_prs

        # Count PRs by state category (using current-release PRs only)
        done_states = ("merged", "closed")
        open_count = sum(1 for pr in effective_prs if pr.state == "open")
        done_count = sum(1 for pr in effective_prs if pr.state in done_states)
        unknown_count = sum(1 for pr in effective_prs if pr.state not in (*done_states, "open"))
        total_count = len(effective_prs)
        # No explicitly open PRs = all done (unknown state = old PR likely merged)
        all_merged = open_count == 0 and done_count > 0
        ra = vep.analysis["risk_assessment"]
        prob = ra.get("merge_probability", 100)

        # All PRs merged/closed and VEP is definitively done — ensure 100%
        vep_merged = getattr(vep.compliance, 'vep_merged', False) or vep.context.deadline.get("vep_merged", False)
        past_freeze = vep.context.deadline.get("ef_passed", False) or vep.context.deadline.get("cf_passed", False)
        if all_merged and prob < 100 and (vep_merged or past_freeze):
            old_prob = prob
            ra["merge_probability"] = 100
            ra["reviewer_sentiment"] = "positive"
            ra["recent_progress"] = True
            ra["recommend_escalation"] = False
            ra["escalation_actions"] = []
            ra["reasoning"] = f"All {total_count} implementation PRs are done and VEP is fully landed. (was {old_prob}%)"
            log(f"Corrected {vep.name}: fully landed (all PRs done, {'VEP merged' if vep_merged else 'past freeze'}), probability {old_prob}% → 100%", node="analyze_combined")

        # All PRs merged/closed (none open) but LLM gave low probability — correct to 95%
        elif all_merged and prob < 80:
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
