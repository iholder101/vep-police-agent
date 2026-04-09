"""Shared helpers for deterministic check nodes."""

import re
from datetime import datetime, timezone
from typing import Optional


def get_board_vep(board_veps: dict, vep_id: int) -> dict:
    """Look up board VEP handling int/str key mismatch from JSON cache."""
    return board_veps.get(vep_id) or board_veps.get(str(vep_id)) or {}


def extract_vep_num(name: str) -> Optional[int]:
    """Extract numeric part from VEP identifier (e.g., 'vep-0176' -> 176)."""
    m = re.search(r'(\d+)', name or "")
    return int(m.group(1)) if m else None


def parse_iso_date(s: Optional[str]) -> Optional[datetime]:
    """Parse ISO date string with None guard and Z handling.

    Always returns timezone-aware UTC datetime or None.
    """
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace('Z', '+00:00'))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def collect_impl_prs(
    tracking_issue_id: int,
    board_veps: dict,
    vep_to_pr_mappings: dict,
    prs_index: list,
) -> list:
    """Collect and deduplicate implementation PRs from all indexed sources.

    Sources: board data, vep_to_pr_mappings, prs_index by vep_issue_number.
    Returns list of {number, state, repo}.
    """
    seen = set()
    impl_prs = []

    # Source 1: Board data
    board_vep = get_board_vep(board_veps, tracking_issue_id)
    for pr in board_vep.get("impl_prs", []):
        pr_num = pr.get("number")
        if pr_num and pr_num not in seen:
            seen.add(pr_num)
            impl_prs.append({
                "number": pr_num,
                "state": pr.get("state", "unknown"),
                "repo": pr.get("repo", "kubevirt/kubevirt"),
            })

    # Source 2: VEP-to-PR mappings
    for pr in vep_to_pr_mappings.get(str(tracking_issue_id), []):
        pr_num = pr.get("number")
        if pr_num and pr_num not in seen:
            seen.add(pr_num)
            impl_prs.append({
                "number": pr_num,
                "state": pr.get("state", "unknown"),
                "repo": pr.get("repo", "kubevirt/kubevirt"),
            })

    # Source 3: prs_index (kubevirt PRs referencing this tracking issue)
    for pr in prs_index:
        if not isinstance(pr, dict):
            continue
        if pr.get("vep_issue_number") != tracking_issue_id:
            continue
        pr_num = pr.get("number")
        pr_url = pr.get("html_url") or pr.get("url", "")
        if "enhancements" in pr_url:
            continue
        if pr_num and pr_num not in seen:
            seen.add(pr_num)
            # Enrich state from prs_index data
            state = pr.get("state", "unknown")
            if pr.get("merged"):
                state = "merged"
            impl_prs.append({
                "number": pr_num,
                "state": state,
                "repo": "kubevirt/kubevirt",
            })

    return impl_prs
