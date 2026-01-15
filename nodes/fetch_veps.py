"""VEP discovery node - fetches VEPs from kubevirt/enhancements repository."""

import json
import os
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
    
    # In test-sheets mode, create minimal mock VEPs without LLM calls
    # Check if mock VEPs should be used (from flag or legacy debug mode)
    mock_veps = state.get("mock_veps", False)
    debug_mode = os.environ.get("DEBUG_MODE")
    use_mock_veps = mock_veps or (debug_mode == "test-sheets")
    
    log(f"Mock VEPs check: mock_veps={mock_veps}, debug_mode={debug_mode}, use_mock_veps={use_mock_veps}", node="fetch_veps", level="DEBUG")
    
    if use_mock_veps:
        log("Mock VEPs mode enabled - creating minimal mock VEPs for testing (skipping GitHub fetch)", node="fetch_veps")
        
        # Import required models for mock VEPs
        from state import VEPMilestone, VEPCompliance, VEPActivity
        
        # Create a few minimal VEPs for testing sheets with all required fields
        now = datetime.now()
        mock_veps = [
            VEPInfo(
                tracking_issue_id=1001,
                name="vep-001",
                title="Test VEP 1",
                owner="testuser1",
                owning_sig="compute",
                status="open",
                last_updated=now,
                created_at=now,
                current_milestone=VEPMilestone(
                    version="v1.8",
                    status="Tracked",
                    promotion_phase="Net New",
                    exception_phase="None",
                    target_stage="Alpha",
                    all_code_prs_merged=False
                ),
                compliance=VEPCompliance(
                    template_complete=True,
                    all_sigs_signed_off=False,
                    vep_merged=True,
                    prs_linked=True,
                    docs_pr_created=False,
                    labels_valid=True
                ),
                activity=VEPActivity(
                    last_activity=now,
                    days_since_update=5,
                    review_lag_days=None
                ),
                tracking_issue=None,
                target_release="v1.8"
            ),
            VEPInfo(
                tracking_issue_id=1002,
                name="vep-002",
                title="Test VEP 2",
                owner="testuser2",
                owning_sig="network",
                status="in-progress",
                last_updated=now,
                created_at=now,
                current_milestone=VEPMilestone(
                    version="v1.8",
                    status="Tracked",
                    promotion_phase="Remaining",
                    exception_phase="None",
                    target_stage="Beta",
                    all_code_prs_merged=False
                ),
                compliance=VEPCompliance(
                    template_complete=True,
                    all_sigs_signed_off=False,
                    vep_merged=True,
                    prs_linked=True,
                    docs_pr_created=False,
                    labels_valid=True
                ),
                activity=VEPActivity(
                    last_activity=now,
                    days_since_update=2,
                    review_lag_days=1
                ),
                tracking_issue=None,
                target_release="v1.8"
            ),
            VEPInfo(
                tracking_issue_id=1003,
                name="vep-003",
                title="Test VEP 3",
                owner="testuser3",
                owning_sig="storage",
                status="closed",
                last_updated=now,
                created_at=now,
                current_milestone=VEPMilestone(
                    version="v1.9",
                    status="Complete",
                    promotion_phase="Graduating",
                    exception_phase="None",
                    target_stage="Stable",
                    all_code_prs_merged=True
                ),
                compliance=VEPCompliance(
                    template_complete=True,
                    all_sigs_signed_off=True,
                    vep_merged=True,
                    prs_linked=True,
                    docs_pr_created=True,
                    labels_valid=True
                ),
                activity=VEPActivity(
                    last_activity=now,
                    days_since_update=0,
                    review_lag_days=None
                ),
                tracking_issue=None,
                target_release="v1.9"
            ),
        ]
        log(f"Created {len(mock_veps)} mock VEPs for sheets testing", node="fetch_veps", level="DEBUG")
        return {
            "veps": mock_veps,
            "last_check_times": last_check_times,
            "next_tasks": next_tasks,
            "sheets_need_update": True,  # Trigger sheets update
        }
    
    # Build system prompt
    system_prompt = """You are a VEP governance agent discovering Virtualization Enhancement Proposals from the KubeVirt enhancements repository.

CRITICAL UNDERSTANDING - THE TRACKING ISSUE IS THE VEP:
================================================================================
THE TRACKING ISSUE IN kubevirt/enhancements IS THE PRIMARY DEFINITION OF A VEP.
A VEP exists as long as its tracking issue is open (until the feature reaches GA).

VEP Lifecycle:
1. A tracking ISSUE is opened in kubevirt/enhancements - THIS IS THE VEP
2. VEP PRs to kubevirt/enhancements create/update the VEP .md file:
   - Initial PR creates the design document
   - Additional PRs update it during the cycle (based on implementation)
   - PRs before cycle graduation update target (beta/GA)
3. Implementation PRs to kubevirt/kubevirt implement the VEP
4. The tracking issue remains open until the feature reaches GA

Edge Case (rare, but happens):
- Some people mistakenly open a VEP PR before the tracking issue
- In this case, a VEP file may exist without a corresponding issue
- This is wrong, but you should still discover these VEPs

PRIORITY ORDER FOR DISCOVERY:
1. TRACKING ISSUES are PRIMARY - they define VEP existence
2. VEP FILES are SECONDARY - they document the VEP (may not exist yet)
3. Handle edge case: Files without issues (wrong, but discover them)
================================================================================

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

Step 2: Process ALL VEP TRACKING ISSUES FIRST (PRIMARY - THE ISSUE IS THE VEP)
- The indexed_context["issues_index"] contains VEP-related issues - these are THE VEPs
- For EACH VEP-related issue in the index:
  * Extract VEP number from issue title/body/labels (e.g., "VEP 176", "VEP-176", "VEP #176", "vep-1234")
  * Use issue number as tracking_issue_id (REQUIRED - this is the primary identifier)
  * Extract title from issue title
  * Extract owner using this priority order:
    1. PRIMARY: Use issue.assignee (if the issue is assigned to someone, they are the owner)
    2. SECONDARY: Use issue.author (if not assigned, use the person who created/opened the issue)
    3. TERTIARY: Only if assignee and author are both null/missing, try to extract from issue body
    4. DO NOT use random mentions, comment authors, or bot usernames as owner
    5. DO NOT use usernames from the body unless they are explicitly stated as the owner (e.g., "Owner: @username")
  * Extract SIG from issue labels (sig/compute, sig/network, sig/storage)
  * Use issue state as status (open = active VEP, closed = completed/merged)
  * Use issue timestamps for created_at and last_updated
  * Create a VEPInfo object for THIS ISSUE - THE ISSUE IS THE VEP
- CRITICAL: Every VEP-related issue MUST become a VEPInfo object
- The tracking issue remains open until the feature reaches GA - this defines the VEP's lifecycle

Step 3: Match VEP Files to Tracking Issues (SECONDARY - FILES DOCUMENT THE VEP)
- The indexed_context["vep_files_index"] contains VEP files - these document the VEPs
- For EACH VEP file in the index:
  * Extract the VEP number from the file content (look for "VEP 176", "VEP-176", "VEP #176", etc.)
  * VEP numbers may also be in the filename (vep-0176.md) or path
  * Find the corresponding tracking issue (from Step 2) by matching VEP number
  * If a matching issue exists, enrich the VEPInfo with file content:
    - Update title if more detailed in file
    - Update owner if specified in file
    - Extract target release from file
    - Extract additional metadata from file
  * If NO matching issue exists (edge case - wrong but happens):
    - Create a VEPInfo object from the file
    - Use a placeholder tracking_issue_id or derive from VEP number
    - Note that this is an edge case (file without issue)
- VEP files are created/updated via PRs to kubevirt/enhancements
- Files may not exist yet for new VEPs - that's OK, the issue is still the VEP

Step 4: Find Related PRs
- VEP PRs to kubevirt/enhancements: Create/update the VEP .md file
  * Initial PR creates the design document
  * Additional PRs update it during the cycle
  * PRs before cycle graduation update target (beta/GA)
- Implementation PRs to kubevirt/kubevirt: Implement the VEP
- Use the indexed PRs list from kubevirt/kubevirt (provided in indexed_context)
- Search for PRs in kubevirt/enhancements that reference VEP numbers
- Match PRs from the index to their corresponding VEPs by:
  * Checking PR titles/bodies for VEP number references (e.g., "vep-1234", "VEP-1234")
  * Checking PR labels for VEP-related labels
  * Linking implementation PRs to VEP tracking issues
- Link PRs to their corresponding VEPs (tracking_issue_id is the key)

Step 5: Create VEPInfo Objects for ALL Discovered VEPs
- You MUST create a VEPInfo object for EVERY VEP tracking issue you found
- REMEMBER: THE TRACKING ISSUE IS THE VEP - files are just documentation
- For each discovered VEP, create a VEPInfo object with:
- tracking_issue_id: The GitHub issue number that tracks this VEP (REQUIRED - from Step 2)
- name: VEP identifier (e.g., "vep-1234") - extract from issue or file
- title: VEP title from the issue (preferred) or document
- owner: GitHub username of VEP owner (PRIORITY: issue.assignee > issue.author > explicit mention in body. DO NOT use random mentions or bots)
- owning_sig: Primary SIG ("compute", "network", or "storage") - from issue labels or file
- status: Current status from tracking issue (open = active, closed = completed)
- last_updated: Last update timestamp from tracking issue
- created_at: Creation timestamp from tracking issue
- current_milestone: Initial milestone data with target_release from current development cycle
- compliance: Initial compliance data (can be minimal, monitoring checks will fill in)
- activity: Initial activity data (can be minimal, monitoring checks will fill in)
- target_release: Target release version (from file if available, or issue)

CRITICAL REQUIREMENTS - YOU MUST FIND ALL VEPs (NO EXCEPTIONS):
================================================================================
MANDATORY PROCESSING REQUIREMENTS:
1. THE TRACKING ISSUE IS THE PRIMARY SOURCE OF TRUTH - process issues FIRST
2. For EACH VEP-related issue in indexed_context["issues_index"], you MUST:
   a. Create a VEPInfo object (THE ISSUE IS THE VEP) - MANDATORY, NO SKIPPING
   b. Extract VEP number from issue title/body/labels
   c. Use issue number as tracking_issue_id (REQUIRED)
   d. Extract all metadata from the issue
   e. Then match to VEP file (if exists) to enrich with file content
3. For EACH VEP file in indexed_context["vep_files_index"], you MUST:
   a. Extract the VEP number (e.g., vep-0176, vep-0168, etc.)
   b. Find the corresponding tracking issue (from Step 2)
   c. If issue exists: Enrich the VEPInfo with file content
   d. If NO issue exists (edge case): Create VEPInfo from file (note this is wrong but happens)
4. COUNT VERIFICATION: After processing, count your VEPInfo objects:
   - You MUST have at least one VEPInfo per VEP-related issue
   - You MUST have accounted for every VEP file (either enriched existing or created new)
   - If your count is less than the number of issues or files provided, you FAILED
   - GO BACK and process ALL items systematically - do not return until count is correct

ABSOLUTE PROHIBITIONS:
- DO NOT SKIP ANY ISSUES - every VEP-related issue MUST become a VEPInfo object
- DO NOT SKIP ANY FILES - match them to issues or handle edge case
- DO NOT FILTER - process everything provided
- DO NOT EXCLUDE - every item must be accounted for
- DO NOT RETURN until you have processed ALL items

The indexed context is your source of truth - it lists everything that exists.
PRIORITY: Issues first (they define VEPs), then files (they document VEPs).
If you return fewer VEPs than the number of issues or files provided, you have FAILED.
================================================================================

IMPORTANT GUIDANCE:
- You will receive INDEXED CONTEXT that pre-lists all issues and VEP files - USE THIS COMPREHENSIVE LIST
- The indexed context shows you exactly what exists - check EVERY item in the index systematically
- Do not rely on search/filtering - use the complete index provided to ensure nothing is missed
- The current development cycle version is critical - VEPs targeting this version are most relevant
- If you find existing VEPs in the state, merge/update them with new information rather than creating duplicates
- Process every item in the indexed context - completeness is more important than speed
- The indexed context eliminates guesswork - you know exactly what issues and files exist
- Process the index systematically: for each VEP file, create a VEPInfo; for each VEP issue without a file, create a VEPInfo

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
    # Get cache timeout from state (default: 60 minutes)
    index_cache_minutes = state.get("index_cache_minutes", 60)
    indexed_context = create_indexed_context(cache_max_age_minutes=index_cache_minutes)
    
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
    # Include ALL VEP files (no limit) - truncate content to save tokens but include all files
    vep_files_summary = []
    for vep_file in vep_files_index:  # Process ALL VEP files - no limit
        vep_summary = {
            "filename": vep_file.get("filename"),
            "vep_number": vep_file.get("vep_number"),
            "has_content": vep_file.get("content") is not None,
            "content_length": vep_file.get("content_length", 0),
        }
        # Include first 1500 chars of content (enough to extract metadata, reduced to fit more files)
        if vep_file.get("content"):
            content_preview = vep_file["content"][:1500]
            if len(vep_file["content"]) > 1500:
                truncated_msg = f"\n... (truncated, total length: {len(vep_file['content'])} chars)"
                content_preview = content_preview + truncated_msg
            vep_summary["content_preview"] = content_preview
        vep_files_summary.append(vep_summary)
    
    # Prepare issue summary text - include ALL issues (not just first 20)
    # For large lists, we'll include all but truncate body_preview to save tokens
    vep_issues_for_prompt = []
    for issue in vep_related_issues:
        issue_copy = issue.copy()
        # Truncate body_preview to 200 chars to save tokens while keeping all issues
        if "body_preview" in issue_copy and len(issue_copy["body_preview"]) > 200:
            issue_copy["body_preview"] = issue_copy["body_preview"][:200] + "..."
        vep_issues_for_prompt.append(issue_copy)
    
    vep_issues_text = json.dumps(vep_issues_for_prompt, indent=2, default=str)
    
    user_prompt = f"""Discover ALL VEPs from the kubevirt/enhancements repository.

================================================================================
CRITICAL: YOU MUST PROCESS EVERY SINGLE ITEM - NO EXCEPTIONS
================================================================================
You have been provided with:
- {len(vep_related_issues)} VEP-related ISSUES (these are THE VEPs - primary source)
- {len(vep_files_index)} VEP FILES (these document the VEPs - secondary source)

YOU MUST CREATE A VEPInfo OBJECT FOR EVERY SINGLE ONE OF THESE {len(vep_related_issues) + len(vep_files_index)} ITEMS.
DO NOT SKIP ANY. DO NOT FILTER. DO NOT EXCLUDE ANYTHING.
If you return fewer than {max(len(vep_related_issues), len(vep_files_index))} VEPs, you have FAILED.
================================================================================

CURRENT STATE:
{json.dumps({k: v for k, v in context.items() if k != "indexed_context"}, indent=2, default=str)}

INDEXED INFORMATION (pre-fetched - USE THIS DATA DIRECTLY):
- Release Info: {json.dumps(indexed_context.get("release_info"), indent=2, default=str) if indexed_context.get("release_info") else "Not available"}
- Enhancements README: {json.dumps(indexed_context.get("enhancements_readme"), indent=2, default=str) if indexed_context.get("enhancements_readme") else "Not available"}
- Issues Index: Found {len(issues_index)} total issues, {len(vep_related_issues)} VEP-related issues
  VEP-related issues: {vep_issues_text}
- PRs Index: Found {len(indexed_context.get("prs_index", []))} PRs: {json.dumps(indexed_context.get("prs_index", []), indent=2, default=str)}
- VEP Files: Found {len(vep_files_index)} VEP files with FULL CONTENT already indexed:
{json.dumps(vep_files_summary, indent=2, default=str)}

CRITICAL: The VEP files are ALREADY PARSED and their CONTENT is above. You do NOT need to read them again with tool calls!
- Each VEP file above contains: filename, vep_number, and content_preview (full content is available)
- Use the content_preview directly to extract VEP metadata (title, owner, target_release, SIG, etc.)
- Only use tool calls to read full issue details if the indexed data is insufficient

CRITICAL UNDERSTANDING - THE TRACKING ISSUE IS THE VEP:
================================================================================
THE TRACKING ISSUE IN kubevirt/enhancements IS THE PRIMARY DEFINITION OF A VEP.
A VEP exists as long as its tracking issue is open (until the feature reaches GA).

VEP Lifecycle:
1. A tracking ISSUE is opened in kubevirt/enhancements - THIS IS THE VEP
2. VEP PRs to kubevirt/enhancements create/update the VEP .md file
3. Implementation PRs to kubevirt/kubevirt implement the VEP
4. The tracking issue remains open until the feature reaches GA

PRIORITY: Issues FIRST (they define VEPs), Files SECOND (they document VEPs)
================================================================================

MANDATORY WORKFLOW (FOLLOW EXACTLY - NO SHORTCUTS):

Step 1: Read enhancements_readme above to understand VEP process and labels

Step 2: Process ALL {len(vep_related_issues)} VEP-related ISSUES (PRIMARY - THE ISSUES ARE THE VEPs):
   CRITICAL: You MUST process EVERY SINGLE ONE of the {len(vep_related_issues)} issues listed above.
   For EACH issue in the list:
     a. Extract VEP number from issue title/body/labels (patterns: "vep-1234", "VEP-1234", "vep1234", "VEP 1234", "VEP #1234")
     b. Use issue number as tracking_issue_id (REQUIRED - this is the primary identifier)
     c. Use issue title as VEP title
     d. Extract owner using this priority:
        1. Use issue.assignee if available (assigned person is the owner)
        2. Use issue.author if assignee is not available (creator is the owner)
        3. Only if both are missing, look for explicit owner mention in body (e.g., "Owner: @username")
        4. DO NOT use random mentions, comment authors, bot usernames, or people who just commented
     e. Extract SIG from issue labels (sig/compute, sig/network, sig/storage)
     f. Use issue state as status (open = active VEP, closed = completed)
     g. Use issue timestamps for created_at and last_updated
     h. Create a VEPInfo object for THIS ISSUE - THE ISSUE IS THE VEP
   
   VERIFICATION: After processing, you MUST have created {len(vep_related_issues)} VEPInfo objects from issues.
   Count them. If you have fewer, you missed some - go back and process ALL issues.

Step 3: Match VEP files to issues (SECONDARY - FILES DOCUMENT THE VEPs):
   CRITICAL: You MUST process EVERY SINGLE ONE of the {len(vep_files_index)} files listed above.
   For EACH file in the list:
     a. Extract VEP number, title, owner, target_release, SIG from the content_preview
     b. Find the corresponding tracking issue (from step 2) by matching VEP number
     c. If issue exists: Enrich the VEPInfo with file content (update title, owner, target_release if more detailed)
     d. If NO issue exists (edge case - wrong but happens): Create VEPInfo from file, note this is an edge case
   
   VERIFICATION: After processing, every file must either:
     - Enriched an existing VEPInfo (from step 2), OR
     - Created a new VEPInfo (if no matching issue)
   Count them. Every file must be accounted for.

Step 4: Cross-reference with PRs to link VEP PRs and implementation PRs

FINAL VERIFICATION REQUIREMENTS:
================================================================================
BEFORE RETURNING YOUR RESPONSE, YOU MUST VERIFY:

1. COUNT CHECK: You must return AT LEAST {max(len(vep_related_issues), len(vep_files_index))} VEPs
   - Minimum: {max(len(vep_related_issues), len(vep_files_index))} (one per issue OR one per file, whichever is larger)
   - Target: {len(vep_related_issues) + len(vep_files_index)} (all issues + all files, accounting for overlaps)
   - If you have fewer, you FAILED - go back and process ALL items

2. ISSUE COVERAGE: Every one of the {len(vep_related_issues)} VEP-related issues MUST have a corresponding VEPInfo
   - Check: For each issue number in the list above, verify you created a VEPInfo with that tracking_issue_id
   - Missing any? You FAILED - go back and process ALL issues

3. FILE COVERAGE: Every one of the {len(vep_files_index)} VEP files MUST be accounted for
   - Check: For each file in the list above, verify you either:
     * Enriched an existing VEPInfo (matched to an issue), OR
     * Created a new VEPInfo (no matching issue - edge case)
   - Missing any? You FAILED - go back and process ALL files

4. SYSTEMATIC PROCESSING: Work through the lists methodically:
   - Process issues in order: issue 1, issue 2, issue 3, ... issue {len(vep_related_issues)}
   - Process files in order: file 1, file 2, file 3, ... file {len(vep_files_index)}
   - Do not skip. Do not filter. Process EVERY item.

IF YOUR FINAL COUNT IS LESS THAN {max(len(vep_related_issues), len(vep_files_index))} VEPs, YOU HAVE FAILED.
GO BACK AND PROCESS ALL ITEMS SYSTEMATICALLY.
================================================================================

The indexed context above eliminates the need for most tool calls - use the data directly.
DO NOT skip any VEP-related issue - every issue marked as VEP-related must become a VEPInfo object.
DO NOT skip any VEP file - every file in the list above must result in a VEPInfo object."""
    
    # Invoke LLM with structured output
    try:
        log("About to invoke LLM for VEP discovery...", node="fetch_veps", level="DEBUG")
        result = invoke_llm_check("fetch_veps", context, system_prompt, user_prompt, FetchVEPsResponse)
        log("LLM invocation completed", node="fetch_veps", level="DEBUG")
        
        discovered_veps = result.updated_veps
        discovered_count = len(discovered_veps)
        
        # Calculate statistics for better logging
        vep_files_count = len(vep_files_index)
        vep_issues_count = len(vep_related_issues)
        # Expected minimum: at least as many as files (since each file should produce a VEP)
        # But also account for issues without files
        expected_min = max(vep_files_count, vep_issues_count)
        expected_target = vep_files_count + max(0, vep_issues_count - vep_files_count)  # All files + issues without files
        
        # Extract VEP numbers from files and issues for comparison
        vep_numbers_from_files = set()
        for vep_file in vep_files_index:
            vep_num = vep_file.get("vep_number")
            if vep_num:
                vep_numbers_from_files.add(vep_num.lower())
            # Also try to extract from filename
            filename = vep_file.get("filename", "")
            import re
            match = re.search(r'vep-?(\d+)', filename, re.IGNORECASE)
            if match:
                vep_numbers_from_files.add(f"vep-{int(match.group(1)):04d}".lower())
        
        vep_numbers_from_issues = set()
        for issue in vep_related_issues:
            # Extract VEP number from issue title/body
            title = issue.get("title", "")
            body = issue.get("body_preview", "")
            import re
            for text in [title, body]:
                match = re.search(r'vep-?(\d+)', text, re.IGNORECASE)
                if match:
                    vep_numbers_from_issues.add(f"vep-{int(match.group(1)):04d}".lower())
                    break
        
        # Extract VEP numbers from discovered VEPs
        discovered_vep_numbers = set()
        for vep in discovered_veps:
            vep_name = getattr(vep, 'name', '')
            if vep_name:
                discovered_vep_numbers.add(vep_name.lower())
        
        # Find missing VEPs
        missing_from_files = vep_numbers_from_files - discovered_vep_numbers
        missing_from_issues = vep_numbers_from_issues - discovered_vep_numbers
        
        # Count VEPs by status and SIG
        open_count = sum(1 for vep in discovered_veps if hasattr(vep, 'status') and vep.status and 'open' in str(vep.status).lower())
        closed_count = discovered_count - open_count
        
        sig_counts = {}
        for vep in discovered_veps:
            sig = getattr(vep, 'owning_sig', None) or 'unknown'
            sig_counts[sig] = sig_counts.get(sig, 0) + 1
        
        # Log comprehensive summary
        log("="*80, node="fetch_veps")
        log(f"VEP DISCOVERY SUMMARY", node="fetch_veps")
        log("="*80, node="fetch_veps")
        log(f"Total VEPs discovered: {discovered_count}", node="fetch_veps")
        log(f"  - Expected minimum: {expected_min} (based on {vep_files_count} VEP files and {vep_issues_count} VEP-related issues)", node="fetch_veps")
        log(f"  - Expected target: {expected_target} (all {vep_files_count} files + {max(0, vep_issues_count - vep_files_count)} issues without files)", node="fetch_veps")
        
        if discovered_count > 0:
            log(f"  - Status breakdown: {open_count} open, {closed_count} closed/merged", node="fetch_veps")
            if sig_counts:
                sig_breakdown = ", ".join([f"{sig}: {count}" for sig, count in sorted(sig_counts.items())])
                log(f"  - SIG breakdown: {sig_breakdown}", node="fetch_veps")
            
            # Log all discovered VEP names
            vep_names = [vep.name for vep in discovered_veps]
            log(f"  - Discovered VEPs: {', '.join(sorted(vep_names))}", node="fetch_veps")
        else:
            log(f"  - WARNING: No VEPs discovered! Expected at least {expected_min} VEPs.", node="fetch_veps", level="WARNING")
        
        # Log missing VEPs for debugging
        if missing_from_files:
            log(f"  - MISSING VEPs from files ({len(missing_from_files)}): {', '.join(sorted(missing_from_files))}", node="fetch_veps", level="ERROR")
        if missing_from_issues:
            log(f"  - MISSING VEPs from issues ({len(missing_from_issues)}): {', '.join(sorted(missing_from_issues))}", node="fetch_veps", level="ERROR")
        
        if discovered_count < expected_min:
            log(f"  - ERROR: Discovered {discovered_count} VEPs but expected at least {expected_min} (missing {expected_min - discovered_count})", node="fetch_veps", level="ERROR")
            log(f"  - This indicates the LLM did not process all VEP files or issues. Check the prompt and LLM response.", node="fetch_veps", level="ERROR")
        elif discovered_count < expected_target:
            log(f"  - WARNING: Discovered {discovered_count} VEPs but target was {expected_target} (missing {expected_target - discovered_count} issues without files)", node="fetch_veps", level="WARNING")
        
        log("="*80, node="fetch_veps")
        
        # Update alerts if any
        alerts = state.get("alerts", [])
        alerts.extend(result.alerts)
        
        # If skip_monitoring is enabled, set sheets_need_update to trigger analyze_combined
        # (which will then trigger both update_sheets and alert_summary in parallel)
        skip_monitoring = state.get("skip_monitoring", False)
        sheets_need_update = False
        if skip_monitoring and discovered_count > 0:
            sheets_need_update = True
            log("Skip-monitoring mode: Setting sheets_need_update to trigger analyze_combined (which will trigger alert_summary)", node="fetch_veps")
        
        return {
            "last_check_times": last_check_times,
            "veps": discovered_veps,  # Replace VEPs with discovered ones
            "alerts": alerts,
            "next_tasks": next_tasks,
            "sheets_need_update": sheets_need_update,  # Set flag if skip_monitoring enabled
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
