"""Compliance context fetch node - fetches compliance-related data for VEPs.

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


def check_compliance_node(state: VEPState) -> Any:
    """Fetch compliance-related context for VEPs.

    This is a FETCH node using lightweight LLM (Flash). It:
    1. Fetches PR status, reviews, and labels from GitHub
    2. Checks VEP template completeness indicators
    3. Stores raw compliance data in vep.context.compliance

    NO analysis is done here - that's handled by analyze_combined.
    """
    veps = state.get("veps", [])
    veps_count = len(veps)
    log(f"Fetching compliance context for {veps_count} VEP(s)", node="check_compliance")

    last_check_times = state.get("last_check_times", {})
    last_check_times["check_compliance"] = datetime.now(UTC)

    if not veps:
        return {
            "last_check_times": last_check_times,
        }

    # Build system prompt - FETCH ONLY, no analysis
    system_prompt = """You are a lightweight data fetcher for VEP compliance information.

Your task is to FETCH raw data only - do NOT analyze or generate alerts.

FETCH TASKS for each VEP:
1. Check VEP PR status in kubevirt/enhancements:
   - pr_state: "open", "merged", "closed"
   - pr_number: int
   - pr_url: str
   - has_lgtm: bool (has LGTM comment or approval)
   - has_approved_label: bool
   - reviewers: list of usernames who reviewed

2. Check tracking issue labels:
   - sig_labels: list of SIG labels (sig/compute, sig/network, sig/storage)
   - release_labels: list of release labels (v1.8, etc.)
   - other_labels: list of other relevant labels

3. Check for linked PRs:
   - implementation_prs: list of {number, state, repo} for implementation PRs
   - docs_pr: {number, state} if docs PR exists, null otherwise

4. Template completeness indicators (from PR or issue content):
   - has_motivation_section: bool
   - has_design_section: bool
   - has_api_section: bool
   - has_test_plan: bool

IMPORTANT: Do NOT analyze, do NOT judge compliance, do NOT make recommendations.
Just fetch the raw data. Compliance analysis is done by a separate node.

Return context_updates with the raw compliance data for each VEP."""

    # Serialize minimal state for LLM
    context = {
        "veps": [{"tracking_issue_id": vep.tracking_issue_id, "name": vep.name, "title": vep.title,
                  "tracking_issue": vep.tracking_issue.model_dump() if vep.tracking_issue else None,
                  "enhancement_prs": [pr.model_dump() for pr in vep.enhancement_prs]} for vep in veps],
    }

    user_prompt = f"""Fetch compliance context for these VEPs:

{json.dumps(context, indent=2, default=str)}

For each VEP, use GitHub MCP tools to fetch PR/issue details and return context_updates with:
- pr_state, pr_number, has_lgtm, has_approved_label, reviewers
- sig_labels, release_labels, other_labels
- implementation_prs, docs_pr
- has_motivation_section, has_design_section, has_api_section, has_test_plan"""

    # Invoke lightweight LLM to fetch data
    result = invoke_llm_fetch("check_compliance", context, system_prompt, user_prompt, FetchResponse)

    # Store context updates for merge node to apply
    context_by_id = {cu.tracking_issue_id: cu.context_data for cu in result.context_updates}

    # Store in vep_updates_by_check for merge node
    vep_updates_by_check = state.get("vep_updates_by_check", {})
    vep_updates_by_check["check_compliance"] = {"context_field": "compliance", "updates": context_by_id}

    log(f"Fetched compliance context for {len(context_by_id)} VEP(s)", node="check_compliance")

    return {
        "last_check_times": last_check_times,
        "vep_updates_by_check": vep_updates_by_check,
    }
