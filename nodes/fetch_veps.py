"""VEP discovery node - fetches VEPs from kubevirt/enhancements repository."""

import json
from datetime import datetime
from typing import Any
from state import VEPState, VEPInfo
from services.utils import log
from services.llm_helper import invoke_llm_check
from services.response_models import CheckResponse
from services.indexer import create_indexed_context


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

Step 0: Read VEP Process Documentation (CRITICAL FIRST STEP)
- Read the enhancements README.md content provided in indexed_context
- This contains:
  * VEP process documentation and requirements
  * Labels used to identify VEP tracking issues (e.g., "kind/vep", "area/enhancement")
  * How VEPs are structured and organized
  * What makes an issue a VEP tracker
  * Process requirements and compliance criteria
- Understanding this documentation is ESSENTIAL for correctly identifying VEPs

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
- Use the indexed PRs list from kubevirt/kubevirt (provided in indexed_context)
- Search for PRs in kubevirt/enhancements that reference VEP numbers
- Match PRs from the index to their corresponding VEPs by:
  * Checking PR titles/bodies for VEP number references (e.g., "vep-1234", "VEP-1234")
  * Checking PR labels for VEP-related labels
  * Linking implementation PRs to VEP tracking issues
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
- You will receive INDEXED CONTEXT that pre-lists all issues and VEP files - USE THIS COMPREHENSIVE LIST
- The indexed context shows you exactly what exists - check EVERY item in the index systematically
- Do not rely on search/filtering - use the complete index provided to ensure nothing is missed
- The current development cycle version is critical - VEPs targeting this version are most relevant
- If you find existing VEPs in the state, merge/update them with new information rather than creating duplicates
- A typical release cycle has 20-30+ VEPs - if you find fewer, you're likely missing some
- The indexed context eliminates guesswork - you know exactly what issues and files exist

Use GitHub MCP tools to:
- Search/list ALL issues in kubevirt/enhancements repository
- Read release schedule from kubevirt/sig-release/releases/{version}/schedule.md
- List and read ALL files from veps/ directory
- Get issue details and metadata
- Search for PRs referencing VEP numbers
- Parse VEP documents to extract metadata

Return ALL discovered VEPs as a list of VEPInfo objects."""
    
    # Create indexed context - pre-fetch key information for precision
    log("Creating indexed context for VEP discovery", node="fetch_veps")
    indexed_context = create_indexed_context()
    
    # Prepare context for LLM
    release_schedule = state.get("release_schedule")
    context = {
        "existing_veps": [vep.model_dump(mode='json') for vep in existing_veps],
        "release_schedule": release_schedule.model_dump(mode='json') if release_schedule else None,
        "current_release": state.get("current_release"),
        "indexed_context": indexed_context,  # Add indexed information
    }
    
    user_prompt = f"""Discover all VEPs from the kubevirt/enhancements repository.

CURRENT STATE:
{json.dumps({k: v for k, v in context.items() if k != "indexed_context"}, indent=2, default=str)}

INDEXED INFORMATION (pre-fetched for your reference):
- Release Info: {json.dumps(indexed_context.get("release_info"), indent=2, default=str) if indexed_context.get("release_info") else "Not available"}
- Enhancements README: {"Available - contains VEP process documentation, labels, and structure" if indexed_context.get("enhancements_readme") else "Not available"}
- Issues Index: Found {len(indexed_context.get("issues_index", []))} issues in kubevirt/enhancements (last {indexed_context.get("days_back", "all")} days)
- PRs Index: Found {len(indexed_context.get("prs_index", []))} PRs in kubevirt/kubevirt (last {indexed_context.get("days_back", "all")} days)
- VEP Files Index: Found {len(indexed_context.get("vep_files_index", []))} items in veps/ directory

The indexed information above gives you a complete picture of what exists. Use this to:
1. Understand the VEP process (from enhancements_readme - READ THIS FIRST to understand labels, structure, requirements)
2. Know the current release version (from release_info)
3. Have a list of all issues to check (from issues_index)
4. Have a list of all PRs that might reference VEPs (from prs_index)
5. Have a list of all VEP files to read (from vep_files_index)

CRITICAL: Read the enhancements_readme content first to understand:
- What labels identify VEP tracking issues
- How VEPs are structured and organized
- What the process requirements are
- How to identify VEP-related issues and PRs

Now use GitHub MCP tools to:
- Read the full details of issues mentioned in the issues_index
- Read the full content of VEP files mentioned in the vep_files_index
- Cross-reference and create VEPInfo objects for ALL discovered VEPs

IMPORTANT: The indexed information shows you what EXISTS. Your job is to read the full details and create complete VEPInfo objects. Do not skip any items from the index - check each one systematically."""
    
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
