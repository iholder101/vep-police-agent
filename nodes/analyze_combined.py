"""Analysis node - reasons about combined check results and generates alerts."""

import json
from datetime import datetime
from typing import Any
from state import VEPState
from services.utils import log
from services.llm_helper import invoke_llm_check
from services.response_models import CheckResponse


class AnalyzeCombinedResponse(CheckResponse):
    """Response model for combined analysis."""
    sheets_need_update: bool = False  # Whether Google Sheets needs to be synced with these changes


def analyze_combined_node(state: VEPState) -> Any:
    """Analyze combined results from all monitoring checks.
    
    This node:
    1. Reads results from all check nodes (deadlines, activity, compliance, exceptions)
    2. Uses LLM to reason about combinations (e.g., "low activity + close deadline = urgent")
    3. Merges insights from all checks into unified analysis
    4. Generates alerts based on combined context
    5. Updates VEP analysis fields with holistic insights
    
    Examples of cross-check reasoning:
    - Low activity + far deadline = OK (not urgent)
    - Low activity + close deadline = URGENT (needs attention)
    - Compliance issues + close deadline = CRITICAL
    - Multiple compliance flags failing = needs immediate review
    """
    veps = state.get("veps", [])
    log(f"Analyzing combined results for {len(veps)} VEP(s)", node="analyze_combined")
    
    last_check_times = state.get("last_check_times", {})
    last_check_times["analyze_combined"] = datetime.now()
    
    if not veps:
        return {
            "last_check_times": last_check_times,
            "alerts": [],
            "sheets_need_update": False,
        }
    
    # Build system prompt
    system_prompt = """You are a VEP governance agent performing holistic analysis of VEP status.

Your task:
1. Analyze each VEP's combined status from all monitoring checks:
   - Deadline proximity (from deadline_risk in analysis)
   - Activity levels (from activity field)
   - Compliance status (from compliance field)
   - Exception status (from exceptions field)
   - Insights from all checks (deadline_insights, activity_insights, compliance_insights, exception_insights)
2. Reason about combinations:
   - Low activity + far deadline = OK (not urgent)
   - Low activity + close deadline = URGENT (needs attention)
   - Compliance issues + close deadline = CRITICAL
   - Multiple compliance flags failing = needs immediate review
3. Merge insights from all checks into vep.analysis["combined_insights"]:
   - Overall status assessment
   - Priority level
   - Recommended actions
   - Cross-check patterns identified
4. Generate additional alerts based on combined reasoning
5. Determine if Google Sheets needs to be updated:
   - Set sheets_need_update to True if there are meaningful changes that should be reflected in the sheets
   - Consider: significant status changes, new alerts, compliance changes, deadline updates
   - Set to False if changes are minor or only internal analysis updates

Return the updated VEP objects with merged analysis and your decision on whether sheets need updating."""
    
    # Serialize full state for LLM
    release_schedule = state.get("release_schedule")
    context = {
        "veps": [vep.model_dump(mode='json') for vep in veps],
        "release_schedule": release_schedule.model_dump(mode='json') if release_schedule else None,
        "current_release": state.get("current_release"),
        "alerts": state.get("alerts", []),  # Include existing alerts for context
    }
    
    user_prompt = f"""Here is the current state with all check results:

{json.dumps(context, indent=2, default=str)}

Analyze the combined results from all monitoring checks. Merge insights and generate holistic recommendations. Return all updated VEPs with combined analysis."""
    
    # Invoke LLM with structured output
    result = invoke_llm_check("analyze_combined", context, system_prompt, user_prompt, AnalyzeCombinedResponse)
    
    # Replace VEPs with updated ones from LLM
    alerts = state.get("alerts", [])
    alerts.extend(result.alerts)
    
    # Use the updated VEPs from LLM directly
    updated_veps = result.updated_veps
    
    # Use LLM's decision on whether sheets need updating
    sheets_need_update = result.sheets_need_update
    
    if alerts:
        log(f"Generated {len(result.alerts)} additional alert(s) from combined analysis", node="analyze_combined")
    
    log(f"Sheets update needed: {sheets_need_update} (decided by LLM)", node="analyze_combined")
    
    return {
        "last_check_times": last_check_times,
        "veps": updated_veps,  # Return updated VEPs explicitly
        "alerts": alerts,
        "sheets_need_update": sheets_need_update,
    }
