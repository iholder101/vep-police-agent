"""Activity context node - computes activity-related data for VEPs.

Deterministic node using indexed context (no LLM). Aggregates timestamps
from issues, enhancement PRs, and implementation PRs to compute
days_since_update and approved VEP PR staleness.
"""

from datetime import datetime, timezone
from typing import Any
from state import VEPState
from services.utils import log
from services.indexer import create_indexed_context
from nodes._check_helpers import extract_vep_num, parse_iso_date


def check_activity_node(state: VEPState) -> Any:
    """Compute activity context for VEPs from indexed data.

    Aggregates timestamps from all indexed sources to determine
    days_since_update for each VEP.
    """
    veps = state.get("veps", [])
    log(f"Computing activity context for {len(veps)} VEP(s)", node="check_activity")

    last_check_times = state.get("last_check_times", {})
    last_check_times["check_activity"] = datetime.now()

    if not veps:
        return {"last_check_times": last_check_times}

    index_cache_minutes = state.get("index_cache_minutes", 60)
    indexed_context = create_indexed_context(cache_max_age_minutes=index_cache_minutes)

    issues_index = indexed_context.get("issues_index", [])
    enhancements_prs = indexed_context.get("enhancements_prs", [])
    prs_index = indexed_context.get("prs_index", [])
    vep_to_pr_mappings = indexed_context.get("vep_to_pr_mappings", {})
    approved_vep_prs = indexed_context.get("approved_vep_prs", [])

    now = datetime.now(timezone.utc)

    context_by_id = {}
    for vep in veps:
        timestamps = []

        # Issue timestamp
        issue = next(
            (i for i in issues_index if i.get("number") == vep.tracking_issue_id),
            None,
        )
        if issue and issue.get("updated_at"):
            timestamps.append(issue["updated_at"])

        # Enhancement PR timestamp
        vep_pr = next(
            (pr for pr in enhancements_prs
             if pr.get("vep_issue_number") == vep.tracking_issue_id),
            None,
        )
        if vep_pr and vep_pr.get("updated_at"):
            timestamps.append(vep_pr["updated_at"])

        # Implementation PR timestamps
        for pr_ref in vep_to_pr_mappings.get(str(vep.tracking_issue_id), []):
            pr_data = next(
                (p for p in prs_index if p.get("number") == pr_ref.get("number")),
                None,
            )
            if pr_data and pr_data.get("updated_at"):
                timestamps.append(pr_data["updated_at"])

        # Compute aggregate
        parsed = [parse_iso_date(t) for t in timestamps]
        parsed = [p for p in parsed if p is not None]
        most_recent = max(parsed) if parsed else None
        days_since_update = (now - most_recent).days if most_recent else 999

        # Approved VEP PR staleness
        vep_num = extract_vep_num(vep.name)
        approved_vep_pr_stale = False
        for apr in approved_vep_prs:
            # matched_vep_numbers may contain leading zeros (e.g., "0176")
            matched = [int(x) for x in apr.get("matched_vep_numbers", []) if x.isdigit()]
            if vep_num is not None and vep_num in matched:
                approved_vep_pr_stale = (
                    apr.get("is_open", False)
                    and (apr.get("days_since_update") or 0) > 3
                )
                break

        context_by_id[vep.tracking_issue_id] = {
            "last_issue_update": issue.get("updated_at") if issue else None,
            "last_pr_update": vep_pr.get("updated_at") if vep_pr else None,
            "last_comment_date": most_recent.isoformat() if most_recent else None,
            "days_since_update": days_since_update,
            "recent_comments": [],
            "recent_commits": [],
            "recent_reviews": [],
            "approved_vep_pr_stale": approved_vep_pr_stale,
        }

    vep_updates_by_check = state.get("vep_updates_by_check", {})
    vep_updates_by_check["check_activity"] = {
        "context_field": "activity",
        "updates": context_by_id,
    }

    log(f"Computed activity context for {len(context_by_id)} VEP(s)", node="check_activity")

    return {
        "last_check_times": last_check_times,
        "vep_updates_by_check": vep_updates_by_check,
    }
