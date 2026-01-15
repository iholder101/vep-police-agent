"""Analysis node - reasons about combined check results and generates alerts."""

import json
from datetime import datetime
from typing import Any, Optional
from state import VEPState
from services.utils import log
from services.llm_helper import invoke_llm_check
from services.response_models import CheckResponse


class AnalyzeCombinedResponse(CheckResponse):
    """Response model for combined analysis."""
    sheets_need_update: bool = False  # Whether Google Sheets needs to be synced with these changes
    general_insights: Optional[str] = None  # General insights and patterns across all VEPs (overall release health, trends, cross-VEP patterns)


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
            "general_insights": None,
            "sheets_need_update": False,
        }
    
    # Check if mock mode is enabled - skip LLM and do naive analysis
    mock_mode = state.get("mock_analyzed_combined", False)
    if mock_mode:
        log("Mock analyzed-combined mode: Skipping LLM call, using naive analysis", node="analyze_combined")
        
        # Naive analysis: just preserve all VEPs, add basic combined insights, set sheets_need_update
        alerts = state.get("alerts", [])
        updated_veps = []
        
        for vep in veps:
            # Add basic combined insights if not present
            if not hasattr(vep, 'analysis') or vep.analysis is None:
                vep.analysis = {}
            
            if "combined_insights" not in vep.analysis:
                vep.analysis["combined_insights"] = "Mock analysis: All checks completed. Status reviewed."
            
            updated_veps.append(vep)
        
        # Always set sheets_need_update in mock mode if skip_monitoring is enabled
        skip_monitoring = state.get("skip_monitoring", False)
        sheets_need_update = True if skip_monitoring else False
        
        log(f"Mock analysis complete: {len(updated_veps)} VEP(s), {len(alerts)} alert(s), sheets_need_update={sheets_need_update}", node="analyze_combined")
        
        return {
            "last_check_times": last_check_times,
            "veps": updated_veps,
            "alerts": alerts,
            "general_insights": "Mock analysis: All checks completed. Status reviewed.",
            "sheets_need_update": sheets_need_update,
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
4. Generate general_insights (free-form text) covering:
   - Overall release health assessment (e.g., "5 of 20 VEPs are at risk this release cycle")
   - Cross-VEP patterns and trends (e.g., "Most VEPs are behind schedule", "Compliance issues are concentrated in network SIG")
   - Release-wide recommendations (e.g., "Consider extending Enhancement Freeze deadline", "Focus SIG review efforts on network VEPs")
   - High-level observations that don't fit into individual VEP analysis
5. Generate additional alerts based on combined reasoning
6. Determine if Google Sheets needs to be updated:
   - Set sheets_need_update to True if there are meaningful changes that should be reflected in the sheets
   - Consider: significant status changes, new alerts, compliance changes, deadline updates
   - Set to False if changes are minor or only internal analysis updates

Return the updated VEP objects with merged analysis, general insights, and your decision on whether sheets need updating."""
    
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
    
    # CRITICAL: Ensure all VEPs are preserved - LLM might drop some during analysis
    if len(updated_veps) < len(veps):
        log(f"Warning: LLM returned {len(updated_veps)} VEP(s), expected {len(veps)}. Preserving all VEPs.", node="analyze_combined", level="WARNING")
        # Fallback: keep existing VEPs that weren't in the analysis result
        existing_names = {vep.name for vep in updated_veps}
        for vep in veps:
            if vep.name not in existing_names:
                log(f"Preserving VEP {vep.name} that was dropped during analysis", node="analyze_combined", level="DEBUG")
                updated_veps.append(vep)
    
    # Also check if we have MORE VEPs than expected (shouldn't happen, but log it)
    if len(updated_veps) > len(veps):
        log(f"Info: LLM returned {len(updated_veps)} VEP(s), expected {len(veps)}. Using all returned VEPs.", node="analyze_combined", level="INFO")
    
    # Use LLM's decision on whether sheets need updating
    # But if skip_monitoring is enabled, always set sheets_need_update to trigger alert_summary
    skip_monitoring = state.get("skip_monitoring", False)
    if skip_monitoring:
        sheets_need_update = True  # Always trigger alert_summary when skip_monitoring is enabled
        log("Skip-monitoring mode: Setting sheets_need_update=True to ensure alert_summary runs", node="analyze_combined")
    else:
        sheets_need_update = result.sheets_need_update
    
    if alerts:
        log(f"Generated {len(result.alerts)} additional alert(s) from combined analysis", node="analyze_combined")
    
    if result.general_insights:
        log(f"General insights generated: {len(result.general_insights)} characters", node="analyze_combined", level="DEBUG")
        # Log first 200 chars as preview
        preview = result.general_insights[:200] + ("..." if len(result.general_insights) > 200 else "")
        log(f"General insights preview: {preview}", node="analyze_combined", level="DEBUG")
    
    log(f"Sheets update needed: {sheets_need_update} (decided by LLM{' or skip_monitoring mode' if skip_monitoring else ''})", node="analyze_combined")
    
    return {
        "last_check_times": last_check_times,
        "veps": updated_veps,  # Return updated VEPs explicitly
        "alerts": alerts,
        "general_insights": result.general_insights,
        "sheets_need_update": sheets_need_update,
    }
