"""Deadline monitoring node - checks VEP deadlines and generates alerts."""

import json
from datetime import datetime
from typing import Any, Optional
from state import VEPState, VEPInfo, ReleaseSchedule
from services.utils import log
from services.llm_helper import invoke_llm_check
from services.response_models import CheckResponse


class DeadlineCheckResponse(CheckResponse):
    """Response model for deadline check."""
    current_release: Optional[str] = None
    release_schedule: Optional[ReleaseSchedule] = None


def check_deadlines_node(state: VEPState) -> Any:
    """Check VEP deadlines and generate alerts for approaching deadlines.
    
    Uses LLM with GitHub MCP tools to:
    1. Fetch release schedule from kubevirt/sig-release
    2. Compute days until EF and CF for each VEP
    3. Generate alerts for approaching deadlines (7d, 3d, 1d warnings)
    4. Flag VEPs that won't make deadlines
    
    Note: Days until EF/CF are computed on-demand, not stored in state.
    """
    veps = state.get("veps", [])
    veps_count = len(veps)
    log(f"Checking deadlines for {veps_count} VEP(s) using LLM", node="check_deadlines")
    
    last_check_times = state.get("last_check_times", {})
    last_check_times["check_deadlines"] = datetime.now()
    
    if not veps:
        return {
            "last_check_times": last_check_times,
            "alerts": [],
        }
    
    # Build system prompt
    system_prompt = """You are a VEP governance agent checking deadlines for KubeVirt Virtualization Enhancement Proposals.

Your task:
1. Fetch the current release schedule from kubevirt/sig-release repository
   - Look for schedule.md files in releases/v1.X/ directories
   - Parse Enhancement Freeze (EF) and Code Freeze (CF) dates
2. For each VEP in the provided state, compute days until EF and CF
3. Update vep.analysis["deadline_risk"] with risk information:
   - at_risk: boolean
   - risk_reason: string
   - days_until_ef: int
   - days_until_cf: int
4. Add insights to vep.analysis["deadline_insights"] with notes, recommendations, and context
5. Generate alerts for approaching deadlines:
   - 7 days before: WARNING
   - 3 days before: URGENT  
   - 1 day before: CRITICAL
6. Flag VEPs at risk (e.g., EF passed but VEP not merged)

Return the updated VEP objects with deadline analysis filled in, and the release schedule if you fetched it."""
    
    # Serialize full state for LLM
    release_schedule = state.get("release_schedule")
    context = {
        "veps": [vep.model_dump(mode='json') for vep in veps],
        "release_schedule": release_schedule.model_dump(mode='json') if release_schedule else None,
        "current_release": state.get("current_release"),
    }
    
    user_prompt = f"""Here is the current state:

{json.dumps(context, indent=2, default=str)}

Use GitHub MCP tools to fetch the release schedule and check deadlines for each VEP. Update the VEP objects with deadline analysis and return all updated VEPs."""
    
    # Invoke LLM with structured output
    result = invoke_llm_check("deadlines", context, system_prompt, user_prompt, DeadlineCheckResponse)
    
    # Replace VEPs with updated ones from LLM
    alerts = state.get("alerts", [])
    alerts.extend(result.alerts)
    
    # Store updates in vep_updates_by_check for the merge node to combine
    vep_updates_by_check = state.get("vep_updates_by_check", {})
    vep_updates_by_check["check_deadlines"] = result.updated_veps
    
    # Update release schedule if LLM fetched it
    current_release = state.get("current_release")
    release_schedule = state.get("release_schedule")
    
    if result.release_schedule:
        release_schedule = result.release_schedule
    
    if result.current_release:
        current_release = result.current_release
    
    if alerts:
        log(f"Generated {len(result.alerts)} deadline alert(s)", node="check_deadlines")
    
    return {
        "last_check_times": last_check_times,
        "current_release": current_release,
        "release_schedule": release_schedule,
        "alerts": alerts,
        "vep_updates_by_check": vep_updates_by_check,  # Store updates for merge node
    }