"""Analysis node - performs ALL reasoning about VEP status and generates alerts.

This is the ONLY analysis node. Fetch nodes (check_*) only gather raw data.
This node has access to ALL context at once and does holistic reasoning.
"""

import json
from datetime import UTC, date, datetime, time
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from nodes.alert_formatting import build_markdown_table, build_vep_summary_table
from services.indexer import create_indexed_context
from services.llm_helper import invoke_llm_check
from services.response_models import (
    AnalyzeAttentionResponse,
    AttentionLevel,
    VEPAttention,
)
from services.utils import log
from state import PRInfo, VEPState

_GOVERNANCE_MODEL_CACHE: str | None = None

# Days without human activity beyond which a VEP/PR is considered stale.
# Used only for the deterministic prefill/fallback layers - the LLM makes its
# own phase-aware judgment via the governance model doc.
_STALE_DAYS = 7


def _load_governance_model() -> str:
    """Load docs/governance-model.md once; return '' if unavailable."""
    global _GOVERNANCE_MODEL_CACHE
    if _GOVERNANCE_MODEL_CACHE is None:
        try:
            doc_path = Path(__file__).resolve().parent.parent / "docs" / "governance-model.md"
            _GOVERNANCE_MODEL_CACHE = doc_path.read_text(encoding="utf-8")
        except OSError as e:
            log(f"Could not load governance-model.md: {e}", node="analyze_combined", level="WARNING")
            _GOVERNANCE_MODEL_CACHE = ""
    return _GOVERNANCE_MODEL_CACHE


def classify_prs_by_release(impl_prs: list[PRInfo], cutoff: datetime) -> tuple[list[PRInfo], list[PRInfo]]:
    """Classify implementation PRs as current-release vs previous-release work.

    The cutoff is the cycle start date (first date in the release schedule, or
    previous CF + 14d fallback). PRs merged before the cutoff belong to the
    previous release; PRs merged after or still open belong to the current release.

    Returns (current_release_prs, previous_release_prs).
    Closed-not-merged (abandoned) PRs are excluded from both lists.
    Unknown-state PRs are treated as current-release (conservative) to prevent
    false exclusions when PR data is incomplete.
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


def _classify_effective_impl_prs(vep, release_cutoff: datetime | None) -> list[PRInfo]:
    """Return the implementation PRs relevant to the CURRENT release cycle.

    If a VEP has implementation PRs but ALL of them predate the cycle start
    (previous-release leftovers), treat it as having none for current-cycle
    attention purposes - the same signal as "no impl PRs yet".
    """
    impl_prs = vep.implementation_prs or []
    if not impl_prs or not release_cutoff:
        return impl_prs
    current, previous = classify_prs_by_release(impl_prs, release_cutoff)
    if current:
        return current
    if previous:
        return []
    return impl_prs


def _compute_staleness(vep) -> dict[str, Any]:
    """Derive a staleness dict, preferring grounded WS3 conversation data.

    Looks at `days_since_last_human_activity` across all of a VEP's PRs
    (implementation + enhancement) and takes the minimum (freshest signal).
    Falls back to `vep.activity.days_since_update` when no PR has been
    conversation-enriched.
    """
    days: int | None = None
    for pr in list(vep.implementation_prs or []) + list(vep.enhancement_prs or []):
        conversation = getattr(pr, "conversation", None) or {}
        candidate = conversation.get("days_since_last_human_activity")
        if isinstance(candidate, int) and (days is None or candidate < days):
            days = candidate
    if days is None and vep.activity:
        days = vep.activity.days_since_update

    is_stale = days is not None and days > _STALE_DAYS
    stale_reason = f"No human activity for {days} days" if is_stale else None
    return {
        "days_since_human_activity": days,
        "is_stale": is_stale,
        "stale_reason": stale_reason,
    }


def _reason(kind: str, text: str) -> dict[str, str]:
    return {"kind": kind, "text": text}


def _set_vep_attention(vep, attention: dict[str, Any], deterministic: bool = False) -> None:
    """Validate/coerce an attention dict via VEPAttention and store it on the VEP.

    `analysis` stays an untyped dict; this is the single choke point that
    guarantees whatever lands under `analysis["attention"]` matches the
    VEPAttention contract, whether it came from the LLM or a deterministic path.
    """
    if not hasattr(vep, 'analysis') or vep.analysis is None:
        vep.analysis = {}
    validated = VEPAttention.model_validate(attention)
    vep.analysis["attention"] = validated.model_dump(mode="json")
    if deterministic:
        vep.analysis["_deterministic_attention"] = True


def _prefill_phase_risk_attention(
    veps: list, phase_risks: list[dict[str, Any]], release_phase: str, phase_detail: dict[str, Any],
) -> set[str]:
    """Deterministically set attention for VEPs flagged by check_phase_risks.

    These are unambiguous worst cases (stale AND under-reviewed proposal/impl
    PRs, or missing impl PRs late in the phase) - no LLM call needed.

    Returns the set of VEP names that were prefilled.
    """
    prefilled: set[str] = set()
    fraction = phase_detail.get("fraction_through_phase")

    for risk in phase_risks:
        if not risk.get("has_risks"):
            continue
        vep_name = risk["vep_name"]
        vep_id = risk["vep_id"]

        # Skip deterministic prefill for VEPs with merged proposals and low risk
        if risk.get("proposal_merged"):
            risk_level = risk.get("risk_level", "medium")
            if risk_level == "low":
                log(f"Skipping deterministic prefill for {vep_name}: proposal merged, risk_level=low (early in phase)",
                    node="analyze_combined")
                continue

        vep = next((v for v in veps if v.tracking_issue_id == vep_id), None)
        if vep is None:
            continue

        phase = risk.get("phase", release_phase)
        proposal_pr = risk.get("proposal_pr", {})
        stale_impl_prs = risk.get("stale_impl_prs", [])
        days_inactive = (proposal_pr.get("days_since_update")
                         if proposal_pr else
                         max((p.get("days_since_update", 0) for p in stale_impl_prs), default=0))
        days_to_deadline = risk.get("days_to_deadline", 0)
        staleness = {
            "days_since_human_activity": days_inactive,
            "is_stale": days_inactive > _STALE_DAYS,
            "stale_reason": f"No human activity for {days_inactive} days" if days_inactive > _STALE_DAYS else None,
        }

        if risk.get("proposal_merged"):
            # No impl PRs yet, but proposal merged - phase-aware watch/needs_attention.
            late = fraction is not None and fraction >= 0.6
            level = AttentionLevel.NEEDS_ATTENTION if late else AttentionLevel.WATCH
            summary = f"No implementation PRs yet ({days_to_deadline}d to deadline)."
            if fraction is not None:
                summary = f"No implementation PRs yet; {round(fraction * 100)}% through {phase} phase, {days_to_deadline}d to deadline."
            _set_vep_attention(vep, {
                "attention_level": level,
                "attention_reasons": [
                    _reason("coverage", "No implementation PRs yet."),
                    _reason("temporal", f"{days_to_deadline} days to deadline."),
                ],
                "health_summary": summary,
                "suggested_action": "Open implementation PRs for current release." if late else None,
                "staleness": staleness,
                "phase": phase,
            }, deterministic=True)
            prefilled.add(vep_name)
            log(f"Phase-aware prefill for {vep_name}: proposal merged, no impl PRs, level={level.value}",
                node="analyze_combined")
            continue

        if phase == "design":
            pr_num = proposal_pr.get("number", "?")
            review_count = proposal_pr.get("review_count", 0)
            summary = (f"Proposal PR #{pr_num} stale ({days_inactive}d inactive) with only "
                       f"{review_count} review(s). EF deadline in {days_to_deadline} days.")
            reasons = [
                _reason("review", f"Proposal PR #{pr_num}: only {review_count} review(s)."),
                _reason("activity", f"Stale for {days_inactive} days."),
                _reason("temporal", f"EF deadline in {days_to_deadline} days."),
            ]
            if proposal_pr.get("changes_requested_unaddressed"):
                reasons.append(_reason("review", "Changes requested but unaddressed."))
        else:
            stale_nums = ", ".join(f"#{p.get('number', '?')}" for p in stale_impl_prs)
            summary = (f"Implementation PR(s) {stale_nums} stale ({days_inactive}+ days inactive). "
                       f"CF deadline in {days_to_deadline} days.")
            reasons = [
                _reason("activity", f"Impl PR(s) {stale_nums} stale ({days_inactive}+ days)."),
                _reason("temporal", f"CF deadline in {days_to_deadline} days."),
            ]
            if any(p.get("changes_requested_unaddressed") for p in stale_impl_prs):
                reasons.append(_reason("review", "Changes requested but unaddressed on at least one PR."))

        _set_vep_attention(vep, {
            "attention_level": AttentionLevel.NEEDS_ATTENTION,
            "attention_reasons": reasons,
            "health_summary": summary,
            "suggested_action": "Ping reviewers immediately; request expedited review.",
            "staleness": staleness,
            "phase": phase,
        }, deterministic=True)
        prefilled.add(vep_name)
        log(f"Deterministic needs_attention for {vep_name}: stale {phase} phase risk", node="analyze_combined")

    return prefilled


def _generic_fallback_attention(
    vep, release_phase: str, phase_detail: dict[str, Any], release_cutoff: datetime | None,
) -> dict[str, Any]:
    """Deterministic best-effort attention when the LLM didn't produce one.

    This is a safety net for batch failures / dropped VEPs / invalid LLM
    output - the primary reasoning path is the LLM + governance model.
    """
    staleness = _compute_staleness(vep)
    fraction = phase_detail.get("fraction_through_phase")
    late = fraction is not None and fraction >= 0.6

    if release_phase == "design":
        has_proposal_pr = bool(vep.enhancement_prs)
        proposal_merged = any(pr.state == "merged" or pr.merged for pr in vep.enhancement_prs) if has_proposal_pr else False
        vep_merged = getattr(vep.compliance, 'vep_merged', False) or vep.context.deadline.get("vep_merged", False)

        if vep_merged or proposal_merged:
            return {
                "attention_level": AttentionLevel.OK,
                "attention_reasons": [_reason("coverage", "Proposal PR merged.")],
                "health_summary": "Proposal PR merged during design phase.",
                "suggested_action": None,
                "staleness": staleness,
                "phase": release_phase,
            }

        if not has_proposal_pr:
            level = AttentionLevel.NEEDS_ATTENTION if late else AttentionLevel.WATCH
            return {
                "attention_level": level,
                "attention_reasons": [_reason("coverage", "No proposal PR found.")],
                "health_summary": "No proposal or implementation PRs found.",
                "suggested_action": "Create proposal PR before VEP Freeze." if level == AttentionLevel.NEEDS_ATTENTION else None,
                "staleness": staleness,
                "phase": release_phase,
            }

        level = AttentionLevel.NEEDS_ATTENTION if staleness["is_stale"] else AttentionLevel.OK
        summary = "Proposal PR open and stale." if staleness["is_stale"] else "Proposal PR open with recent activity."
        return {
            "attention_level": level,
            "attention_reasons": [_reason("activity", summary)],
            "health_summary": summary,
            "suggested_action": "Ping proposal reviewers." if level == AttentionLevel.NEEDS_ATTENTION else None,
            "staleness": staleness,
            "phase": release_phase,
        }

    # development / stabilization
    effective_prs = _classify_effective_impl_prs(vep, release_cutoff)
    if effective_prs:
        done_states = ("merged", "closed", "unknown")
        all_done = all(pr.state in done_states for pr in effective_prs)
        if all_done:
            return {
                "attention_level": AttentionLevel.OK,
                "attention_reasons": [_reason("coverage", "All implementation PRs are merged/closed.")],
                "health_summary": "All implementation PRs are merged/closed.",
                "suggested_action": None,
                "staleness": staleness,
                "phase": release_phase,
            }
        level = AttentionLevel.NEEDS_ATTENTION if staleness["is_stale"] else AttentionLevel.WATCH
        merged_count = sum(1 for pr in effective_prs if pr.state == "merged")
        summary = f"{merged_count}/{len(effective_prs)} implementation PRs merged."
        return {
            "attention_level": level,
            "attention_reasons": [_reason("coverage", summary)],
            "health_summary": summary,
            "suggested_action": "Review open implementation PRs." if level == AttentionLevel.NEEDS_ATTENTION else None,
            "staleness": staleness,
            "phase": release_phase,
        }

    level = AttentionLevel.NEEDS_ATTENTION if late else AttentionLevel.WATCH
    return {
        "attention_level": level,
        "attention_reasons": [_reason("coverage", "No current-release implementation PRs found.")],
        "health_summary": "No implementation PRs found for the current release.",
        "suggested_action": "Identify and track implementation PRs." if level == AttentionLevel.NEEDS_ATTENTION else None,
        "staleness": staleness,
        "phase": release_phase,
    }


def _apply_all_merged_shortcut(vep, release_phase: str, release_cutoff: datetime | None) -> None:
    """Force attention to "ok" once nothing is left open for the current release.

    Mirrors the pre-attention-contract "fully landed" shortcut: regardless of
    what the LLM (or fallback) guessed, a VEP with no open current-release
    implementation PRs is on track. No-op during design phase (impl PRs are
    irrelevant there) or when there's no attention assessment yet.
    """
    if release_phase == "design":
        return
    if not hasattr(vep, 'analysis') or not vep.analysis or not vep.analysis.get("attention"):
        return

    effective_prs = _classify_effective_impl_prs(vep, release_cutoff)
    if not effective_prs:
        return
    if any(pr.state == "open" for pr in effective_prs):
        return

    attn = vep.analysis["attention"]
    if attn.get("attention_level") == AttentionLevel.OK.value:
        return

    old_level = attn.get("attention_level")
    vep_merged = getattr(vep.compliance, 'vep_merged', False) or vep.context.deadline.get("vep_merged", False)
    past_freeze = vep.context.deadline.get("ef_passed", False) or vep.context.deadline.get("cf_passed", False)
    summary = "All implementation PRs are merged." + (" VEP is fully landed." if (vep_merged or past_freeze) else "")

    _set_vep_attention(vep, {
        "attention_level": AttentionLevel.OK,
        "attention_reasons": [_reason("coverage", summary)],
        "health_summary": summary,
        "compliance_flags": attn.get("compliance_flags", []),
        "suggested_action": None,
        "staleness": attn.get("staleness") or _compute_staleness(vep),
        "phase": release_phase,
    })
    log(f"Landed shortcut for {vep.name}: {old_level} -> ok (all impl PRs merged/closed)", node="analyze_combined")


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
    3. Updates vep.analysis with combined insights and an attention verdict
       (see services.response_models.VEPAttention) - the agent steers
       maintainer attention, it does not predict merge probability.
    4. Generates alerts for issues that need attention
    5. Determines if sheets need updating
    """
    veps = state.get("veps", [])
    log(f"Analyzing {len(veps)} VEP(s) with combined context", node="analyze_combined")

    last_check_times = state.get("last_check_times", {})
    last_check_times["analyze_combined"] = datetime.now(UTC)

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
    phase_detail = indexed_context.get("phase_detail", {})

    # Parse cycle start date for release-aware PR classification
    cycle_start_str = indexed_context.get("cycle_start_date")
    release_cutoff: datetime | None = None
    if cycle_start_str:
        try:
            release_cutoff = datetime.combine(
                date.fromisoformat(cycle_start_str), time.min, tzinfo=UTC
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
    governance_model = _load_governance_model()
    system_prompt = f"""You are a VEP governance analyst. Your job is to decide, for each VEP,
whether a maintainer needs to look at it NOW - and if so, why, and what to do about it.
This is an ATTENTION-STEERING task, NOT a merge-probability prediction. Judge everything
against exactly where we are in the release cycle.

RELEASE CONTEXT:
- Phase: {release_phase} (design=pre-EF proposal review, development=EF-CF implementation, stabilization=CF-GA testing, post_release=done)
- Temporal position: {json.dumps(phase_detail, default=str)}
  (days_into_phase / days_left_in_phase / fraction_through_phase 0.0-1.0 / next_freeze = the upcoming freeze;
   use these to calibrate attention - the SAME state is healthy early in a phase and alarming late in it)
- Deadlines: {json.dumps(release_deadlines, default=str)}
- Board VEPs: {len(board_veps)} VEPs tracked on project board
- Phase-specific risks detected: {len(phase_risks)}

GOVERNANCE MODEL (authoritative reference - anchor your reasoning to this):
{governance_model}

INPUT: Each VEP has raw context data from fetch nodes:
- context.deadline: {{days_until_ef, days_until_cf, ef_passed, cf_passed, vep_merged, target_release}}
- context.activity: {{last_issue_update, last_pr_update, days_since_update, recent_comments, recent_commits}}
- context.compliance: {{pr_state, has_lgtm, has_approved_label, sig_labels, implementation_prs, template sections}}
- context.exceptions: {{exception_issue_number, exception_issue_state, has_post_ef_commits, has_post_cf_commits}}
- context.phase_risks: {{has_risks, phase, proposal_pr, stale_impl_prs, missing_impl_prs, days_to_deadline, risk_level}}
- Each PR (enhancement_prs / implementation_prs) carries a "conversation" dict when enriched:
  reviews, approved_count, changes_requested_count, changes_requested_unaddressed, unaddressed_by,
  last_comment_author, last_comment_date, days_since_last_human_activity, last_commit_pushed_at.
  This is GROUND-TRUTH reviewer sentiment - prefer it over guessing from staleness alone.
- tracking_issue (when populated) carries the VEP's own stated plan/criteria in its body - use it
  to judge whether delivered work covers what was promised.

YOUR ANALYSIS TASKS:

1. DEADLINE RISK ASSESSMENT:
   - Calculate risk level (low/medium/high/critical) based on days to freeze + current progress
   - Flag VEPs at risk of missing deadlines
   - Note if VEP is merged (safe) vs still pending

2. ACTIVITY ANALYSIS:
   - Identify stale VEPs (no human activity for >7 days during active development)
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

6. ATTENTION ASSESSMENT (FOR EVERY VEP):
   You MUST generate an `attention` object for EVERY VEP, not just flagged ones.
   Skip VEPs marked with "_deterministic_attention" in their analysis - those are pre-assessed.

   Signals that mean "needs_attention": no agreement / going nowhere; stale for a while;
   negative reviewer sentiment; changes requested but unaddressed; no impl PRs late in the
   implementation phase; delivered work doesn't cover the criteria the VEP promised; policy
   violations. "watch" = mildly behind, or normal-for-this-point-in-the-phase but worth
   tracking. "ok" = healthy for where we are in the cycle (see the governance model's phase
   playbook).

   a) attention_level: "needs_attention" | "watch" | "ok"
   b) attention_reasons: list of {{"kind": ..., "text": ...}} - kind is one of
      temporal|review|coverage|compliance|activity|board. Be concrete (PR numbers, day
      counts) - not vague.
   c) health_summary: ONE sentence, human-readable, suitable for a project-board comment.
   d) compliance_flags: list of strings for obvious policy violations (may be empty - a
      later workstream fills this in more thoroughly).
   e) suggested_action: ONE concrete next step, or null if attention_level is "ok".
   f) staleness: {{"days_since_human_activity": int|null, "is_stale": bool, "stale_reason": str|null}} -
      derive days_since_human_activity from the freshest PR `conversation.days_since_last_human_activity`
      (fall back to context.activity.days_since_update if no conversation data). is_stale = true if
      that exceeds ~7 days during active phases.
   g) phase: echo the current release phase string.

   This `attention` object becomes this VEP's entry in the top-level `analyses` list (see task 7).

7. OUTPUT FOR EACH VEP:
   Add ONE entry to the top-level `analyses` list per VEP (skip VEPs with
   "_deterministic_attention" set - those are pre-assessed and must NOT be included):
   - tracking_issue_id: the VEP's tracking_issue_id (GitHub issue number)
   - attention: the object built in task 6
   Do NOT echo back the full VEP object - only tracking_issue_id + attention.

8. GENERAL INSIGHTS (release-wide):
   Return a list of strings covering:
   - Overall release health ("5 of 20 VEPs need attention")
   - Cross-VEP patterns ("Network SIG VEPs are behind")
   - Release-wide recommendations

9. SHEETS UPDATE DECISION:
   Set sheets_need_update=True if:
   - Any VEP has attention_level "needs_attention"
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

    # Pre-fill deterministic attention for stale VEPs detected by check_phase_risks.
    # These are worst-case: stale (>7d) AND under-reviewed - no LLM needed.
    prefilled_vep_names = _prefill_phase_risk_attention(veps, phase_risks, release_phase, phase_detail)
    if prefilled_vep_names:
        log(f"Pre-filled {len(prefilled_vep_names)} stale VEP(s) with deterministic attention: {', '.join(sorted(prefilled_vep_names))}", node="analyze_combined")

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
            "today": datetime.now(UTC).strftime("%Y-%m-%d"),
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
1. Review all context fields (deadline, activity, compliance, exceptions, phase_risks) and
   each PR's `conversation` dict.
2. Perform cross-domain reasoning to identify risks (low activity + close deadline = urgent,
   compliance issues + close deadline = critical, post-freeze work + no exception = blocker).

For EVERY VEP (skip any with "_deterministic_attention" in analysis):
3. Decide attention_level (needs_attention/watch/ok) per the governance model's phase
   playbook - judge against fraction_through_phase / days_left_in_phase, not a fixed rule.
4. List concrete attention_reasons (PR numbers, day counts, unaddressed change requests).
5. Write a one-sentence health_summary suitable for a board comment.
6. Note any obvious compliance_flags (policy checklist violations).
7. Give one suggested_action, or null if ok.
8. Populate staleness from PR conversation data (fallback: context.activity.days_since_update).
9. Add ONE entry to `analyses`: {{"tracking_issue_id": <this VEP's tracking_issue_id>,
   "attention": {{attention_level, attention_reasons, health_summary, compliance_flags,
   suggested_action, staleness, phase}}}}.

RELEASE-AWARENESS: PRs merged before the cycle start date ({cycle_start_str or 'unknown'})
are PREVIOUS-RELEASE work, not current-release progress. Ignore previous-release PRs when
judging completeness - only current-release PRs matter.
During design phase, implementation PRs are IRRELEVANT - judge by proposal PR status only.
Note: implementation_prs are intentionally stripped from VEP data during design phase.

For VEPs where ALL current-release implementation PRs are merged (or the VEP itself is
merged / we are past the relevant freeze): attention_level = "ok" - these VEPs are on
track or fully landed.

For ALL VEPs:
10. Generate alerts for issues needing attention.

Return `analyses` (one entry per VEP with tracking_issue_id + attention object, omitting
VEPs with "_deterministic_attention" set), `general_insights`, `alerts`, and
`sheets_need_update`. Do NOT echo back full VEP objects."""

        try:
            result = invoke_llm_check("analyze_combined", batch_context, system_prompt, batch_user_prompt, AnalyzeAttentionResponse)

            # Merge the LLM's lean attention updates onto the ORIGINAL VEP objects
            # (matched by tracking_issue_id) - the LLM no longer echoes full VEPInfo.
            originals_by_id = {vep.tracking_issue_id: vep for vep in batch_veps}
            merged_count = 0
            for update in result.analyses:
                original = originals_by_id.get(update.tracking_issue_id)
                if original is None:
                    log(f"Batch {batch_num}: LLM returned attention for unknown tracking_issue_id {update.tracking_issue_id}",
                        node="analyze_combined", level="WARNING")
                    continue
                orig_analysis = original.analysis if hasattr(original, 'analysis') and original.analysis else {}
                if orig_analysis.get("_deterministic_attention"):
                    # Deterministic prefill wins - don't let the LLM overwrite it.
                    continue
                original.analysis = orig_analysis or {}
                original.analysis["attention"] = update.attention.model_dump(mode="json")
                merged_count += 1

            all_updated_veps.extend(batch_veps)
            all_alerts.extend(result.alerts)
            all_insights.extend(result.general_insights)
            if result.sheets_need_update:
                sheets_need_update_any = True

            log(f"Batch {batch_num} complete: {merged_count}/{len(batch_veps)} VEPs got LLM attention, {len(result.alerts)} alerts", node="analyze_combined")

        except (RuntimeError, ValueError, TypeError, KeyError) as e:
            log(f"Batch {batch_num} failed: {e}. Preserving original VEPs.", node="analyze_combined", level="WARNING")
            all_updated_veps.extend(batch_veps)

    updated_veps = all_updated_veps

    # Generate fallback attention for VEPs that LLM genuinely dropped or failed.
    # This should be rare - LLM generates attention for all VEPs, and stale
    # VEPs get deterministic attention pre-filled.
    fallback_count = 0
    for vep in updated_veps:
        if not hasattr(vep, 'analysis') or not vep.analysis or not vep.analysis.get("attention"):
            if not hasattr(vep, 'analysis') or vep.analysis is None:
                vep.analysis = {}
            fallback_count += 1
            try:
                attention = _generic_fallback_attention(vep, release_phase, phase_detail, release_cutoff)
                _set_vep_attention(vep, attention)
                log(f"Generated fallback attention for {vep.name}: {vep.analysis['attention']['attention_level']}", node="analyze_combined", level="DEBUG")
            except (ValueError, TypeError, ValidationError) as e:
                log(f"Fallback attention failed for {vep.name}: {e}", node="analyze_combined", level="WARNING")

    if fallback_count > 0:
        log(f"Fallback attention generated for {fallback_count} VEP(s) (LLM did not produce attention for these)", node="analyze_combined", level="WARNING")

    # Force "ok" once nothing is left open for the current release - regardless
    # of what the LLM/fallback guessed.
    for vep in updated_veps:
        try:
            _apply_all_merged_shortcut(vep, release_phase, release_cutoff)
        except (ValueError, TypeError, ValidationError) as e:
            log(f"Landed shortcut failed for {vep.name}: {e}", node="analyze_combined", level="WARNING")

    # Determine sheets update need
    skip_monitoring = state.get("skip_monitoring", False)
    sheets_need_update = True if skip_monitoring else sheets_need_update_any

    # Count VEPs with attention assessments
    attention_count = sum(
        1 for vep in updated_veps
        if hasattr(vep, 'analysis') and vep.analysis and vep.analysis.get("attention")
    )

    # Log results
    if result.alerts:
        log(f"Analysis generated {len(result.alerts)} alert(s)", node="analyze_combined")
    if result.general_insights:
        log(f"Generated {len(result.general_insights)} general insight(s)", node="analyze_combined")
    vep_summary_table = []
    if attention_count > 0:
        log(f"Attention assessments: {attention_count} VEP(s)", node="analyze_combined")

        # Log VEPs needing attention and stale VEPs
        needs_attention_veps = []
        stale_veps = []
        for vep in updated_veps:
            if hasattr(vep, 'analysis') and vep.analysis and vep.analysis.get("attention"):
                attn = vep.analysis["attention"]
                level = attn.get("attention_level")
                staleness = attn.get("staleness") or {}
                is_stale = staleness.get("is_stale", False)
                days_inactive = staleness.get("days_since_human_activity") or 0

                if level == AttentionLevel.NEEDS_ATTENTION.value:
                    needs_attention_veps.append(f"{vep.name} ({level})")

                if is_stale:
                    stale_veps.append(f"{vep.name} ({days_inactive}d inactive)")

        if needs_attention_veps:
            log(f"VEPs needing attention: {', '.join(needs_attention_veps)}", node="analyze_combined", level="WARNING")

        if stale_veps:
            log(f"Stale VEPs (no recent human activity): {', '.join(stale_veps)}", node="analyze_combined", level="WARNING")

        # Build and log VEP summary table for visibility
        # Also stored in state so downstream nodes (send_slack, send_email) reuse it
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

                # Log attention-level summary
                red_count = sum(1 for row in vep_summary_table if row["urgency"] == "RED")
                yellow_count = sum(1 for row in vep_summary_table if row["urgency"] == "YELLOW")
                green_count = sum(1 for row in vep_summary_table if row["urgency"] == "GREEN")
                log(f"Summary: {red_count} NEEDS ATTENTION (RED), {yellow_count} WATCH (YELLOW), {green_count} OK (GREEN)", node="analyze_combined")
        except (RuntimeError, ValueError, TypeError, KeyError) as e:
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
