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

CRITICAL: You must follow this systematic workflow to discover ALL VEPs. Do not skip steps.

WORKFLOW (follow in order):

Step 1: Determine Current Development Cycle
- First, check the release schedule from kubevirt/sig-release repository
- Navigate to: kubevirt/sig-release/releases/ directory
- Find the current active release by checking schedule.md files
- The current release is typically the one with future dates (EF, CF, GA)
- Read the schedule.md file to understand the current development cycle version (e.g., v1.8)
- This is critical for finding VEPs targeting the current release

Step 2: Query All Issues in kubevirt/enhancements
- Use GitHub search/list_issues to get ALL issues in kubevirt/enhancements repository
- Do not filter - get all issues first, then identify VEP trackers
- Look for issues that:
  * Have labels like "kind/vep", "vep", or similar VEP-related labels
  * Reference VEP numbers in title or body (e.g., "vep-1234", "VEP-1234")
  * Are tracking issues for enhancements
- VEP tracking issues typically link to VEP documents in the veps/ directory

Step 3: Read VEP Documents from veps/ Directory
- List all files in kubevirt/enhancements/veps/ directory
- Read each VEP markdown file (vep-*.md format)
- Each VEP document contains:
  * VEP number (e.g., vep-1234)
  * Title and description
  * Owner information
  * Target release version
  * SIG information
  * Status and metadata

Step 4: Find Related PRs
- Search for PRs in kubevirt/enhancements that reference VEP numbers
- Search for PRs in kubevirt/kubevirt that implement VEP features
- Link PRs to their corresponding VEPs

Step 5: Create VEPInfo Objects
For each discovered VEP, create a VEPInfo object with:
- tracking_issue_id: The GitHub issue number that tracks this VEP
- name: VEP identifier (e.g., "vep-1234")
- title: VEP title from the document or issue
- owner: GitHub username of VEP owner (from issue or document)
- owning_sig: Primary SIG ("compute", "network", or "storage")
- status: Current status from tracking issue
- last_updated: Last update timestamp from issue or document
- created_at: Creation timestamp
- current_milestone: Initial milestone data with target_release from current development cycle
- compliance: Initial compliance data (can be minimal, monitoring checks will fill in)
- activity: Initial activity data (can be minimal, monitoring checks will fill in)
- target_release: Target release version (should match current development cycle if active)

IMPORTANT GUIDANCE:
- You must be THOROUGH - query ALL issues, read ALL VEP documents, find ALL related PRs
- Do not assume you found all VEPs after finding a few - continue searching systematically
- The current development cycle version is critical - VEPs targeting this version are most relevant
- If you find existing VEPs in the state, merge/update them with new information rather than creating duplicates
- A typical release cycle has 20-30+ VEPs - if you find fewer, you're likely missing some

Use GitHub MCP tools to:
- Search/list ALL issues in kubevirt/enhancements repository
- Read release schedule from kubevirt/sig-release/releases/{version}/schedule.md
- List and read ALL files from veps/ directory
- Get issue details and metadata
- Search for PRs referencing VEP numbers
- Parse VEP documents to extract metadata

Return ALL discovered VEPs as a list of VEPInfo objects."""
    
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
