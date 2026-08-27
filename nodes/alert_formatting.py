"""Alert formatting helpers for rich phase-aware VEP summary tables.

Provides utilities to build per-VEP summary tables with:
- VEP #, Title
- Proposal PR(s) and Impl PR(s) links
- Urgency indicators (RED/YELLOW/GREEN)
- Status comments from LLM risk assessment
"""

import re
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any

from services.attribution import parse_enumerated_impl_prs, resolve_impl_owner
from services.indexer import create_indexed_context
from services.utils import log


def get_urgency_level(vep: Any) -> tuple[str, str]:
    """Determine urgency level and color for a VEP.

    Returns:
        (urgency_text, color) tuple
        - RED: probability <50% or blocked
        - YELLOW: probability 50-80% or concerned or escalation recommended
        - GREEN: >80% and positive/neutral
    """
    if not hasattr(vep, 'analysis') or not vep.analysis:
        return ("UNKNOWN", "gray")

    risk_assessment = vep.analysis.get("risk_assessment")
    if not risk_assessment:
        return ("OK", "green")

    prob = risk_assessment.get("merge_probability", 100)
    sentiment = risk_assessment.get("reviewer_sentiment", "neutral")
    recommend_escalation = risk_assessment.get("recommend_escalation", False)

    # RED: High risk - low probability or blocked sentiment
    if prob < 50 or sentiment == "blocked":
        return ("RED", "red")

    # YELLOW: Medium risk - moderate probability, concern, or escalation recommended
    if prob < 80 or sentiment == "concerned" or recommend_escalation:
        return ("YELLOW", "orange")

    # GREEN: Low risk
    return ("GREEN", "green")


def build_vep_summary_table(
    veps: list[Any],
    indexed_context: dict[str, Any] | None = None,
    pr_metadata_fetcher: Callable[[list[int]], dict[int, dict[str, Any]]] | None = None,
) -> list[dict[str, Any]]:
    """Build per-VEP summary table data.

    Args:
        veps: List of VEPInfo objects
        indexed_context: Optional indexed context (will be fetched if not provided)
        pr_metadata_fetcher: Optional override for fetching real metadata (state,
            merged_at, base_ref) of impl PRs enumerated in a tracking issue but
            absent from prs_index ("widened" PRs). Defaults to
            services.indexer.fetch_pr_metadata_by_number. Injectable for tests.

    Returns:
        List of dicts with table row data:
        {
            "vep_number": int,
            "vep_name": str,
            "title": str,
            "proposal_prs": [{"number": int, "url": str}],
            "impl_prs": [{"number": int, "url": str}],
            "urgency": str (RED/YELLOW/GREEN),
            "urgency_color": str,
            "status_comment": str (from risk_assessment reasoning),
            "escalate": bool
        }
    """
    if indexed_context is None:
        indexed_context = create_indexed_context(cache_max_age_minutes=60)

    enhancements_prs = indexed_context.get("enhancements_prs", [])
    enhancements_by_number = {pr.get("number"): pr for pr in enhancements_prs}
    issues_index = indexed_context.get("issues_index", [])
    kubevirt_prs = indexed_context.get("prs_index", [])

    # Build base_ref lookup from prs_index for filtering backport impl PRs
    prs_by_number = {}
    for pr in kubevirt_prs:
        if isinstance(pr, dict) and pr.get("number"):
            prs_by_number[pr["number"]] = pr

    # Build a lookup from issue number to issue data
    issues_by_number = {}
    for issue in issues_index:
        issue_num = issue.get("number")
        if issue_num:
            issues_by_number[issue_num] = issue

    # ---- Precompute implementation-PR ownership (one PR -> one VEP) ----
    # Attribution is reference-based, never title-based. Two signals per PR:
    # the PR's self-declared tracking issue and the tracking issue's own
    # enumerated PR list; resolve_impl_owner reconciles them (a reciprocated
    # link wins on conflict). Computed once so each PR lands on a single VEP.
    in_scope_issues = {v.tracking_issue_id for v in veps}
    enumerated_by_pr: dict[int, set[int]] = {}
    for vep in veps:
        issue = issues_by_number.get(vep.tracking_issue_id)
        body = (issue.get("body") or issue.get("body_preview") or "") if issue else ""
        for pr_num in parse_enumerated_impl_prs(body):
            enumerated_by_pr.setdefault(pr_num, set()).add(vep.tracking_issue_id)

    impl_by_vep: dict[int, list[dict[str, Any]]] = {}
    for pr in kubevirt_prs:
        if not isinstance(pr, dict):
            continue
        pr_num = pr.get("number")
        if not pr_num:
            continue
        pr_url = pr.get("url") or pr.get("html_url") or f"https://github.com/kubevirt/kubevirt/pull/{pr_num}"
        if "enhancements" in pr_url:
            continue  # proposal PR, never an implementation PR
        self_ref = pr.get("vep_issue_number")
        if self_ref not in in_scope_issues:
            self_ref = None
        enumerating = enumerated_by_pr.get(pr_num, set()) & in_scope_issues
        owner, conflict = resolve_impl_owner(self_ref, enumerating)
        if owner is None:
            continue
        if conflict:
            log(
                f"Impl PR #{pr_num}: attribution conflict "
                f"(self-ref {pr.get('vep_issue_number')}, enumerated by {sorted(enumerating)}) "
                f"-> assigned VEP #{owner}",
                node="alert_formatting",
            )
        impl_by_vep.setdefault(owner, []).append({
            "number": pr_num,
            "url": pr_url,
            "_state": pr.get("state"),
            "_merged_at": pr.get("merged_at"),
        })

    # Widen: attribute issue-enumerated impl PRs that never appeared in
    # prs_index (e.g. old PRs beyond the ~1200-PR fetch window). They carry no
    # self-ref, so resolve_impl_owner keys purely on the enumerating issue
    # (unambiguous single issue -> that VEP). Real metadata is fetched below so
    # they still go through the same cycle-scoping filters as indexed PRs.
    already_placed = {p["number"] for prs in impl_by_vep.values() for p in prs}
    widened: list[tuple[int, int]] = []  # (pr_num, owner)
    for pr_num, enumerating_issues in enumerated_by_pr.items():
        if pr_num in already_placed:
            continue
        enumerating = enumerating_issues & in_scope_issues
        owner, conflict = resolve_impl_owner(None, enumerating)
        if owner is None:
            continue
        if conflict:
            log(
                f"Impl PR #{pr_num}: enumerated by multiple in-scope issues "
                f"{sorted(enumerating)} and absent from prs_index -> left unlinked",
                node="alert_formatting",
            )
            continue
        widened.append((pr_num, owner))

    if widened:
        fetcher = pr_metadata_fetcher
        if fetcher is None:
            from services.indexer import fetch_pr_metadata_by_number
            fetcher = fetch_pr_metadata_by_number
        meta = fetcher(sorted({pr_num for pr_num, _ in widened}))
        for pr_num, owner in widened:
            m = meta.get(pr_num, {})
            if not m:
                log(
                    f"Impl PR #{pr_num}: could not fetch metadata for widened PR "
                    f"-> including without cycle-scoping verification",
                    node="alert_formatting",
                    level="WARNING",
                )
            impl_by_vep.setdefault(owner, []).append({
                "number": pr_num,
                "url": f"https://github.com/kubevirt/kubevirt/pull/{pr_num}",
                "_state": m.get("state"),
                "_merged_at": m.get("merged_at"),
                "_base_ref": m.get("base_ref"),
            })

    table_rows = []

    for vep in veps:
        # Proposal PRs: attribute strictly by the PR's own tracking-issue
        # reference (one PR -> one VEP). vep_issue_number is set at index time
        # by the hardened extractor; title / issue-body scanning is not used.
        proposal_pr_numbers = {
            pr.get("number")
            for pr in enhancements_prs
            if pr.get("vep_issue_number") == vep.tracking_issue_id and pr.get("number")
        }

        # Build proposal PRs list with URLs and state/merged_at for filtering
        proposal_prs = []
        for pr_num in sorted(proposal_pr_numbers):
            enh_pr = enhancements_by_number.get(pr_num, {})
            state = enh_pr.get("state", "")
            merged_at = enh_pr.get("merged_at")
            if state == "closed" and not merged_at:
                continue
            proposal_prs.append({
                "number": pr_num,
                "url": f"https://github.com/kubevirt/enhancements/pull/{pr_num}",
                "_merged_at": merged_at,
            })

        # Filter out proposal PRs merged before the current release cycle
        cycle_start_date_str = indexed_context.get("cycle_start_date")
        if cycle_start_date_str:
            cycle_start = date.fromisoformat(cycle_start_date_str)
            before_count = len(proposal_prs)
            filtered = []
            for p in proposal_prs:
                merged_at = p.get("_merged_at")
                if merged_at:
                    try:
                        if isinstance(merged_at, datetime):
                            merged_date = merged_at.date()
                        else:
                            merged_date = datetime.fromisoformat(merged_at).date()
                        if merged_date < cycle_start:
                            continue
                    except (ValueError, TypeError):
                        pass
                filtered.append(p)
            proposal_prs = filtered
            removed = before_count - len(proposal_prs)
            if removed:
                log(f"VEP {vep.name}: filtered {removed} pre-cycle proposal PR(s) (cycle start: {cycle_start_date_str})",
                    node="alert_formatting")

        # Strip internal fields from proposal PRs
        proposal_prs = [{"number": p["number"], "url": p["url"]} for p in proposal_prs]

        # Implementation PRs: from the precomputed one-PR-one-VEP ownership map.
        impl_prs = [dict(p) for p in impl_by_vep.get(vep.tracking_issue_id, [])]
        impl_pr_numbers = {p["number"] for p in impl_prs}

        # Exclude impl PRs that are actually from the enhancements repo (proposal PRs)
        # Compare by URL since the same PR number can exist in both repos
        impl_prs = [p for p in impl_prs if "enhancements" not in p.get("url", "")]

        # Sort by PR number
        impl_prs = sorted(impl_prs, key=lambda x: x["number"])

        # Filter out closed-unmerged PRs (abandoned/superseded)
        before_count = len(impl_prs)
        filtered = []
        for p in impl_prs:
            state = p.get("_state", "")
            merged_at = p.get("_merged_at")
            if not state:
                pr_index_data = prs_by_number.get(p["number"])
                if pr_index_data:
                    state = pr_index_data.get("state", "")
                    merged_at = merged_at or pr_index_data.get("merged_at")
            if state == "closed" and not merged_at:
                impl_pr_numbers.discard(p["number"])
                continue
            filtered.append(p)
        impl_prs = filtered
        removed = before_count - len(impl_prs)
        if removed:
            log(f"VEP {vep.name}: filtered {removed} closed-unmerged impl PR(s)",
                node="alert_formatting")

        # Filter out backport PRs (targeting release branches, not main)
        before_count = len(impl_prs)
        filtered = []
        for p in impl_prs:
            pr_index_data = prs_by_number.get(p["number"])
            base_ref = (pr_index_data or {}).get("base_ref") or p.get("_base_ref")
            if base_ref and base_ref not in ("main", "master"):
                continue
            filtered.append(p)
        impl_prs = filtered
        removed = before_count - len(impl_prs)
        if removed:
            log(f"VEP {vep.name}: filtered {removed} backport impl PR(s)",
                node="alert_formatting")

        # Filter out merged PRs from before the current release cycle
        cycle_start_date = indexed_context.get("cycle_start_date")
        if cycle_start_date:
            cycle_start = date.fromisoformat(cycle_start_date)
            before_count = len(impl_prs)
            filtered = []
            for p in impl_prs:
                merged_at = p.get("_merged_at")
                if merged_at:
                    try:
                        if isinstance(merged_at, datetime):
                            merged_date = merged_at.date()
                        else:
                            merged_date = datetime.fromisoformat(merged_at).date()
                        if merged_date < cycle_start:
                            continue
                    except (ValueError, TypeError):
                        pass
                filtered.append(p)
            impl_prs = filtered
            removed = before_count - len(impl_prs)
            if removed:
                log(f"VEP {vep.name}: filtered {removed} pre-cycle impl PR(s) (cycle start: {cycle_start_date})",
                    node="alert_formatting")

        # Filter out borderline impl PRs that were backported to the previous release.
        # PRs merged early in the cycle (before midpoint of cycle_start to EF) that
        # have cherry-picks targeting the previous release branch are previous-cycle
        # work that happened to land during the transition period.
        ef_date_str = indexed_context.get("release_deadlines", {}).get("enhancement_freeze")
        current_release = indexed_context.get("current_release", "")
        if cycle_start_date and ef_date_str and current_release:
            cycle_start_d = date.fromisoformat(cycle_start_date) if isinstance(cycle_start_date, str) else cycle_start_date
            ef_date_clean = ef_date_str.split('T')[0] if 'T' in ef_date_str else ef_date_str
            ef_date_d = date.fromisoformat(ef_date_clean) if isinstance(ef_date_str, str) else ef_date_str
            midpoint = cycle_start_d + (ef_date_d - cycle_start_d) / 2
            # Derive previous release branch name (e.g., "v1.9" -> "release-1.8")
            ver_match = re.match(r'v?(\d+)\.(\d+)', current_release)
            if ver_match:
                major, minor = int(ver_match.group(1)), int(ver_match.group(2))
                prev_branch = f"release-{major}.{minor - 1}"
                # Build set of PR numbers that have backports to previous release
                backported_prs = set()
                for pr in kubevirt_prs:
                    if not isinstance(pr, dict):
                        continue
                    if pr.get("base_ref") == prev_branch:
                        body = pr.get("body_preview", "") or pr.get("body", "") or ""
                        title = pr.get("title", "") or ""
                        # Cherry-pick PRs reference original via "Cherry pick of #NNNNN" or "#NNNNN" in title
                        refs = re.findall(r'(?:#|/pull/)(\d+)', f"{title} {body}")
                        for ref in refs:
                            backported_prs.add(int(ref))
                # Filter impl PRs merged in the borderline window that were backported
                before_count = len(impl_prs)
                filtered = []
                for p in impl_prs:
                    merged_at = p.get("_merged_at")
                    if not merged_at:
                        pr_idx = prs_by_number.get(p["number"])
                        if pr_idx:
                            merged_at = pr_idx.get("merged_at")
                    if merged_at and p["number"] in backported_prs:
                        try:
                            if isinstance(merged_at, str):
                                merged_date = date.fromisoformat(merged_at.replace('Z', '+00:00').split('T')[0] if 'T' in merged_at else merged_at)
                            else:
                                merged_date = merged_at.date() if hasattr(merged_at, 'date') else merged_at
                            if merged_date <= midpoint:
                                impl_pr_numbers.discard(p["number"])
                                continue
                        except (ValueError, TypeError):
                            pass
                    filtered.append(p)
                impl_prs = filtered
                removed = before_count - len(impl_prs)
                if removed:
                    log(f"VEP {vep.name}: filtered {removed} backported borderline impl PR(s) "
                        f"(merged before {midpoint}, backported to {prev_branch})",
                        node="alert_formatting")

        # Strip internal fields before output
        impl_prs = [{"number": p["number"], "url": p["url"]} for p in impl_prs]

        # Get urgency level
        urgency, urgency_color = get_urgency_level(vep)

        # Get status comment from risk_assessment
        status_comment = "No risk assessment"
        if hasattr(vep, 'analysis') and vep.analysis:
            risk_assessment = vep.analysis.get("risk_assessment")
            if risk_assessment:
                prob = risk_assessment.get("merge_probability", "?")
                sentiment = risk_assessment.get("reviewer_sentiment", "unknown")
                recent_progress = risk_assessment.get("recent_progress", True)

                # Build status comment
                if prob != "?":
                    if prob >= 80:
                        status_comment = f"Likely to merge ({prob}%) - {sentiment}"
                    elif prob >= 50:
                        status_comment = f"Moderate risk ({prob}%) - {sentiment}"
                    else:
                        status_comment = f"At risk ({prob}%) - {sentiment}"

                    if not recent_progress:
                        status_comment += " [STALE]"
                else:
                    status_comment = f"Sentiment: {sentiment}"

        table_rows.append({
            "vep_number": vep.tracking_issue_id,
            "vep_name": vep.name,
            "title": vep.title[:50] if vep.title else "",  # Truncate for readability
            "proposal_prs": proposal_prs,
            "impl_prs": impl_prs,
            "urgency": urgency,
            "urgency_color": urgency_color,
            "status_comment": status_comment,
            "escalate": (
                vep.analysis.get("risk_assessment", {}).get("recommend_escalation", False)
                if hasattr(vep, 'analysis') and vep.analysis else False
            ),
        })

    # Log summary statistics (detailed table is logged in analyze_combined)
    red_count = sum(1 for row in table_rows if row["urgency"] == "RED")
    yellow_count = sum(1 for row in table_rows if row["urgency"] == "YELLOW")
    green_count = sum(1 for row in table_rows if row["urgency"] == "GREEN")
    log(
        f"VEP summary table built: {len(table_rows)} VEPs "
        f"(🔴 {red_count} RED, 🟡 {yellow_count} YELLOW, 🟢 {green_count} GREEN)",
        node="alert_formatting"
    )

    return table_rows


def format_pr_links_markdown(prs: list[dict[str, Any]]) -> str:
    """Format PR list as markdown links (for email alerts).

    Args:
        prs: List of {"number": int, "url": str}

    Returns:
        Comma-separated markdown links or "-" if empty
    """
    if not prs:
        return "-"

    links = [f"[#{pr['number']}]({pr['url']})" for pr in prs]
    return ", ".join(links)


def format_pr_links_slack(prs: list[dict[str, Any]]) -> str:
    """Format PR list as Slack mrkdwn links.

    Args:
        prs: List of {"number": int, "url": str}

    Returns:
        Comma-separated Slack links or "-" if empty
    """
    if not prs:
        return "-"

    links = [f"<{pr['url']}|#{pr['number']}>" for pr in prs]
    return ", ".join(links)


def format_pr_links_plain(prs: list[dict[str, Any]]) -> str:
    """Format PR list as plain text.

    Args:
        prs: List of {"number": int, "url": str}

    Returns:
        Comma-separated PR numbers or "-" if empty
    """
    if not prs:
        return "-"

    return ", ".join([f"#{pr['number']}" for pr in prs])


def format_pr_links_urls(prs: list[dict[str, Any]]) -> str:
    """Format PR list as full URLs for GitHub Projects V2 (auto-linked).

    Args:
        prs: List of {"number": int, "url": str}

    Returns:
        Comma-separated full URLs or "-" if empty
    """
    if not prs:
        return "-"

    return ", ".join(pr["url"] for pr in prs)


def build_markdown_table(table_rows: list[dict[str, Any]]) -> str:
    """Build markdown table from VEP summary data.

    Args:
        table_rows: Output from build_vep_summary_table()

    Returns:
        Markdown table string
    """
    if not table_rows:
        return "*No VEPs to display*"

    lines = []
    lines.append("| VEP # | Title | Proposal PR(s) | Impl PR(s) | Urgency | Status Comment |")
    lines.append("|-------|-------|----------------|------------|---------|----------------|")

    for row in table_rows:
        vep_num = row["vep_number"]
        title = row["title"]
        proposal_prs = format_pr_links_markdown(row["proposal_prs"])
        impl_prs = format_pr_links_markdown(row["impl_prs"])
        urgency = row["urgency"]
        status = row["status_comment"]

        # Add emoji for urgency
        urgency_emoji = {
            "RED": "🔴",
            "YELLOW": "🟡",
            "GREEN": "🟢",
        }.get(urgency, "⚪")

        lines.append(
            f"| {vep_num} | {title} | {proposal_prs} | {impl_prs} | {urgency_emoji} {urgency} | {status} |"
        )

    return "\n".join(lines)


def build_slack_table(table_rows: list[dict[str, Any]]) -> str:
    """Build Slack-formatted table from VEP summary data.

    Args:
        table_rows: Output from build_vep_summary_table()

    Returns:
        Slack mrkdwn formatted table string
    """
    if not table_rows:
        return "*No VEPs to display*"

    lines = []

    for row in table_rows:
        vep_num = row["vep_number"]
        vep_name = row["vep_name"]
        title = row["title"]
        proposal_prs = format_pr_links_slack(row["proposal_prs"])
        impl_prs = format_pr_links_slack(row["impl_prs"])
        urgency = row["urgency"]
        status = row["status_comment"]

        # Urgency emoji
        urgency_emoji = {
            "RED": ":red_circle:",
            "YELLOW": ":large_yellow_circle:",
            "GREEN": ":white_check_mark:",
        }.get(urgency, ":white_circle:")

        # VEP link
        vep_url = f"https://github.com/kubevirt/enhancements/issues/{vep_num}"
        vep_link = f"<{vep_url}|{vep_name}>"

        lines.append(
            f"{urgency_emoji} *{vep_link}* - {title}\n"
            f"   Proposal: {proposal_prs} | Impl: {impl_prs}\n"
            f"   Status: {status}"
        )

    return "\n\n".join(lines)


def get_phase_context_summary(indexed_context: dict[str, Any] | None = None) -> dict[str, Any]:
    """Get phase context summary for alert subjects/headers.

    Args:
        indexed_context: Optional indexed context (will be fetched if not provided)

    Returns:
        {
            "phase": str,
            "phase_display": str (friendly name),
            "release": str,
            "days_to_ef": int or None,
            "days_to_cf": int or None,
            "days_to_ga": int or None,
            "deadline_text": str (e.g., "12 days to Code Freeze")
        }
    """
    from datetime import datetime

    if indexed_context is None:
        indexed_context = create_indexed_context(cache_max_age_minutes=60)

    phase = indexed_context.get("release_phase", "unknown")
    release = indexed_context.get("current_release", "unknown")
    deadlines = indexed_context.get("release_deadlines", {})

    # Friendly phase names
    phase_display_map = {
        "design": "Design Phase",
        "development": "Development Phase",
        "stabilization": "Stabilization Phase",
        "post_release": "Post-Release",
    }
    phase_display = phase_display_map.get(phase, phase.title())

    # Calculate days to deadlines
    now = datetime.now(UTC)
    days_to_ef = None
    days_to_cf = None
    days_to_ga = None

    if deadlines.get("enhancement_freeze"):
        try:
            ef_str = deadlines["enhancement_freeze"]
            if isinstance(ef_str, str) and ef_str.endswith('Z'):
                ef_str = ef_str[:-1] + '+00:00'
            ef_date = datetime.fromisoformat(ef_str)
            if ef_date.tzinfo is None:
                ef_date = ef_date.replace(tzinfo=UTC)
            days_to_ef = (ef_date - now).days
        except (ValueError, TypeError):
            pass

    if deadlines.get("code_freeze"):
        try:
            cf_str = deadlines["code_freeze"]
            if isinstance(cf_str, str) and cf_str.endswith('Z'):
                cf_str = cf_str[:-1] + '+00:00'
            cf_date = datetime.fromisoformat(cf_str)
            if cf_date.tzinfo is None:
                cf_date = cf_date.replace(tzinfo=UTC)
            days_to_cf = (cf_date - now).days
        except (ValueError, TypeError):
            pass

    if deadlines.get("ga"):
        try:
            ga_str = deadlines["ga"]
            if isinstance(ga_str, str) and ga_str.endswith('Z'):
                ga_str = ga_str[:-1] + '+00:00'
            ga_date = datetime.fromisoformat(ga_str)
            if ga_date.tzinfo is None:
                ga_date = ga_date.replace(tzinfo=UTC)
            days_to_ga = (ga_date - now).days
        except (ValueError, TypeError):
            pass

    # Build deadline text based on phase
    deadline_text = ""
    if phase == "design" and days_to_ef is not None:
        deadline_text = f"{days_to_ef} days to Enhancement Freeze"
    elif phase == "development" and days_to_cf is not None:
        deadline_text = f"{days_to_cf} days to Code Freeze"
    elif phase == "stabilization" and days_to_ga is not None:
        deadline_text = f"{days_to_ga} days to GA"

    return {
        "phase": phase,
        "phase_display": phase_display,
        "release": release,
        "days_to_ef": days_to_ef,
        "days_to_cf": days_to_cf,
        "days_to_ga": days_to_ga,
        "deadline_text": deadline_text,
    }
