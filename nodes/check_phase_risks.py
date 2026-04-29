"""Phase-specific risk detection node - monitors risks based on release phase.

This node detects different risks depending on the current release phase:
- Design phase (pre-EF): Stale/under-reviewed proposal PRs in enhancements repo
- Development phase (EF-CF): VEPs with missing/stale implementation PRs in kubevirt repo

Uses indexed_context (release_phase, release_deadlines, board_veps) for efficient detection.
"""

from datetime import datetime, timezone, timedelta
from typing import Any, Dict
from state import VEPState
from services.utils import log
from services.indexer import create_indexed_context


# Configurable thresholds
DESIGN_PHASE_THRESHOLDS = {
    "stale_days": 7,  # Days without updates to consider stale
    "min_reviews": 2,  # Minimum reviews for proposal PR
    "deadline_extension_days": 14,  # Grace period after EF
}

DEVELOPMENT_PHASE_THRESHOLDS = {
    "stale_days": 5,  # Days without updates to consider stale
    "min_reviews": 1,  # Minimum reviews for impl PR
    "deadline_extension_days": 7,  # Grace period after CF
}


def check_phase_risks_node(state: VEPState) -> Any:
    """Detect phase-specific risks for VEPs.

    Design phase: Flags stale/under-reviewed proposal PRs
    Development phase: Flags missing/stale implementation PRs

    Stores risk data in vep_updates_by_check for merge node.
    """
    log("Checking phase-specific risks", node="check_phase_risks")

    last_check_times = state.get("last_check_times", {})
    last_check_times["check_phase_risks"] = datetime.now()

    # Get indexed context (cached, efficient)
    index_cache_minutes = state.get("index_cache_minutes", 60)
    indexed_context = create_indexed_context(cache_max_age_minutes=index_cache_minutes)

    release_phase = indexed_context.get("release_phase", "unknown")
    release_deadlines = indexed_context.get("release_deadlines", {})
    board_veps = indexed_context.get("board_veps", {})

    log(f"Release phase: {release_phase}, Board VEPs: {len(board_veps)}", node="check_phase_risks")

    risks_by_vep = {}
    risk_type = "unknown"

    if release_phase == "design":
        # Design phase: check proposal PRs in enhancements repo
        risks_by_vep = _check_design_phase_risks(
            indexed_context, release_deadlines, board_veps
        )
        risk_type = "design"
    elif release_phase == "development":
        # Development phase: check implementation PRs in kubevirt repo
        risks_by_vep = _check_development_phase_risks(
            indexed_context, release_deadlines, board_veps
        )
        risk_type = "development"
    else:
        log(f"Phase '{release_phase}' has no specific risk checks", node="check_phase_risks")

    # Store in vep_updates_by_check format for merge node
    vep_updates_by_check = state.get("vep_updates_by_check", {})
    vep_updates_by_check["check_phase_risks"] = {
        "context_field": "phase_risks",
        "updates": risks_by_vep
    }

    risk_count = sum(1 for risks in risks_by_vep.values() if risks.get("has_risks"))
    if risk_count > 0:
        log(f"Detected {risk_count} {risk_type} phase risk(s)", node="check_phase_risks")
    else:
        log(f"No {risk_type} phase risks detected", node="check_phase_risks")

    return {
        "last_check_times": last_check_times,
        "vep_updates_by_check": vep_updates_by_check,
    }


def _check_design_phase_risks(
    indexed_context: Dict[str, Any],
    release_deadlines: Dict[str, Any],
    board_veps: Dict[int, Dict[str, Any]]
) -> Dict[int, Dict[str, Any]]:
    """Check design phase risks: stale/under-reviewed proposal PRs.

    Returns:
        Dict mapping VEP issue number -> risk data
    """
    # Get enhancement freeze deadline (with extension)
    ef_deadline_str = release_deadlines.get("enhancement_freeze")
    if not ef_deadline_str:
        log("No EF deadline found, skipping design phase checks", node="check_phase_risks", level="WARNING")
        return {}

    ef_deadline = datetime.fromisoformat(ef_deadline_str.replace('Z', '+00:00'))
    # Ensure timezone-aware
    if ef_deadline.tzinfo is None:
        ef_deadline = ef_deadline.replace(tzinfo=timezone.utc)
    ef_with_extension = ef_deadline + timedelta(days=DESIGN_PHASE_THRESHOLDS["deadline_extension_days"])
    now = datetime.now(timezone.utc)
    days_to_ef = (ef_with_extension - now).days

    log(f"EF deadline (with extension): {ef_with_extension.date()}, days remaining: {days_to_ef}", node="check_phase_risks")

    # Get enhancements PRs from indexed context
    enhancements_prs = indexed_context.get("enhancements_prs", [])

    if not enhancements_prs:
        log("No enhancements PRs indexed", node="check_phase_risks", level="DEBUG")
        return {}

    # Filter to PRs that reference VEPs on the board (more precise)
    # Board keys may be str (from JSON cache) or int — normalize to int
    board_vep_numbers = set()
    for k in board_veps.keys():
        try:
            board_vep_numbers.add(int(k))
        except (ValueError, TypeError):
            board_vep_numbers.add(k)
    filtered_prs = []
    for pr in enhancements_prs:
        # MCP tools may return state as "OPEN" (GraphQL) or "open" (REST)
        if (pr.get("state") or "").lower() != "open":
            continue
        vep_issue_num = pr.get("vep_issue_number")
        if vep_issue_num and vep_issue_num in board_vep_numbers:
            filtered_prs.append(pr)

    log(f"Enhancement PRs: {len(enhancements_prs)} total, filtering to open PRs linked to {len(board_vep_numbers)} board VEPs", node="check_phase_risks", level="DEBUG")
    log(f"Checking {len(filtered_prs)} open proposal PRs linked to board VEPs", node="check_phase_risks", level="DEBUG")

    risks_by_vep = {}

    # Check each PR for staleness and review status
    for pr in filtered_prs:
        vep_issue_num = pr.get("vep_issue_number")
        # Board keys may be str (from JSON cache) — try both int and str
        board_vep = board_veps.get(vep_issue_num) or board_veps.get(str(vep_issue_num))
        if not board_vep:
            continue
        status = board_vep.get("fields", {}).get("Status", "")

        # Only check tracked VEPs
        if status not in ["Tracked", "At risk"]:
            continue

        # Check staleness
        updated_at_str = pr.get("updated_at")
        if updated_at_str:
            updated_at = datetime.fromisoformat(updated_at_str.replace('Z', '+00:00'))
            days_since_update = (now - updated_at).days
        else:
            days_since_update = 999  # Unknown, assume stale

        is_stale = days_since_update > DESIGN_PHASE_THRESHOLDS["stale_days"]

        # Check review count
        review_count = pr.get("review_count") or 0
        low_reviews = review_count < DESIGN_PHASE_THRESHOLDS["min_reviews"]

        # Check proximity to deadline
        near_deadline = days_to_ef <= DESIGN_PHASE_THRESHOLDS["deadline_extension_days"]

        # Flag if stale AND (low reviews OR near deadline)
        has_risks = is_stale and (low_reviews or near_deadline)

        if has_risks:
            risks_by_vep[vep_issue_num] = {
                "has_risks": True,
                "phase": "design",
                "proposal_pr": {
                    "number": pr.get("number"),
                    "url": pr.get("url", pr.get("html_url")),
                    "title": pr.get("title"),
                    "days_since_update": days_since_update,
                    "review_count": review_count,
                    "is_stale": is_stale,
                    "low_reviews": low_reviews,
                },
                "days_to_deadline": days_to_ef,
                "days_to_ef": days_to_ef,
                "near_deadline": near_deadline,
                "risk_level": "high" if (near_deadline and low_reviews) else "medium",
            }

            log(f"Design risk: VEP {vep_issue_num} - stale proposal PR #{pr.get('number')} ({days_since_update}d, {review_count} reviews)", node="check_phase_risks")

    return risks_by_vep


def _check_development_phase_risks(
    indexed_context: Dict[str, Any],
    release_deadlines: Dict[str, Any],
    board_veps: Dict[int, Dict[str, Any]]
) -> Dict[int, Dict[str, Any]]:
    """Check development phase risks: missing/stale implementation PRs.

    Returns:
        Dict mapping VEP issue number -> risk data
    """
    # Get code freeze deadline (with extension)
    cf_deadline_str = release_deadlines.get("code_freeze")
    if not cf_deadline_str:
        log("No CF deadline found, skipping development phase checks", node="check_phase_risks", level="WARNING")
        return {}

    cf_deadline = datetime.fromisoformat(cf_deadline_str.replace('Z', '+00:00'))
    # Ensure timezone-aware
    if cf_deadline.tzinfo is None:
        cf_deadline = cf_deadline.replace(tzinfo=timezone.utc)
    cf_with_extension = cf_deadline + timedelta(days=DEVELOPMENT_PHASE_THRESHOLDS["deadline_extension_days"])
    now = datetime.now(timezone.utc)
    days_to_cf = (cf_with_extension - now).days

    log(f"CF deadline (with extension): {cf_with_extension.date()}, days remaining: {days_to_cf}", node="check_phase_risks")

    # Get kubevirt PRs and VEP-to-PR mappings
    prs_index = indexed_context.get("prs_index", [])
    vep_to_pr_mappings = indexed_context.get("vep_to_pr_mappings", {})

    risks_by_vep = {}
    prs_by_number = {pr.get("number"): pr for pr in prs_index if isinstance(pr, dict)}

    # Check each board VEP for implementation status
    for vep_issue_num, board_vep in board_veps.items():
        status = board_vep.get("fields", {}).get("Status", "")

        # Only check tracked VEPs
        if status not in ["Tracked", "At risk"]:
            continue

        # Collect implementation PRs from all sources
        impl_pr_numbers = set()
        impl_prs = []
        for pr in board_vep.get("impl_prs", []):
            pr_num = pr.get("number")
            if pr_num and pr_num not in impl_pr_numbers and pr_num in prs_by_number:
                base_ref = prs_by_number[pr_num].get("base_ref")
                if base_ref and base_ref not in ("main", "master"):
                    continue
                impl_pr_numbers.add(pr_num)
                impl_prs.append(pr)

        # Source 2: VEP-to-PR mappings (PRs with "vep-N" in title/body)
        vep_num_str = str(vep_issue_num)
        for pr in vep_to_pr_mappings.get(vep_num_str, []):
            pr_num = pr.get("number")
            base_ref = pr.get("base_ref")
            if not base_ref and pr_num:
                idx = prs_by_number.get(pr_num)
                if idx:
                    base_ref = idx.get("base_ref")
            if base_ref and base_ref not in ("main", "master"):
                continue
            if pr_num and pr_num not in impl_pr_numbers:
                impl_pr_numbers.add(pr_num)
                impl_prs.append(pr)

        # Source 3: prs_index by vep_issue_number (PRs referencing tracking issue)
        for pr in prs_index:
            if isinstance(pr, dict) and pr.get("vep_issue_number") == vep_issue_num:
                pr_num = pr.get("number")
                pr_url = pr.get("html_url") or pr.get("url", "")
                if "enhancements" in pr_url:
                    continue
                base_ref = pr.get("base_ref")
                if base_ref and base_ref not in ("main", "master"):
                    continue
                if pr_num and pr_num not in impl_pr_numbers:
                    impl_pr_numbers.add(pr_num)
                    impl_prs.append({"number": pr_num, "url": pr_url})

        # Flag if no implementation PRs
        if not impl_prs:
            risks_by_vep[vep_issue_num] = {
                "has_risks": True,
                "phase": "development",
                "missing_impl_prs": True,
                "days_to_deadline": days_to_cf,
                "days_to_cf": days_to_cf,
                "near_deadline": days_to_cf <= DEVELOPMENT_PHASE_THRESHOLDS["deadline_extension_days"],
                "risk_level": "high" if days_to_cf <= 7 else "medium",
            }
            log(f"Development risk: VEP {vep_issue_num} - no implementation PRs", node="check_phase_risks")
            continue

        # Check if all implementation PRs are merged or closed — no risk
        # A PR is only considered "still open" if we have data confirming state="open"
        # PRs not in prs_index (unknown state) are likely old merged PRs outside pagination
        has_open_pr = False
        confirmed_done = 0
        for impl_pr in impl_prs:
            pr_num = impl_pr.get("number")
            pr_data = next((pr for pr in prs_index if pr.get("number") == pr_num), None)
            if not pr_data:
                continue  # Not in index = unknown, don't treat as open
            pr_state = (pr_data.get("state") or "").lower()
            if pr_state in ("merged", "closed") or pr_data.get("merged"):
                confirmed_done += 1
                continue
            # PR is confirmed open
            has_open_pr = True
        if not has_open_pr and confirmed_done > 0:
            log(f"VEP {vep_issue_num}: no open impl PRs ({confirmed_done} confirmed done, {len(impl_prs)} total), no risk", node="check_phase_risks", level="DEBUG")
            continue

        # Check staleness of implementation PRs
        stale_prs = []
        for impl_pr in impl_prs:
            pr_num = impl_pr.get("number")

            # Find full PR data
            pr_data = next((pr for pr in prs_index if pr.get("number") == pr_num), None)
            if not pr_data:
                continue

            # Skip if merged or closed
            if (pr_data.get("state") or "").lower() in ("merged", "closed"):
                continue

            # Check staleness
            updated_at_str = pr_data.get("updated_at")
            if updated_at_str:
                updated_at = datetime.fromisoformat(updated_at_str.replace('Z', '+00:00'))
                days_since_update = (now - updated_at).days
            else:
                days_since_update = 999

            is_stale = days_since_update > DEVELOPMENT_PHASE_THRESHOLDS["stale_days"]

            # Check review count
            review_count = pr_data.get("review_count") or 0
            low_reviews = review_count < DEVELOPMENT_PHASE_THRESHOLDS["min_reviews"]

            if is_stale and low_reviews:
                stale_prs.append({
                    "number": pr_num,
                    "url": impl_pr.get("url"),
                    "days_since_update": days_since_update,
                    "review_count": review_count,
                })

        if stale_prs:
            near_deadline = days_to_cf <= DEVELOPMENT_PHASE_THRESHOLDS["deadline_extension_days"]
            risks_by_vep[vep_issue_num] = {
                "has_risks": True,
                "phase": "development",
                "stale_impl_prs": stale_prs,
                "days_to_deadline": days_to_cf,
                "days_to_cf": days_to_cf,
                "near_deadline": near_deadline,
                "risk_level": "high" if near_deadline else "medium",
            }
            log(f"Development risk: VEP {vep_issue_num} - {len(stale_prs)} stale impl PR(s)", node="check_phase_risks")

    return risks_by_vep
