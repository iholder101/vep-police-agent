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


def _create_mock_veps() -> list[VEPInfo]:
    """Create mock VEPs for testing without GitHub API calls."""
    from state import VEPMilestone, VEPCompliance, VEPActivity

    now = datetime.now()
    return [
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
        mock_veps = _create_mock_veps()
        log(f"Created {len(mock_veps)} mock VEPs for sheets testing", node="fetch_veps", level="DEBUG")
        return {
            "veps": mock_veps,
            "last_check_times": last_check_times,
            "next_tasks": next_tasks,
            "sheets_need_update": True,  # Trigger sheets update
            "release_phase": "development",  # Mock phase for testing
        }
    
    # Build system prompt
    system_prompt = """You are a VEP governance agent discovering Virtualization Enhancement Proposals from KubeVirt.

CORE CONCEPT: The tracking issue in kubevirt/enhancements IS the VEP. It remains open until GA.
- Priority: Issues (define VEPs) → Files (document VEPs) → Edge case: files without issues

VEP LIFECYCLE:
1. Tracking issue opened → VEP exists
2. VEP PRs create/update the .md file in kubevirt/enhancements
3. Implementation PRs in kubevirt/kubevirt implement the feature
4. Tracking issue closes at GA

WORKFLOW:
1. Read enhancements README.md from indexed_context for VEP process/labels
2. Use release_info from indexed_context for current development cycle
3. Process ALL issues from indexed_context["issues_index"]:
   - Extract VEP number (patterns: "VEP 176", "VEP-176", "vep-1234")
   - tracking_issue_id = issue number (required)
   - Owner priority: issue.assignee > issue.author > explicit "Owner: @user" in body
   - Do NOT use random mentions, comment authors, or bots as owner
   - SIG from labels (sig/compute, sig/network, sig/storage)
   - Status from issue state (open = active, closed = completed)
4. Match VEP files from indexed_context["vep_files_index"] to issues:
   - If issue exists: enrich VEPInfo with file content (title, target_release, etc.)
   - If no issue (edge case): create VEPInfo from file
5. Link PRs using indexed_context["vep_to_pr_mappings"] and "approved_vep_prs"
6. Populate board_fields from indexed_context["project_board_items"]

DATA PRECEDENCE:
- GitHub issue/PR state is authoritative (project board may be outdated)
- When issue and board conflict, trust the issue

VEPINFO FIELDS:
- tracking_issue_id: GitHub issue number (required, primary identifier)
- name: VEP identifier (e.g., "vep-1234")
- title, owner, owning_sig, status, last_updated, created_at
- target_release, current_milestone, compliance, activity
- board_fields: from project_board_items lookup
- implementation_prs: from vep_to_pr_mappings lookup

MISSING DATA: Use sensible defaults. If target_release unknown, leave empty. Note gaps in analysis.

EXAMPLE OUTPUT (one VEPInfo):
{
  "tracking_issue_id": 176,
  "name": "vep-0176",
  "title": "VM Export API",
  "owner": "jdoe",
  "owning_sig": "compute",
  "status": "open",
  "target_release": "v1.8"
}

REQUIREMENTS:
- Create VEPInfo for EVERY issue in issues_index
- Account for EVERY file in vep_files_index (enrich or create)
- If existing VEPs in state, UPDATE by tracking_issue_id (don't duplicate)
- Return count must be >= max(issues_count, files_count)

Return ALL discovered VEPs as VEPInfo objects."""
    
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
    
    user_prompt = f"""Discover ALL VEPs from kubevirt/enhancements.

INPUT COUNTS: {len(vep_related_issues)} issues, {len(vep_files_index)} files
MINIMUM OUTPUT: {max(len(vep_related_issues), len(vep_files_index))} VEPs

CURRENT STATE:
{json.dumps({k: v for k, v in context.items() if k != "indexed_context"}, indent=2, default=str)}

INDEXED DATA (use directly, no tool calls needed for this data):

Release Info: {json.dumps(indexed_context.get("release_info"), indent=2, default=str) if indexed_context.get("release_info") else "Not available"}

Release Phase: {indexed_context.get("release_phase", "unknown")}
(design=pre-EF, development=EF-CF, stabilization=post-CF highest risk, post_release=done)

Enhancements README: {json.dumps(indexed_context.get("enhancements_readme"), indent=2, default=str) if indexed_context.get("enhancements_readme") else "Not available"}

VEP-RELATED ISSUES ({len(vep_related_issues)} total):
{vep_issues_text}

VEP FILES ({len(vep_files_index)} total, content already parsed):
{json.dumps(vep_files_summary, indent=2, default=str)}

PRs Index: {json.dumps(indexed_context.get("prs_index", []), indent=2, default=str)}

VEP-to-PR Mappings: {json.dumps(indexed_context.get("vep_to_pr_mappings", {}), indent=2, default=str)}

Project Board Items: {json.dumps(indexed_context.get("project_board_items", {}), indent=2, default=str)}

Approved-VEP PRs (is_mismatched=true means label but no VEP reference):
{json.dumps(indexed_context.get("approved_vep_prs", []), indent=2, default=str)}

VEPs Missing PRs (urgent in stabilization): {json.dumps(indexed_context.get("veps_missing_prs", []), indent=2, default=str)}

PROCESS:
1. Create VEPInfo for each of the {len(vep_related_issues)} issues
2. Enrich with file content where VEP numbers match
3. Create VEPInfo for files without matching issues (edge case)
4. Link PRs from mappings

VERIFY before returning: count >= {max(len(vep_related_issues), len(vep_files_index))}"""
    
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
        log("VEP DISCOVERY SUMMARY", node="fetch_veps")
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
            log("  - This indicates the LLM did not process all VEP files or issues. Check the prompt and LLM response.", node="fetch_veps", level="ERROR")
        elif discovered_count < expected_target:
            log(f"  - WARNING: Discovered {discovered_count} VEPs but target was {expected_target} (missing {expected_target - discovered_count} issues without files)", node="fetch_veps", level="WARNING")
        
        log("="*80, node="fetch_veps")
        
        # Merge discovered VEPs with existing VEPs (UPDATE, don't replace)
        # Match by tracking_issue_id (primary identifier)
        existing_veps_dict = {vep.tracking_issue_id: vep for vep in existing_veps}
        merged_veps = []
        updated_count = 0
        new_count = 0
        
        for discovered_vep in discovered_veps:
            existing_vep = existing_veps_dict.get(discovered_vep.tracking_issue_id)
            if existing_vep:
                # Update existing VEP - preserve fields that haven't changed, update with new info
                # The LLM should have already done intelligent merging, but we preserve existing fields as fallback
                # For now, use the discovered VEP (LLM should have merged intelligently)
                # TODO: Could add more sophisticated merging logic here if needed
                merged_veps.append(discovered_vep)
                updated_count += 1
                log(f"Updated existing VEP {discovered_vep.tracking_issue_id} ({discovered_vep.name})", node="fetch_veps", level="DEBUG")
            else:
                # New VEP - add it
                merged_veps.append(discovered_vep)
                new_count += 1
                log(f"Discovered new VEP {discovered_vep.tracking_issue_id} ({discovered_vep.name})", node="fetch_veps", level="DEBUG")
        
        # Preserve any existing VEPs that weren't in the discovery result (shouldn't happen, but be safe)
        discovered_ids = {vep.tracking_issue_id for vep in discovered_veps}
        for existing_vep in existing_veps:
            if existing_vep.tracking_issue_id not in discovered_ids:
                log(f"Preserving existing VEP {existing_vep.tracking_issue_id} ({existing_vep.name}) not in discovery result", node="fetch_veps", level="WARNING")
                merged_veps.append(existing_vep)
        
        log(f"VEP merge complete: {new_count} new, {updated_count} updated, {len(merged_veps)} total", node="fetch_veps")

        # NOTE: Do NOT add alerts here - alert_summary node is responsible for all alert generation
        # This avoids duplicate alerts from multiple nodes
        if result.alerts:
            log(f"LLM identified {len(result.alerts)} discovery issue(s) (alerts generated by alert_summary)", node="fetch_veps")

        # If skip_monitoring is enabled, set sheets_need_update to trigger analyze_combined
        skip_monitoring = state.get("skip_monitoring", False)
        sheets_need_update = False
        if skip_monitoring and (new_count > 0 or updated_count > 0):
            sheets_need_update = True
            log("Skip-monitoring mode: Setting sheets_need_update to trigger analyze_combined", node="fetch_veps")

        # Extract release phase from indexed context
        release_phase = indexed_context.get("release_phase", "unknown")
        log(f"Release phase: {release_phase}", node="fetch_veps")

        return {
            "last_check_times": last_check_times,
            "veps": merged_veps,  # Merged VEPs (updated + new)
            "next_tasks": next_tasks,
            "sheets_need_update": sheets_need_update,  # Set flag if skip_monitoring enabled
            "release_phase": release_phase,  # Pass release phase to downstream nodes
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
