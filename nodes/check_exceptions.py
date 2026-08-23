"""Exception context fetch node - fetches exception-related data for VEPs.

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


def check_exceptions_node(state: VEPState) -> Any:
    """Fetch exception-related context for VEPs.

    This is a FETCH node using lightweight LLM (Flash). It:
    1. Searches for exception requests in kubevirt/enhancements
    2. Checks for post-freeze work that might need exceptions
    3. Stores raw exception data in vep.context.exceptions

    NO analysis is done here - that's handled by analyze_combined.
    """
    veps = state.get("veps", [])
    veps_count = len(veps)
    log(f"Fetching exception context for {veps_count} VEP(s)", node="check_exceptions")

    last_check_times = state.get("last_check_times", {})
    last_check_times["check_exceptions"] = datetime.now(UTC)

    if not veps:
        return {
            "last_check_times": last_check_times,
        }

    # Get release schedule for freeze dates
    release_schedule = state.get("release_schedule")

    # Build system prompt - FETCH ONLY, no analysis
    system_prompt = """You are a lightweight data fetcher for VEP exception information.

Your task is to FETCH raw data only - do NOT analyze or generate alerts.

FETCH TASKS:
1. Search for exception-related issues in kubevirt/enhancements:
   - Search patterns: "exception", "exemption", "freeze extension", "post-freeze"
   - Check for "exception" label on issues
   - For each found exception issue, get: number, title, state, labels, body (first 500 chars)

2. For each VEP, check if it has an associated exception:
   - exception_issue_number: int or null
   - exception_issue_state: "open", "closed" or null
   - exception_labels: list of labels on exception issue

3. Check for post-freeze activity:
   - has_post_ef_commits: bool (commits after Enhancement Freeze)
   - has_post_cf_commits: bool (commits after Code Freeze)
   - post_freeze_pr_numbers: list of PR numbers with post-freeze activity

IMPORTANT: Do NOT analyze whether exceptions are needed, do NOT judge completeness.
Just fetch the raw data. Exception analysis is done by a separate node.

Return context_updates with the raw exception data for each VEP."""

    # Serialize minimal state for LLM
    context = {
        "veps": [{"tracking_issue_id": vep.tracking_issue_id, "name": vep.name, "title": vep.title,
                  "target_release": vep.target_release} for vep in veps],
        "release_schedule": release_schedule.model_dump(mode='json') if release_schedule else None,
        "current_release": state.get("current_release"),
    }

    user_prompt = f"""Fetch exception context for these VEPs:

{json.dumps(context, indent=2, default=str)}

First, search kubevirt/enhancements for exception-related issues.
Then, for each VEP, return context_updates with:
- exception_issue_number, exception_issue_state, exception_labels
- has_post_ef_commits, has_post_cf_commits, post_freeze_pr_numbers"""

    # Invoke lightweight LLM to fetch data
    result = invoke_llm_fetch("check_exceptions", context, system_prompt, user_prompt, FetchResponse)

    # Store context updates for merge node to apply
    context_by_id = {cu.tracking_issue_id: cu.context_data for cu in result.context_updates}

    # Store in vep_updates_by_check for merge node
    vep_updates_by_check = state.get("vep_updates_by_check", {})
    vep_updates_by_check["check_exceptions"] = {"context_field": "exceptions", "updates": context_by_id}

    log(f"Fetched exception context for {len(context_by_id)} VEP(s)", node="check_exceptions")

    return {
        "last_check_times": last_check_times,
        "vep_updates_by_check": vep_updates_by_check,
    }
