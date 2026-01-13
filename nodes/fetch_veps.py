"""VEP discovery node - fetches VEPs from kubevirt/enhancements repository."""

import json
from datetime import datetime
from typing import Any
from state import VEPState, VEPInfo
from services.utils import log
from services.llm_helper import invoke_llm_check
from services.response_models import CheckResponse


class FetchVEPsResponse(CheckResponse):
    """Response model for VEP discovery."""
    pass


def fetch_veps_node(state: VEPState) -> Any:
    """Discover VEPs from kubevirt/enhancements repository.
    
    Uses LLM with GitHub MCP tools to:
    1. Search for VEP tracking issues in kubevirt/enhancements
    2. Read VEP documents from veps/ directory
    3. Create initial VEPInfo objects with basic metadata
    4. Return discovered VEPs
    
    Runs periodically (every 6 hours) and also when VEPs list is empty.
    """
    existing_veps = state.get("veps", [])
    existing_count = len(existing_veps)
    log(f"Fetching VEPs from GitHub | Current VEPs: {existing_count}", node="fetch_veps")
    
    last_check_times = state.get("last_check_times", {})
    last_check_times["fetch_veps"] = datetime.now()
    
    # Remove this task from queue
    next_tasks = state.get("next_tasks", [])
    if next_tasks and next_tasks[0] == "fetch_veps":
        next_tasks = next_tasks[1:]
    
    # Build system prompt
    system_prompt = """You are a VEP governance agent discovering Virtualization Enhancement Proposals from the KubeVirt enhancements repository.

Your task:
1. Search for VEP tracking issues in the kubevirt/enhancements repository
   - Look for issues that track VEPs (may have specific labels or patterns)
   - Check the repository structure and documentation to understand how VEPs are tracked
2. Read VEP documents from the veps/ directory in kubevirt/enhancements
   - VEP documents are typically markdown files in veps/ directory
   - Each VEP has a number (e.g., vep-1234) and metadata
3. For each discovered VEP, create a VEPInfo object with:
   - tracking_issue_id: The GitHub issue number that tracks this VEP
   - name: VEP identifier (e.g., "vep-1234")
   - title: VEP title from the document or issue
   - owner: GitHub username of VEP owner (from issue or document)
   - owning_sig: Primary SIG ("compute", "network", or "storage")
   - status: Current status from tracking issue
   - last_updated: Last update timestamp from issue or document
   - created_at: Creation timestamp
   - current_milestone: Initial milestone data (can be minimal, monitoring checks will fill in)
   - compliance: Initial compliance data (can be minimal, monitoring checks will fill in)
   - activity: Initial activity data (can be minimal, monitoring checks will fill in)
   - target_release: Target release version if specified
4. Return all discovered VEPs as a list of VEPInfo objects

Use GitHub MCP tools to:
- Search/list issues in kubevirt/enhancements repository
- Read files from veps/ directory
- Get issue details and metadata
- Parse VEP documents to extract metadata

Return the discovered VEPs. If you find existing VEPs in the state, you may want to merge/update them rather than creating duplicates."""
    
    # Prepare context for LLM
    release_schedule = state.get("release_schedule")
    context = {
        "existing_veps": [vep.model_dump(mode='json') for vep in existing_veps],
        "release_schedule": release_schedule.model_dump(mode='json') if release_schedule else None,
        "current_release": state.get("current_release"),
    }
    
    user_prompt = f"""Discover all VEPs from the kubevirt/enhancements repository.

Current state:
{json.dumps(context, indent=2, default=str)}

Use GitHub MCP tools to search for VEP tracking issues and read VEP documents. Create VEPInfo objects for all discovered VEPs and return them. If there are existing VEPs, update them with any new information found."""
    
    # Invoke LLM with structured output
    try:
        result = invoke_llm_check("fetch_veps", context, system_prompt, user_prompt, FetchVEPsResponse)
        
        discovered_veps = result.updated_veps
        discovered_count = len(discovered_veps)
        
        log(f"Discovered {discovered_count} VEP(s) from GitHub", node="fetch_veps")
        
        if discovered_count > 0:
            # Log some details about discovered VEPs
            vep_names = [vep.name for vep in discovered_veps[:5]]  # First 5
            log(f"Sample VEPs: {', '.join(vep_names)}{'...' if discovered_count > 5 else ''}", node="fetch_veps")
        
        # Update alerts if any
        alerts = state.get("alerts", [])
        alerts.extend(result.alerts)
        
        return {
            "last_check_times": last_check_times,
            "veps": discovered_veps,  # Replace VEPs with discovered ones
            "alerts": alerts,
            "next_tasks": next_tasks,
        }
        
    except Exception as e:
        log(f"Error fetching VEPs: {e}", node="fetch_veps", level="ERROR")
        import traceback
        log(f"Traceback: {traceback.format_exc()}", node="fetch_veps", level="ERROR")
        
        # Log error to state
        errors = state.get("errors", [])
        errors.append({
            "node": "fetch_veps",
            "error": str(e),
            "timestamp": datetime.now().isoformat(),
        })
        
        return {
            "last_check_times": last_check_times,
            "next_tasks": next_tasks,
            "errors": errors,
        }
