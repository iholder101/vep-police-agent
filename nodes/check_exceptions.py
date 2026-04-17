"""Exception context node - computes exception-related data for VEPs.

Deterministic node using indexed context (no LLM). Discovers exception
issues, maps them to VEPs, and detects post-freeze activity.
"""

import re
from datetime import datetime
from typing import Any
from state import VEPState
from services.utils import log
from services.indexer import create_indexed_context
from nodes._check_helpers import get_board_vep, collect_impl_prs, parse_iso_date


EXCEPTION_PATTERNS = ["exception", "exemption", "freeze extension", "post-freeze"]


def check_exceptions_node(state: VEPState) -> Any:
    """Compute exception context for VEPs from indexed data.

    Discovers exception issues from issues_index, maps them to VEPs,
    checks board exception phase, and detects post-freeze PR activity.
    """
    veps = state.get("veps", [])
    log(f"Computing exception context for {len(veps)} VEP(s)", node="check_exceptions")

    last_check_times = state.get("last_check_times", {})
    last_check_times["check_exceptions"] = datetime.now()

    if not veps:
        return {"last_check_times": last_check_times}

    index_cache_minutes = state.get("index_cache_minutes", 60)
    indexed_context = create_indexed_context(cache_max_age_minutes=index_cache_minutes)

    issues_index = indexed_context.get("issues_index", [])
    release_deadlines = indexed_context.get("release_deadlines", {})
    prs_index = indexed_context.get("prs_index", [])
    vep_to_pr_mappings = indexed_context.get("vep_to_pr_mappings", {})
    board_veps = indexed_context.get("board_veps", {})

    # Phase 1: Discover exception issues
    exception_issues = []
    for issue in issues_index:
        labels_lower = [l.lower() for l in issue.get("labels", [])]
        title_lower = issue.get("title", "").lower()
        body_text = (issue.get("body") or issue.get("body_preview") or "")[:500].lower()

        if ("exception" in labels_lower
                or any(pat in title_lower for pat in EXCEPTION_PATTERNS)
                or any(pat in body_text for pat in EXCEPTION_PATTERNS)):
            exception_issues.append(issue)

    log(f"Found {len(exception_issues)} exception-related issue(s)", node="check_exceptions")

    # Phase 2: Map exceptions to VEPs via body references
    vep_to_exception = {}
    for exc_issue in exception_issues:
        body = exc_issue.get("body") or exc_issue.get("body_preview") or ""
        refs = re.findall(
            r'(?:vep[-\s#]*|tracking\s+issue\s*#?)(\d+)', body, re.IGNORECASE
        )
        for ref in refs:
            vep_to_exception[int(ref)] = exc_issue

    # Phase 3: Per-VEP context
    ef_date = parse_iso_date(release_deadlines.get("enhancement_freeze"))
    cf_date = parse_iso_date(release_deadlines.get("code_freeze"))

    context_by_id = {}
    for vep in veps:
        exc_issue = vep_to_exception.get(vep.tracking_issue_id)

        # Board exception phase (authoritative signal)
        board_vep = get_board_vep(board_veps, vep.tracking_issue_id)
        exception_phase = board_vep.get("fields", {}).get("Exception Phase", "None")

        # Post-freeze activity (PR updated_at as proxy for activity)
        has_post_ef_commits = False
        has_post_cf_commits = False
        post_freeze_pr_numbers = []

        if ef_date or cf_date:
            impl_prs = collect_impl_prs(
                vep.tracking_issue_id, board_veps, vep_to_pr_mappings, prs_index
            )
            seen = set()
            for impl_pr in impl_prs:
                pr_data = next(
                    (p for p in prs_index if p.get("number") == impl_pr.get("number")),
                    None,
                )
                if not pr_data or not pr_data.get("updated_at"):
                    continue
                updated_at = parse_iso_date(pr_data["updated_at"])
                if not updated_at:
                    continue
                if ef_date and updated_at > ef_date:
                    has_post_ef_commits = True
                    if pr_data["number"] not in seen:
                        post_freeze_pr_numbers.append(pr_data["number"])
                        seen.add(pr_data["number"])
                if cf_date and updated_at > cf_date:
                    has_post_cf_commits = True

        context_by_id[vep.tracking_issue_id] = {
            "exception_issue_number": exc_issue.get("number") if exc_issue else None,
            "exception_issue_state": exc_issue.get("state") if exc_issue else None,
            "exception_labels": exc_issue.get("labels", []) if exc_issue else [],
            "exception_phase": exception_phase,
            "has_post_ef_commits": has_post_ef_commits,
            "has_post_cf_commits": has_post_cf_commits,
            "post_freeze_pr_numbers": post_freeze_pr_numbers,
        }

    vep_updates_by_check = state.get("vep_updates_by_check", {})
    vep_updates_by_check["check_exceptions"] = {
        "context_field": "exceptions",
        "updates": context_by_id,
    }

    log(f"Computed exception context for {len(context_by_id)} VEP(s)", node="check_exceptions")

    return {
        "last_check_times": last_check_times,
        "vep_updates_by_check": vep_updates_by_check,
    }
