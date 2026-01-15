"""Merge VEP updates node - combines updates from parallel check nodes using LLM."""

import json
from typing import Any
from state import VEPState
from services.utils import log
from services.llm_helper import invoke_llm_check
from services.response_models import CheckResponse


class MergeVEPUpdatesResponse(CheckResponse):
    """Response model for VEP merge operation."""
    pass  # Inherits updated_veps and alerts from CheckResponse


def merge_vep_updates_node(state: VEPState) -> Any:
    """Merge VEP updates from parallel check nodes using LLM.
    
    This node runs after all check nodes complete and uses an LLM to intelligently
    merge their updates into the main veps list. The LLM can handle conflicts,
    understand field semantics, and provide merge insights.
    
    The check nodes store their updates in vep_updates_by_check, and this node
    asks the LLM to merge them all into complete VEP objects.
    """
    veps = state.get("veps", [])
    vep_updates_by_check = state.get("vep_updates_by_check", {})
    
    if not vep_updates_by_check:
        log("No VEP updates to merge", node="merge_vep_updates")
        return {}
    
    log(f"Merging VEP updates from {len(vep_updates_by_check)} check(s) using LLM", node="merge_vep_updates")
    
    # Build system prompt
    system_prompt = """You are a VEP governance agent merging updates from parallel monitoring checks.

Your task:
1. You will receive the current VEP state and updates from multiple parallel checks
2. Each check updates different aspects of VEPs:
   - check_deadlines: updates analysis["deadline_risk"], analysis["deadline_insights"], release_schedule
   - check_activity: updates activity field, analysis["activity_insights"]
   - check_compliance: updates compliance field, analysis["compliance_insights"]
   - check_exceptions: updates exceptions field, analysis["exception_insights"]
3. Merge all updates intelligently:
   - Combine updates from all checks for each VEP
   - Preserve existing fields that weren't updated
   - Deep merge nested structures (analysis, exceptions)
   - Handle any conflicts by taking the most recent/complete information
   - Ensure all VEP objects are complete and valid
4. Return the merged VEP objects with all updates combined

Return the complete merged VEP objects."""
    
    # Prepare context for LLM
    context = {
        "current_veps": [vep.model_dump(mode='json') for vep in veps],
        "updates_by_check": {
            check_name: [vep.model_dump(mode='json') for vep in updated_veps]
            for check_name, updated_veps in vep_updates_by_check.items()
        },
    }
    
    user_prompt = f"""Here is the current VEP state and updates from parallel checks:

{json.dumps(context, indent=2, default=str)}

Merge all the updates from the different checks into complete VEP objects. Each check updated different aspects, so combine them all. Return all merged VEP objects."""
    
    # Invoke LLM to perform the merge
    result = invoke_llm_check("merge_vep_updates", context, system_prompt, user_prompt, MergeVEPUpdatesResponse)
    
    # The LLM returns merged VEPs - use them directly
    merged_veps = result.updated_veps
    
    # CRITICAL: Ensure all VEPs are preserved - LLM might drop some during merge
    if len(merged_veps) < len(veps):
        log(f"Warning: LLM returned {len(merged_veps)} VEP(s), expected {len(veps)}. Preserving all VEPs.", node="merge_vep_updates", level="WARNING")
        # Fallback: keep existing VEPs that weren't in the merge result
        existing_names = {vep.name for vep in merged_veps}
        for vep in veps:
            if vep.name not in existing_names:
                log(f"Preserving VEP {vep.name} that was dropped during merge", node="merge_vep_updates", level="DEBUG")
                merged_veps.append(vep)
    
    # Also check if we have MORE VEPs than expected (shouldn't happen, but log it)
    if len(merged_veps) > len(veps):
        log(f"Info: LLM returned {len(merged_veps)} VEP(s), expected {len(veps)}. Using all returned VEPs.", node="merge_vep_updates", level="INFO")
    
    alerts = state.get("alerts", [])
    alerts.extend(result.alerts)
    
    log(f"Merged {len(merged_veps)} VEP(s) using LLM", node="merge_vep_updates")
    
    return {
        "veps": merged_veps,
        "vep_updates_by_check": {},  # Clear the updates after merging
        "alerts": alerts,  # Merge any alerts from merge process
    }
