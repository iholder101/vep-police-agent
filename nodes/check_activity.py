"""Activity monitoring node - checks for inactive VEPs and review lag."""

import json
from datetime import datetime
from typing import Any
from services.response_models import CheckResponse
from state import VEPState
from services.utils import log
from services.llm_helper import invoke_llm_check


class ActivityCheckResponse(CheckResponse):
    """Response model for activity check."""
    pass


def check_activity_node(state: VEPState) -> Any:
    """Monitor VEP activity and flag inactive VEPs.
    
    Uses LLM with GitHub MCP tools to:
    1. Fetch issue/PR updates from GitHub
    2. Calculate activity metrics (last activity, days since update, review lag)
    3. Flag inactive VEPs (>2 weeks without updates)
    4. Monitor review lag times (>1 week without review)
    """
    veps = state.get("veps", [])
    veps_count = len(veps)
    log(f"Checking activity for {veps_count} VEP(s) using LLM", node="check_activity")
    
    last_check_times = state.get("last_check_times", {})
    last_check_times["check_activity"] = datetime.now()
    
    if not veps:
        return {
            "last_check_times": last_check_times,
            "alerts": [],
        }
    
    # Build system prompt
    system_prompt = """You are a VEP governance agent monitoring activity for KubeVirt Virtualization Enhancement Proposals.

Your task:
1. For each VEP in the provided state, use GitHub MCP tools to fetch the tracking issue and related PRs
2. Calculate activity metrics and update vep.activity:
   - last_activity: datetime (from issues/PRs)
   - days_since_update: int
   - review_lag_days: Optional[int] (days since last review)
3. Add insights to vep.analysis["activity_insights"] with notes, recommendations, and context
4. Flag inactive VEPs (>2 weeks without updates)
5. Flag review lag (>1 week without review)

CRITICAL - GitHub Search Query Requirements:
- When using search_issues tool, ALL queries MUST include either "is:issue" or "is:pull-request"
- Correct examples:
  * "org:kubevirt \"VEP 160\" is:issue" (to search for issues)
  * "org:kubevirt \"VEP 160\" is:pull-request" (to search for PRs)
  * "repo:kubevirt/enhancements \"VEP 160\" is:issue"
- Incorrect (will fail): "org:kubevirt \"VEP 160\"" (missing is:issue or is:pull-request)
- If you need both issues and PRs, make two separate queries

Return the updated VEP objects with activity fields filled in."""
    
    # Serialize full state for LLM
    release_schedule = state.get("release_schedule")
    context = {
        "veps": [vep.model_dump(mode='json') for vep in veps],
        "release_schedule": release_schedule.model_dump(mode='json') if release_schedule else None,
        "current_release": state.get("current_release"),
    }
    
    user_prompt = f"""Here is the current state:

{json.dumps(context, indent=2, default=str)}

Use GitHub MCP tools to check activity for each VEP. Update the VEP objects with activity information and return all updated VEPs."""
    
    # Invoke LLM with structured output
    result = invoke_llm_check("activity", context, system_prompt, user_prompt, ActivityCheckResponse)
    
    # Replace VEPs with updated ones from LLM
    alerts = state.get("alerts", [])
    alerts.extend(result.alerts)
    
    # Store updates in vep_updates_by_check for the merge node to combine
    vep_updates_by_check = state.get("vep_updates_by_check", {})
    vep_updates_by_check["check_activity"] = result.updated_veps
    
    if alerts:
        log(f"Generated {len(alerts)} activity alert(s)", node="check_activity")
    
    return {
        "last_check_times": last_check_times,
        "alerts": alerts,
        "vep_updates_by_check": vep_updates_by_check,  # Store updates for merge node
    }
