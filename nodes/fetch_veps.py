"""VEP discovery node - fetches VEPs from indexed context.

Constructs VEPInfo objects directly from indexed data (board_veps, issues, files)
without using LLM, making it fast, reliable, and not subject to token limits.
"""

import re
from datetime import datetime, timezone
from typing import Any, List, Dict, Optional
from state import VEPState, VEPInfo, VEPMilestone, VEPCompliance, VEPActivity, PRInfo
from services.utils import log
from services.indexer import create_indexed_context
from config.config import DEFAULT_RELEASE, KNOWN_SIGS


def _extract_vep_number_from_text(text: str) -> Optional[str]:
    """Extract VEP number from text (e.g., 'VEP 176' -> 'vep-0176')."""
    if not text:
        return None
    match = re.search(r'vep[- ]?(\d+)', text, re.IGNORECASE)
    if match:
        return f"vep-{int(match.group(1)):04d}"
    return None


def _parse_owner_from_issue(issue: Dict[str, Any]) -> str:
    """Extract owner from issue (assignee > author > 'unknown')."""
    # Priority: assignee > author
    assignee = issue.get("assignee")
    if assignee and isinstance(assignee, dict):
        return assignee.get("login", "unknown")

    author = issue.get("author")
    if author and isinstance(author, dict):
        return author.get("login", "unknown")

    return "unknown"


def _parse_sig_from_labels(labels: List[str]) -> str:
    """Extract SIG from labels (sig/compute, sig/network, sig/storage)."""
    for label in labels:
        if label.startswith("sig/"):
            sig = label.split("/", 1)[1]
            if sig in KNOWN_SIGS:
                return sig
    return "unknown"


def _parse_target_release_from_file(vep_file_content: str) -> Optional[str]:
    """Extract target release from VEP file content."""
    if not vep_file_content:
        return None

    # Look for patterns like:
    # - Release: v1.8
    # - Target Release: v1.8
    # - targetRelease: v1.8
    # - This VEP targets alpha for version: 1.8 (kubevirt/enhancements#212 format)
    patterns = [
        r'targets\s+\w+\s+for\s+version:\s*v?([\d.]+)',
        r'target[- ]?release:\s*v?([\d.]+)',
        r'release:\s*v?([\d.]+)',
    ]

    for pattern in patterns:
        match = re.search(pattern, vep_file_content, re.IGNORECASE | re.MULTILINE)
        if match:
            version = match.group(1)
            # Normalize to v1.8 format
            return f"v{version}" if not version.startswith('v') else version

    return None


def _create_vep_from_board_item(
    issue_number: int,
    board_vep: Dict[str, Any],
    issues_by_number: Dict[int, Dict[str, Any]],
    vep_files_by_number: Dict[str, Dict[str, Any]],
    current_release: str
) -> Optional[VEPInfo]:
    """Create VEPInfo from board item, enriching with issue and file data."""

    tracking_issue_id = issue_number

    # Get corresponding issue
    issue = issues_by_number.get(tracking_issue_id)
    if not issue:
        log(f"No issue found for board VEP #{tracking_issue_id}, using board data only", node="fetch_veps", level="DEBUG")

    # Extract VEP number from title or board data
    title = board_vep.get("title") or (issue.get("title") if issue else "Unknown VEP")
    vep_number = _extract_vep_number_from_text(title)
    if not vep_number:
        # Fallback: use tracking issue number
        vep_number = f"vep-{tracking_issue_id:04d}"

    # Get VEP file for this VEP number
    vep_number_int = int(vep_number.split("-")[1]) if "-" in vep_number else None
    vep_file = vep_files_by_number.get(vep_number_int) if vep_number_int else None

    # Parse owner (issue assignee/author > board assignee > unknown)
    owner = "unknown"
    if issue:
        owner = _parse_owner_from_issue(issue)
    elif board_vep.get("assignees"):
        assignees = board_vep.get("assignees", [])
        if assignees and len(assignees) > 0:
            owner = assignees[0] if isinstance(assignees[0], str) else assignees[0].get("login", "unknown")

    # Parse SIG from labels
    labels = issue.get("labels", []) if issue else board_vep.get("labels", [])
    owning_sig = _parse_sig_from_labels(labels)

    # Parse status
    status = issue.get("state", "open") if issue else board_vep.get("status", "open")

    # Parse timestamps
    created_at = datetime.fromisoformat(issue.get("created_at").replace('Z', '+00:00')) if issue and issue.get("created_at") else datetime.now()
    last_updated = datetime.fromisoformat(issue.get("updated_at").replace('Z', '+00:00')) if issue and issue.get("updated_at") else datetime.now()

    # Ensure timezone-aware (GitHub timestamps are UTC)
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    if last_updated.tzinfo is None:
        last_updated = last_updated.replace(tzinfo=timezone.utc)

    # Calculate days since update
    days_since_update = (datetime.now(timezone.utc) - last_updated).days

    # Parse target release from VEP file or use current release
    target_release = current_release
    if vep_file and vep_file.get("content"):
        parsed_release = _parse_target_release_from_file(vep_file["content"])
        if parsed_release:
            target_release = parsed_release

    # Build milestone (from board fields or defaults)
    # Board custom fields are stored in board_vep["fields"] dict
    board_fields = board_vep.get("fields", {})
    milestone_status = board_fields.get("Status", "Tracked")

    # Skip VEPs that are removed from milestone or completed
    if milestone_status in ["Removed from Milestone", "Complete"]:
        log(f"Skipping VEP #{tracking_issue_id} ({title}) - status: {milestone_status}", node="fetch_veps", level="DEBUG")
        return None

    promotion_phase = board_fields.get("Promotion Phase", "Net New")
    exception_phase = board_fields.get("Exception Phase", "None")
    target_stage = board_fields.get("Target Stage", "Alpha")

    current_milestone = VEPMilestone(
        version=current_release or "unknown",
        status=milestone_status if milestone_status in ['Proposed for consideration', 'Tracked', 'Unchanged', 'Removed from Milestone', 'Exception Required', 'At risk', 'Complete'] else 'Tracked',
        promotion_phase=promotion_phase if promotion_phase in ['Net New', 'Remaining', 'Graduating', 'Deprecation'] else 'Net New',
        exception_phase=exception_phase if exception_phase in ['Accepted', 'Pending', 'Rejected', 'Completed', 'None'] else 'None',
        target_stage=target_stage if target_stage in ['Alpha', 'Beta', 'Stable', 'Deprecation/Removal'] else 'Alpha',
        all_code_prs_merged=False  # Will be determined by analyze_combined
    )

    # Build compliance (defaults - will be checked by check_compliance node)
    compliance = VEPCompliance(
        template_complete=True,  # Assume true until checked
        all_sigs_signed_off=False,
        vep_merged=True if status == "closed" else False,
        prs_linked=len(board_vep.get("impl_prs", [])) > 0,
        docs_pr_created=False,
        labels_valid=owning_sig != "unknown"
    )

    # Build activity
    activity = VEPActivity(
        last_activity=last_updated,
        days_since_update=days_since_update,
        review_lag_days=None  # Will be calculated by check_activity node
    )

    # Build implementation PRs from board data
    # Note: board_vep.impl_prs only has {number, url} from _parse_impl_prs_from_text
    # We create minimal PRInfo objects - full data will be fetched by check_activity if needed
    # IMPORTANT: Skip enhancements PRs - they are proposal PRs, not implementation PRs
    implementation_prs = []
    for pr_data in board_vep.get("impl_prs", []):
        if pr_data.get("number"):
            pr_url = pr_data.get("url", "")
            # Skip enhancements PRs (they are proposal PRs, not implementation PRs)
            if "enhancements" in pr_url:
                continue
            # Use current time as placeholder for required datetime fields
            # These will be updated when full PR data is fetched
            now = datetime.now(timezone.utc)
            pr = PRInfo(
                number=pr_data.get("number"),
                title=f"PR #{pr_data.get('number')}",  # Placeholder title
                url=pr_url,
                state="unknown",  # Will be updated when fetched
                created_at=now,
                updated_at=now,
                author="unknown"
            )
            implementation_prs.append(pr)

    # Create VEPInfo
    vep_info = VEPInfo(
        tracking_issue_id=tracking_issue_id,
        name=vep_number,
        title=title,
        owner=owner,
        owning_sig=owning_sig,
        status=status,
        last_updated=last_updated,
        created_at=created_at,
        current_milestone=current_milestone,
        compliance=compliance,
        activity=activity,
        tracking_issue=None,  # Will be enriched by other nodes if needed
        enhancement_prs=[],  # Will be populated by other nodes
        implementation_prs=implementation_prs,
        target_release=target_release,
        board_fields=board_vep  # Store all board fields for reference
    )

    return vep_info


def fetch_veps_node(state: VEPState) -> Any:
    """Discover VEPs from indexed context.

    Constructs VEPInfo objects directly from board_veps, issues, and files
    without using LLM. Fast, reliable, and not subject to token limits.

    Process:
    1. Get indexed context (board_veps is source of truth)
    2. For each board VEP, create VEPInfo by merging:
       - board_veps: tracking_issue_id, impl_prs, board_fields
       - issues: title, owner, status, timestamps
       - vep_files: target_release from content
    3. Return all board-tracked VEPs
    """
    existing_veps = state.get("veps", [])
    existing_count = len(existing_veps)
    log(f"Fetching VEPs from indexed context | Current VEPs: {existing_count}", node="fetch_veps")

    last_check_times = state.get("last_check_times", {})
    last_check_times["fetch_veps"] = datetime.now()

    # Remove this task from queue
    next_tasks = state.get("next_tasks", [])
    if next_tasks and next_tasks[0] == "fetch_veps":
        next_tasks = next_tasks[1:]

    # Get indexed context
    log("Creating indexed context for VEP discovery", node="fetch_veps")
    index_cache_minutes = state.get("index_cache_minutes", 60)
    indexed_context = create_indexed_context(cache_max_age_minutes=index_cache_minutes)

    current_release = state.get("current_release") or indexed_context.get("current_release", DEFAULT_RELEASE)

    # Get source data
    board_veps = indexed_context.get("board_veps", {})  # Dict[issue_number -> board_data]
    issues_index = indexed_context.get("issues_index", [])
    vep_files_index = indexed_context.get("vep_files_index", [])

    # Build lookups
    issues_by_number = {issue.get("number"): issue for issue in issues_index if issue.get("number")}
    vep_files_by_number = {}
    for vep_file in vep_files_index:
        vep_num = vep_file.get("vep_number")
        if vep_num:
            # Extract number from vep-0176 -> 176
            match = re.search(r'(\d+)', vep_num)
            if match:
                vep_files_by_number[int(match.group(1))] = vep_file

    # Build kubevirt PR lookup for enriching implementation PRs with merge status
    prs_index = indexed_context.get("prs_index", [])
    kubevirt_prs_by_number = {}
    for pr in prs_index:
        if isinstance(pr, dict) and pr.get("number"):
            kubevirt_prs_by_number[pr["number"]] = pr

    log(f"Indexed context loaded: {len(board_veps)} board VEPs, {len(issues_by_number)} issues, {len(vep_files_by_number)} VEP files, {len(kubevirt_prs_by_number)} kubevirt PRs", node="fetch_veps")

    # Create VEPInfo objects from board VEPs
    discovered_veps = []
    for issue_number, board_vep in board_veps.items():
        vep_info = _create_vep_from_board_item(
            issue_number,
            board_vep,
            issues_by_number,
            vep_files_by_number,
            current_release
        )
        if vep_info:
            # Enrich implementation PRs with actual state from kubevirt prs_index
            for pr in vep_info.implementation_prs:
                pr_data = kubevirt_prs_by_number.get(pr.number)
                if pr_data:
                    pr.state = "merged" if pr_data.get("merged") else pr_data.get("state", pr.state)
                    pr.title = pr_data.get("title", pr.title)
                    pr.author = pr_data.get("author") or pr.author
                    if pr_data.get("url"):
                        pr.url = pr_data.get("html_url") or pr_data.get("url") or pr.url
                    if pr_data.get("created_at"):
                        try:
                            pr.created_at = datetime.fromisoformat(pr_data["created_at"].replace('Z', '+00:00'))
                        except (ValueError, AttributeError):
                            pass
                    if pr_data.get("updated_at"):
                        try:
                            pr.updated_at = datetime.fromisoformat(pr_data["updated_at"].replace('Z', '+00:00'))
                        except (ValueError, AttributeError):
                            pass

            # Also add implementation PRs discovered via vep_issue_number in prs_index
            existing_pr_numbers = {pr.number for pr in vep_info.implementation_prs}
            for pr_data in prs_index:
                if isinstance(pr_data, dict) and pr_data.get("vep_issue_number") == issue_number:
                    pr_num = pr_data.get("number")
                    pr_url = pr_data.get("html_url") or pr_data.get("url", "")
                    # Skip enhancements PRs (they are proposal PRs, not implementation PRs)
                    if "enhancements" in pr_url:
                        continue
                    if pr_num and pr_num not in existing_pr_numbers:
                        existing_pr_numbers.add(pr_num)
                        now = datetime.now(timezone.utc)
                        created = now
                        updated = now
                        try:
                            if pr_data.get("created_at"):
                                created = datetime.fromisoformat(pr_data["created_at"].replace('Z', '+00:00'))
                            if pr_data.get("updated_at"):
                                updated = datetime.fromisoformat(pr_data["updated_at"].replace('Z', '+00:00'))
                        except (ValueError, AttributeError):
                            pass
                        pr = PRInfo(
                            number=pr_num,
                            title=pr_data.get("title", f"PR #{pr_num}"),
                            url=pr_url or f"https://github.com/kubevirt/kubevirt/pull/{pr_num}",
                            state="merged" if pr_data.get("merged") else pr_data.get("state", "unknown"),
                            created_at=created,
                            updated_at=updated,
                            author=pr_data.get("author") or "unknown",
                        )
                        vep_info.implementation_prs.append(pr)

            # Also add implementation PRs from vep_to_pr_mappings (maps PRs by "vep-62" patterns in title/body)
            vep_to_pr_mappings = indexed_context.get("vep_to_pr_mappings", {})
            vep_key = str(issue_number)
            for pr_data in vep_to_pr_mappings.get(vep_key, []):
                pr_num = pr_data.get("number")
                pr_url = pr_data.get("url", "")
                if "enhancements" in pr_url:
                    continue
                if pr_num and pr_num not in existing_pr_numbers:
                    existing_pr_numbers.add(pr_num)
                    now = datetime.now(timezone.utc)
                    created = now
                    updated = now
                    try:
                        if pr_data.get("updated_at"):
                            updated = datetime.fromisoformat(pr_data["updated_at"].replace('Z', '+00:00'))
                    except (ValueError, AttributeError):
                        pass
                    pr = PRInfo(
                        number=pr_num,
                        title=pr_data.get("title", f"PR #{pr_num}"),
                        url=pr_url or f"https://github.com/kubevirt/kubevirt/pull/{pr_num}",
                        state="merged" if pr_data.get("merged") else pr_data.get("state", "unknown"),
                        created_at=created,
                        updated_at=updated,
                        author="unknown",
                    )
                    vep_info.implementation_prs.append(pr)

            # Update all_code_prs_merged based on enriched PR states
            if vep_info.implementation_prs:
                all_merged = all(pr.state == "merged" for pr in vep_info.implementation_prs)
                vep_info.current_milestone.all_code_prs_merged = all_merged
                merged_count = sum(1 for pr in vep_info.implementation_prs if pr.state == "merged")
                total_count = len(vep_info.implementation_prs)
                if merged_count > 0:
                    log(f"  VEP #{issue_number} ({vep_info.name}): {merged_count}/{total_count} impl PRs merged", node="fetch_veps", level="DEBUG")

            discovered_veps.append(vep_info)

    # Log summary
    log("="*80, node="fetch_veps")
    log("VEP DISCOVERY SUMMARY", node="fetch_veps")
    log("="*80, node="fetch_veps")
    log(f"Total VEPs discovered: {len(discovered_veps)}", node="fetch_veps")
    log(f"  - Source: {len(board_veps)} VEPs from project board", node="fetch_veps")
    log(f"  - Enriched with: {len(issues_by_number)} issues, {len(vep_files_by_number)} VEP files", node="fetch_veps")

    if discovered_veps:
        # Status breakdown
        open_count = sum(1 for vep in discovered_veps if vep.status == "open")
        closed_count = len(discovered_veps) - open_count
        log(f"  - Status: {open_count} open, {closed_count} closed", node="fetch_veps")

        # SIG breakdown
        sig_counts = {}
        for vep in discovered_veps:
            sig_counts[vep.owning_sig] = sig_counts.get(vep.owning_sig, 0) + 1
        sig_breakdown = ", ".join([f"{sig}: {count}" for sig, count in sorted(sig_counts.items())])
        log(f"  - SIG breakdown: {sig_breakdown}", node="fetch_veps")

        # Discovered VEP list
        vep_names = sorted([vep.name for vep in discovered_veps])
        log(f"  - Discovered VEPs: {', '.join(vep_names)}", node="fetch_veps")

    log("="*80, node="fetch_veps")

    return {
        "veps": discovered_veps,
        "last_check_times": last_check_times,
        "next_tasks": next_tasks,
        "sheets_need_update": True,
    }
