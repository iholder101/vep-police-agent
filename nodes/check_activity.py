"""Activity context fetch node - fetches activity-related data for VEPs.

This is a FETCH node - it only gathers raw data, NO analysis.
Analysis is done by analyze_combined which has access to ALL context at once.
"""

import json
from datetime import UTC, datetime
from typing import Any

from services.llm_helper import invoke_llm_fetch
from services.response_models import FetchResponse
from services.utils import log
from state import VEPState


def check_activity_node(state: VEPState) -> Any:
    """Fetch activity-related context for VEPs.

    This is a FETCH node using lightweight LLM (Flash). It:
    1. Fetches recent activity from tracking issues and PRs
    2. Computes days since last update
    3. Stores raw activity data in vep.context.activity

    NO analysis is done here - that's handled by analyze_combined.
    """
    veps = state.get("veps", [])
    veps_count = len(veps)
    log(f"Fetching activity context for {veps_count} VEP(s)", node="check_activity")

    last_check_times = state.get("last_check_times", {})
    last_check_times["check_activity"] = datetime.now(UTC)

    if not veps:
        return {
            "last_check_times": last_check_times,
        }

    # Get approved_vep_prs from config cache if available
    config_cache = state.get("config_cache", {})
    approved_vep_prs = config_cache.get("approved_vep_prs", [])

    # Build system prompt - FETCH ONLY, no analysis
    system_prompt = """You are a lightweight data fetcher for VEP activity information.

Your task is to FETCH raw data only - do NOT analyze or generate alerts.

FETCH TASKS for each VEP:
1. Get activity timestamps:
   - last_issue_update: datetime (from tracking issue)
   - last_pr_update: datetime (from any related PR)
   - last_comment_date: datetime (most recent comment on issue or PR)
   - days_since_update: int (computed from most recent activity)

2. Get recent events (last 5-10):
   - recent_comments: list of {author, date, type} (issue/pr comments)
   - recent_commits: list of {author, date, message} (on related PRs)
   - recent_reviews: list of {author, date, state} (PR reviews)

3. Check for stale approved-vep PRs:
   - For PRs with 'approved-vep' label, get last_updated date
   - approved_vep_pr_stale: bool (not updated in >3 days)

IMPORTANT: Do NOT analyze staleness, do NOT judge activity levels, do NOT make recommendations.
Just fetch the raw data. Activity analysis is done by a separate node.

Return context_updates with the raw activity data for each VEP."""

    # Serialize minimal state for LLM
    context = {
        "veps": [{"tracking_issue_id": vep.tracking_issue_id, "name": vep.name,
                  "last_updated": vep.last_updated.isoformat() if vep.last_updated else None,
                  "tracking_issue": {"number": vep.tracking_issue.number, "updated_at": vep.tracking_issue.updated_at.isoformat()} if vep.tracking_issue else None,
                  "enhancement_prs": [{"number": pr.number, "updated_at": pr.updated_at.isoformat()} for pr in vep.enhancement_prs]} for vep in veps],
        "approved_vep_prs": approved_vep_prs,
        "today": datetime.now(UTC).isoformat(),
    }

    user_prompt = f"""Fetch activity context for these VEPs:

{json.dumps(context, indent=2, default=str)}

For each VEP, use GitHub MCP tools to fetch recent activity and return context_updates with:
- last_issue_update, last_pr_update, last_comment_date, days_since_update
- recent_comments, recent_commits, recent_reviews
- approved_vep_pr_stale (if applicable)"""

    # Invoke lightweight LLM to fetch data
    result = invoke_llm_fetch("check_activity", context, system_prompt, user_prompt, FetchResponse)

    # Store context updates for merge node to apply
    context_by_id = {cu.tracking_issue_id: cu.context_data for cu in result.context_updates}

    # Store in vep_updates_by_check for merge node
    vep_updates_by_check = state.get("vep_updates_by_check", {})
    vep_updates_by_check["check_activity"] = {"context_field": "activity", "updates": context_by_id}

    log(f"Fetched activity context for {len(context_by_id)} VEP(s)", node="check_activity")

    return {
        "last_check_times": last_check_times,
        "vep_updates_by_check": vep_updates_by_check,
    }
