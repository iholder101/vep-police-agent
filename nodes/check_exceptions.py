"""Exception tracking node - monitors exception requests."""

import json
from datetime import datetime
from typing import Any
from services.response_models import CheckResponse
from state import VEPState
from services.utils import log
from services.llm_helper import invoke_llm_check


class ExceptionCheckResponse(CheckResponse):
    """Response model for exception check."""
    pass


def check_exceptions_node(state: VEPState) -> Any:
    """Track exception requests and post-freeze work.
    
    This node:
    1. Fetches exception requests from issues (using GitHub MCP)
    2. Monitors for post-freeze work without exceptions
    3. Tracks exception requests (from mailing list or issues)
    4. Verifies exception completeness (justification, time period, impact)
    5. Updates VEP exception fields in state
    
    Note: This node fetches its own data from GitHub MCP - it's self-contained.
    """
    veps = state.get("veps", [])
    veps_count = len(veps)
    log(f"Checking exceptions for {veps_count} VEP(s) using LLM", node="check_exceptions")
    
    last_check_times = state.get("last_check_times", {})
    last_check_times["check_exceptions"] = datetime.now()
    
    if not veps:
        return {
            "last_check_times": last_check_times,
            "alerts": [],
        }
    
    # Get release schedule context
    release_schedule = state.get("release_schedule")
    current_release = state.get("current_release")
    
    # Build system prompt
    system_prompt = """You are a VEP governance agent monitoring exception requests for KubeVirt VEPs.

Your task:
1. Search for exception-related issues in kubevirt/enhancements using GitHub MCP tools:
   - Search patterns: "exception", "exemption", "freeze extension", "post-freeze", "late addition"
   - Check for "exception" label
   - Link exceptions to VEPs by matching VEP numbers in exception issue title/body
2. For each VEP, check if work is happening after freeze dates:
   - Enhancement Freeze (EF): VEP PRs created/updated after EF need exception
   - Code Freeze (CF): Implementation PRs created/updated after CF need exception
3. Update vep.exceptions with:
   - needs_exception: boolean
   - has_exception: boolean
   - exception_complete: boolean (must have: justification, time period, impact)
   - exception_reason: string
   - exception_issue: dict or null (link to the exception issue if found)
4. Add insights to vep.analysis["exception_insights"]
5. Generate alerts for missing or incomplete exceptions

Return updated VEP objects with exception fields filled."""
    
    # Serialize full state for LLM
    context = {
        "veps": [vep.model_dump(mode='json') for vep in veps],
        "release_schedule": release_schedule.model_dump(mode='json') if release_schedule else None,
        "current_release": current_release,
    }
    
    user_prompt = f"""Here is the current state:

{json.dumps(context, indent=2, default=str)}

Use GitHub MCP tools to check exceptions for each VEP. Update the VEP objects with exception information and return all updated VEPs."""
    
    # Invoke LLM with structured output
    result = invoke_llm_check("exceptions", context, system_prompt, user_prompt, ExceptionCheckResponse)
    
    # Replace VEPs with updated ones from LLM
    alerts = state.get("alerts", [])
    alerts.extend(result.alerts)
    
    # Store updates in vep_updates_by_check for the merge node to combine
    vep_updates_by_check = state.get("vep_updates_by_check", {})
    vep_updates_by_check["check_exceptions"] = result.updated_veps
    
    if alerts:
        log(f"Generated {len(alerts)} exception alert(s)", node="check_exceptions")
    
    return {
        "last_check_times": last_check_times,
        "alerts": alerts,
        "vep_updates_by_check": vep_updates_by_check,  # Store updates for merge node
    }
