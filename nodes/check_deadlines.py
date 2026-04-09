"""Deadline context node - computes deadline-related data for VEPs.

Deterministic node using indexed context (no LLM). Computes days until
Enhancement Freeze and Code Freeze, and populates release_schedule state.
"""

from datetime import datetime, timezone
from typing import Any
from state import VEPState, ReleaseSchedule
from services.utils import log
from services.indexer import create_indexed_context
from nodes._check_helpers import parse_iso_date


def check_deadlines_node(state: VEPState) -> Any:
    """Compute deadline context for VEPs from indexed data.

    Populates release_schedule and current_release in state, and stores
    per-VEP deadline data in vep_updates_by_check for merge node.
    """
    veps = state.get("veps", [])
    log(f"Computing deadline context for {len(veps)} VEP(s)", node="check_deadlines")

    last_check_times = state.get("last_check_times", {})
    last_check_times["check_deadlines"] = datetime.now()

    if not veps:
        return {"last_check_times": last_check_times}

    index_cache_minutes = state.get("index_cache_minutes", 60)
    indexed_context = create_indexed_context(cache_max_age_minutes=index_cache_minutes)

    release_deadlines = indexed_context.get("release_deadlines", {})
    release_info = indexed_context.get("release_info", {})
    enhancements_prs = indexed_context.get("enhancements_prs", [])

    ef_date = parse_iso_date(release_deadlines.get("enhancement_freeze"))
    cf_date = parse_iso_date(release_deadlines.get("code_freeze"))
    ga_date = parse_iso_date(release_deadlines.get("ga"))
    current_release = release_info.get("current_release") or state.get("current_release")

    if not ef_date or not cf_date:
        log("Missing EF or CF date, returning minimal state", node="check_deadlines", level="WARNING")
        vep_updates_by_check = state.get("vep_updates_by_check", {})
        vep_updates_by_check["check_deadlines"] = {"context_field": "deadline", "updates": {}}
        return {"last_check_times": last_check_times, "vep_updates_by_check": vep_updates_by_check}

    # Build ReleaseSchedule for state
    release_schedule = ReleaseSchedule(
        version=current_release or "",
        enhancement_freeze=ef_date,
        code_freeze=cf_date,
        kubevirt_release=ga_date or cf_date,
        freeze_delays=[],
    )

    today = datetime.now(timezone.utc)
    days_until_ef = (ef_date - today).days
    days_until_cf = (cf_date - today).days

    context_by_id = {}
    for vep in veps:
        # VEP merge status from enhancements PRs
        vep_pr = next(
            (pr for pr in enhancements_prs
             if pr.get("vep_issue_number") == vep.tracking_issue_id),
            None,
        )
        vep_merged = vep_pr.get("merged", False) if vep_pr else getattr(vep.compliance, "vep_merged", False)

        context_by_id[vep.tracking_issue_id] = {
            "days_until_ef": days_until_ef,
            "days_until_cf": days_until_cf,
            "ef_passed": days_until_ef < 0,
            "cf_passed": days_until_cf < 0,
            "vep_merged": vep_merged,
            "target_release": vep.target_release or current_release,
        }

    vep_updates_by_check = state.get("vep_updates_by_check", {})
    vep_updates_by_check["check_deadlines"] = {
        "context_field": "deadline",
        "updates": context_by_id,
    }

    log(f"Computed deadline context for {len(context_by_id)} VEP(s)", node="check_deadlines")

    return {
        "last_check_times": last_check_times,
        "current_release": current_release,
        "release_schedule": release_schedule,
        "vep_updates_by_check": vep_updates_by_check,
    }
