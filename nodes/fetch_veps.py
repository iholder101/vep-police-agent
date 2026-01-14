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
    
    # Count VEP-related issues
    issues_index = indexed_context.get("issues_index", [])
    vep_related_issues = [issue for issue in issues_index if issue.get("is_vep_related", False)]
    vep_files_index = indexed_context.get("vep_files_index", [])
    
    # Prepare indexed context summary for the prompt
    # Include full VEP files data (but truncate very long content to avoid token limits)
    vep_files_summary = []
    for vep_file in vep_files_index[:50]:  # Limit to first 50 VEP files to avoid token overflow
        vep_summary = {
            "filename": vep_file.get("filename"),
            "vep_number": vep_file.get("vep_number"),
            "has_content": vep_file.get("content") is not None,
            "content_length": vep_file.get("content_length", 0),
        }
        # Include first 2000 chars of content (enough to extract metadata)
        if vep_file.get("content"):
            content_preview = vep_file["content"][:2000]
            if len(vep_file["content"]) > 2000:
                truncated_msg = f"\n... (truncated, total length: {len(vep_file['content'])} chars)"
                content_preview = content_preview + truncated_msg
            vep_summary["content_preview"] = content_preview
        vep_files_summary.append(vep_summary)
    
    # Prepare issue summary text (avoid backslash in f-string)
    vep_issues_json = json.dumps(vep_related_issues[:20], indent=2, default=str)
    if len(vep_related_issues) > 20:
        vep_issues_text = vep_issues_json + f"\n... and {len(vep_related_issues) - 20} more VEP-related issues"
    else:
        vep_issues_text = vep_issues_json
    
    user_prompt = f"""Discover all VEPs from the kubevirt/enhancements repository.

CURRENT STATE:
{json.dumps({k: v for k, v in context.items() if k != "indexed_context"}, indent=2, default=str)}

INDEXED INFORMATION (pre-fetched - USE THIS DATA DIRECTLY, DO NOT RE-READ FILES):
- Release Info: {json.dumps(indexed_context.get("release_info"), indent=2, default=str) if indexed_context.get("release_info") else "Not available"}
- Enhancements README: {json.dumps(indexed_context.get("enhancements_readme"), indent=2, default=str) if indexed_context.get("enhancements_readme") else "Not available"}
- Issues Index: Found {len(issues_index)} total issues, {len(vep_related_issues)} VEP-related issues
  VEP-related issues: {vep_issues_text}
- PRs Index: Found {len(indexed_context.get("prs_index", []))} PRs (first 10): {json.dumps(indexed_context.get("prs_index", [])[:10], indent=2, default=str)}
- VEP Files: Found {len(vep_files_index)} VEP files with FULL CONTENT already indexed:
{json.dumps(vep_files_summary, indent=2, default=str)}

CRITICAL: The VEP files are ALREADY PARSED and their CONTENT is above. You do NOT need to read them again with tool calls!
- Each VEP file above contains: filename, vep_number, and content_preview (full content is available)
- Use the content_preview directly to extract VEP metadata (title, owner, target_release, SIG, etc.)
- Only use tool calls to read full issue details if the indexed data is insufficient

WORKFLOW:
1. Read enhancements_readme above to understand VEP process and labels
2. For each VEP file listed above ({len(vep_files_index)} files):
   - Extract VEP number, title, owner, target_release, SIG from the content_preview
   - Find the corresponding tracking issue from the issues list (match by VEP number in title/labels)
   - Create VEPInfo object with all available information
3. For VEP-related issues that don't have a VEP file yet:
   - Use tool calls to read full issue details if needed
   - Create VEPInfo object from issue information
4. Cross-reference with PRs to link implementation PRs

IMPORTANT: 
- You have {len(vep_files_index)} VEP files with content already - use them directly from the indexed data above!
- A typical release cycle has 20-30+ VEPs - if you find fewer, you're missing some
- Check ALL {len(vep_files_index)} VEP files systematically
- The indexed context above eliminates the need for most tool calls - use the data directly"""
    
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
