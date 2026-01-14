"""Compliance checking node - verifies VEP compliance with process."""

import json
from datetime import datetime
from typing import Any
from services.response_models import CheckResponse
from state import VEPState
from services.utils import log
from services.llm_helper import invoke_llm_check


class ComplianceCheckResponse(CheckResponse):
    """Response model for compliance check."""
    pass


def check_compliance_node(state: VEPState) -> Any:
    """Check VEP compliance with process requirements.
    
    Uses LLM with GitHub MCP tools to check compliance and return updated VEP objects.
    The LLM receives the full state and returns updated VEPs with compliance fields filled.
    """
    veps = state.get("veps", [])
    veps_count = len(veps)
    log(f"Checking compliance for {veps_count} VEP(s) using LLM", node="check_compliance")
    
    last_check_times = state.get("last_check_times", {})
    last_check_times["check_compliance"] = datetime.now()
    
    if not veps:
        return {
            "last_check_times": last_check_times,
            "alerts": [],
        }
    
    # Build system prompt
    system_prompt = """You are a VEP governance agent checking compliance with the KubeVirt VEP process.

Your task:
1. For each VEP in the provided state, use GitHub MCP tools to fetch PRs, reviews, comments, and tracking issues
2. Check compliance requirements:
   - VEP template completeness (check against template in repo)
   - SIG sign-offs (all 3 SIGs: compute, network, storage)
   - VEP PR merged
   - Implementation PRs merged
   - PRs linked in tracking issue
   - Docs PR created/merged
   - Labels valid (SIG labels and target release labels)
3. Update each VEP's compliance field with the check results
4. Add insights to vep.analysis["compliance_insights"] with notes, recommendations, and context
5. Generate alerts for any compliance violations

Use the GitHub MCP tools to fetch the necessary data. Refer to each tool's description for usage requirements and examples.

Return the updated VEP objects with compliance fields filled and any insights added."""
    
    # Serialize full state for LLM
    release_schedule = state.get("release_schedule")
    context = {
        "veps": [vep.model_dump(mode='json') for vep in veps],
        "release_schedule": release_schedule.model_dump(mode='json') if release_schedule else None,
        "current_release": state.get("current_release"),
    }
    
    user_prompt = f"""Here is the current state:

{json.dumps(context, indent=2, default=str)}

Use GitHub MCP tools to check compliance for each VEP. Update the VEP objects with compliance information and return all updated VEPs."""
    
    # Invoke LLM with structured output
    result = invoke_llm_check("compliance", context, system_prompt, user_prompt, ComplianceCheckResponse)
    
    # Replace VEPs with updated ones from LLM
    alerts = state.get("alerts", [])
    alerts.extend(result.alerts)
    
    # Store updates in vep_updates_by_check for the merge node to combine
    vep_updates_by_check = state.get("vep_updates_by_check", {})
    vep_updates_by_check["check_compliance"] = result.updated_veps
    
    if alerts:
        log(f"Generated {len(result.alerts)} compliance alert(s)", node="check_compliance")
    
    return {
        "last_check_times": last_check_times,
        "alerts": alerts,
        "vep_updates_by_check": vep_updates_by_check,  # Store updates for merge node
    }
