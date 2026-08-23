"""Deadline context fetch node - fetches deadline-related data for VEPs.

This is a FETCH node - it only gathers raw data, NO analysis.
Analysis is done by analyze_combined which has access to ALL context at once.
"""

import json
from datetime import UTC, datetime
from typing import Any

from services.llm_helper import invoke_llm_fetch
from services.response_models import FetchResponse
from services.utils import log
from state import ReleaseSchedule, VEPState


class DeadlineFetchResponse(FetchResponse):
    """Response model for deadline fetch."""
    current_release: str | None = None
    release_schedule: ReleaseSchedule | None = None


def check_deadlines_node(state: VEPState) -> Any:
    """Fetch deadline-related context for VEPs.

    This is a FETCH node using lightweight LLM (Flash). It:
    1. Fetches release schedule from kubevirt/sig-release (if not cached)
    2. Computes days until EF and CF for each VEP
    3. Stores raw deadline data in vep.context.deadline

    NO analysis is done here - that's handled by analyze_combined.
    """
    veps = state.get("veps", [])
    veps_count = len(veps)
    log(f"Fetching deadline context for {veps_count} VEP(s)", node="check_deadlines")

    last_check_times = state.get("last_check_times", {})
    last_check_times["check_deadlines"] = datetime.now(UTC)

    if not veps:
        return {
            "last_check_times": last_check_times,
        }

    # Build system prompt - FETCH ONLY, no analysis
    system_prompt = """You are a lightweight data fetcher for VEP deadline information.

Your task is to FETCH raw data only - do NOT analyze or generate alerts.

FETCH TASKS:
1. If release_schedule is null/missing, fetch it from kubevirt/sig-release repo
   - Look for schedule files with Enhancement Freeze (EF) and Code Freeze (CF) dates
2. For each VEP, compute and return:
   - days_until_ef: int (negative if passed)
   - days_until_cf: int (negative if passed)
   - ef_passed: bool
   - cf_passed: bool
   - vep_merged: bool (is the VEP PR merged?)
   - target_release: str (which release this VEP targets)

IMPORTANT: Do NOT analyze risk, do NOT generate insights, do NOT make recommendations.
Just fetch and compute the raw data. Analysis is done by a separate node.

Return context_updates with the raw deadline data for each VEP."""

    # Serialize minimal state for LLM
    release_schedule = state.get("release_schedule")
    context = {
        "veps": [{"tracking_issue_id": vep.tracking_issue_id, "name": vep.name, "title": vep.title,
                  "target_release": vep.target_release, "compliance": vep.compliance.model_dump()} for vep in veps],
        "release_schedule": release_schedule.model_dump(mode='json') if release_schedule else None,
        "current_release": state.get("current_release"),
        "today": datetime.now(UTC).strftime("%Y-%m-%d"),
    }

    user_prompt = f"""Fetch deadline context for these VEPs:

{json.dumps(context, indent=2, default=str)}

For each VEP, return a context_update with tracking_issue_id and context_data containing:
- days_until_ef, days_until_cf, ef_passed, cf_passed, vep_merged, target_release

If release_schedule is missing, fetch it first using GitHub MCP tools."""

    # Invoke lightweight LLM to fetch data
    result = invoke_llm_fetch("check_deadlines", context, system_prompt, user_prompt, DeadlineFetchResponse)

    # Store context updates for merge node to apply
    # We store the raw context data keyed by VEP ID - merge node will apply to veps
    context_by_id = {cu.tracking_issue_id: cu.context_data for cu in result.context_updates}

    # Store in vep_updates_by_check for merge node
    vep_updates_by_check = state.get("vep_updates_by_check", {})
    vep_updates_by_check["check_deadlines"] = {"context_field": "deadline", "updates": context_by_id}

    # Update release schedule if LLM fetched it
    current_release = state.get("current_release")
    release_schedule_out = state.get("release_schedule")

    if result.release_schedule:
        release_schedule_out = result.release_schedule
        log(f"Fetched release schedule for {result.release_schedule.version}", node="check_deadlines")

    if result.current_release:
        current_release = result.current_release

    log(f"Fetched deadline context for {len(context_by_id)} VEP(s)", node="check_deadlines")

    return {
        "last_check_times": last_check_times,
        "current_release": current_release,
        "release_schedule": release_schedule_out,
        "vep_updates_by_check": vep_updates_by_check,
    }
