"""Activity context fetch node - fetches activity-related data for VEPs.

This is a FETCH node - it only gathers raw data, NO analysis.
Analysis is done by analyze_combined which has access to ALL context at once.

WS3: deterministic — no LLM round-trip. Review/comment/dismissal data is
already grounded upstream by the indexer (services.indexer.derive_pr_conversation_signals)
and attached to each PR as PRInfo.conversation. This node simply aggregates
that already-fetched data per VEP; it does not call GitHub itself.
"""

from datetime import UTC, datetime
from typing import Any

from services.utils import log
from state import PRInfo, VEPState


def _aggregate_vep_activity(vep, now: datetime) -> dict[str, Any]:
    """Deterministically aggregate grounded conversation signals for one VEP.

    Combines the tracking issue's updated_at with the conversation data
    already attached to enhancement/implementation PRs (PRInfo.conversation)
    to produce the same shape check_activity historically produced via an
    LLM fetch, but grounded in real review states/comments.
    """
    all_prs: list[PRInfo] = list(vep.enhancement_prs) + list(vep.implementation_prs)

    last_issue_update = None
    if vep.tracking_issue and vep.tracking_issue.updated_at:
        last_issue_update = vep.tracking_issue.updated_at
    elif vep.last_updated:
        last_issue_update = vep.last_updated

    pr_update_dates = [pr.updated_at for pr in all_prs if pr.updated_at]
    last_pr_update = max(pr_update_dates) if pr_update_dates else None

    conversations = [pr.conversation for pr in all_prs if pr.conversation]

    recent_reviews = []
    for pr in all_prs:
        for review in (pr.conversation.get("reviews") or []):
            recent_reviews.append({**review, "pr_number": pr.number})

    recent_comments = []
    candidate_dates = []
    if last_issue_update:
        candidate_dates.append(last_issue_update)
    if last_pr_update:
        candidate_dates.append(last_pr_update)

    for pr in all_prs:
        last_comment_author = pr.conversation.get("last_comment_author")
        last_comment_date = pr.conversation.get("last_comment_date")
        if last_comment_author and last_comment_date:
            recent_comments.append({
                "author": last_comment_author,
                "date": last_comment_date,
                "pr_number": pr.number,
            })
            try:
                candidate_dates.append(datetime.fromisoformat(last_comment_date))
            except (ValueError, TypeError):
                pass

    last_comment_date_overall = max(candidate_dates).isoformat() if candidate_dates else None
    days_since_update = (now - max(candidate_dates)).days if candidate_dates else None

    changes_requested_unaddressed = any(
        c.get("changes_requested_unaddressed") for c in conversations
    )
    unaddressed_by = []
    for c in conversations:
        unaddressed_by.extend(c.get("unaddressed_by") or [])

    approved_count = sum(c.get("approved_count", 0) for c in conversations)
    changes_requested_count = sum(c.get("changes_requested_count", 0) for c in conversations)

    pr_conversations = {pr.number: pr.conversation for pr in all_prs if pr.conversation}

    return {
        "last_issue_update": last_issue_update.isoformat() if last_issue_update else None,
        "last_pr_update": last_pr_update.isoformat() if last_pr_update else None,
        "last_comment_date": last_comment_date_overall,
        "days_since_update": days_since_update,
        "recent_reviews": recent_reviews,
        "recent_comments": recent_comments,
        "approved_count": approved_count,
        "changes_requested_count": changes_requested_count,
        "changes_requested_unaddressed": changes_requested_unaddressed,
        "unaddressed_by": unaddressed_by,
        "pr_conversations": pr_conversations,
    }


def check_activity_node(state: VEPState) -> Any:
    """Fetch activity-related context for VEPs.

    Deterministic — no LLM call. Aggregates the conversation data already
    grounded by the indexer (PRInfo.conversation on each enhancement/
    implementation PR) into vep.context.activity.
    """
    veps = state.get("veps", [])
    veps_count = len(veps)
    log(f"Computing activity context for {veps_count} VEP(s)", node="check_activity")

    last_check_times = state.get("last_check_times", {})
    last_check_times["check_activity"] = datetime.now(UTC)

    if not veps:
        return {
            "last_check_times": last_check_times,
        }

    now = datetime.now(UTC)
    context_by_id = {
        vep.tracking_issue_id: _aggregate_vep_activity(vep, now)
        for vep in veps
    }

    # Store in vep_updates_by_check for merge node
    vep_updates_by_check = state.get("vep_updates_by_check", {})
    vep_updates_by_check["check_activity"] = {"context_field": "activity", "updates": context_by_id}

    log(f"Computed activity context for {len(context_by_id)} VEP(s)", node="check_activity")

    return {
        "last_check_times": last_check_times,
        "vep_updates_by_check": vep_updates_by_check,
    }
