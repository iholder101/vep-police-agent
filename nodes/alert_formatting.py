"""Alert formatting helpers for rich phase-aware VEP summary tables.

Provides utilities to build per-VEP summary tables with:
- VEP #, Title
- Proposal PR(s) and Impl PR(s) links
- Urgency indicators (RED/YELLOW/GREEN)
- Status comments from LLM risk assessment
"""

import re
from datetime import date, datetime
from typing import List, Dict, Any, Tuple
from services.indexer import create_indexed_context
from services.utils import log


def get_urgency_level(vep: Any) -> Tuple[str, str]:
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


def build_vep_summary_table(veps: List[Any], indexed_context: Dict[str, Any] = None) -> List[Dict[str, Any]]:
    """Build per-VEP summary table data.

    Args:
        veps: List of VEPInfo objects
        indexed_context: Optional indexed context (will be fetched if not provided)

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
    board_veps = indexed_context.get("board_veps", {})
    issues_index = indexed_context.get("issues_index", [])

    # Build base_ref lookup from prs_index for filtering backport impl PRs
    prs_by_number = {}
    for pr in indexed_context.get("prs_index", []):
        if isinstance(pr, dict) and pr.get("number"):
            prs_by_number[pr["number"]] = pr

    # Build a lookup from issue number to issue data
    issues_by_number = {}
    for issue in issues_index:
        issue_num = issue.get("number")
        if issue_num:
            issues_by_number[issue_num] = issue

    table_rows = []

    for vep in veps:
        # Get proposal PRs for this VEP from multiple sources
        proposal_pr_numbers = set()

        # Extract VEP number from VEP name (e.g., "vep-0016" -> 16)
        vep_number = None
        vep_match = re.search(r'vep-?(\d+)', vep.name, re.IGNORECASE)
        if vep_match:
            vep_number = int(vep_match.group(1))

        # Source 1: Extract from tracking issue body
        issue = issues_by_number.get(vep.tracking_issue_id)
        if issue:
            body = issue.get("body", "") or issue.get("body_preview", "")
            if body:
                # Extract enhancements PR URLs from issue body
                pr_url_pattern = re.compile(r'github\.com/kubevirt/enhancements/pull/(\d+)')
                for match in pr_url_pattern.finditer(body):
                    proposal_pr_numbers.add(int(match.group(1)))

        # Source 2: Match PRs by VEP number in PR title (MOST RELIABLE)
        # Many proposal PRs have titles like "VEP 16: Title" or "vep-0016: Title"
        if vep_number:
            for pr in enhancements_prs:
                pr_title = pr.get("title", "")
                # Look for VEP number in PR title
                title_vep_patterns = [
                    rf'VEP[- ]?{vep_number}[\s:]',  # "VEP 16:" or "VEP-16:"
                    rf'vep[- ]?{vep_number:04d}[\s:]',  # "vep-0016:"
                    rf'vep[- ]?{vep_number}[\s:]',  # "vep-16:" or "vep 16:"
                ]
                for pattern in title_vep_patterns:
                    if re.search(pattern, pr_title, re.IGNORECASE):
                        proposal_pr_numbers.add(pr.get("number"))
                        break

        # Source 3: Match from enhancements_prs by vep_issue_number (fallback)
        for pr in enhancements_prs:
            if pr.get("vep_issue_number") == vep.tracking_issue_id:
                proposal_pr_numbers.add(pr.get("number"))

        # Build proposal PRs list with URLs and merged_at for filtering
        proposal_prs = []
        for pr_num in sorted(proposal_pr_numbers):
            enh_pr = enhancements_by_number.get(pr_num, {})
            proposal_prs.append({
                "number": pr_num,
                "url": f"https://github.com/kubevirt/enhancements/pull/{pr_num}",
                "_merged_at": enh_pr.get("merged_at"),
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

        # Get implementation PRs from multiple sources
        # IMPORTANT: Implementation PRs can NEVER be from kubevirt/enhancements (those are proposal PRs)
        impl_pr_numbers = set()
        impl_prs = []

        # Source 1: Use vep.implementation_prs if available (from board data)
        if vep.implementation_prs:
            for pr in vep.implementation_prs:
                # Skip enhancements PRs - they are proposal PRs, not implementation PRs
                if "enhancements" in (pr.url or ""):
                    continue
                if pr.number not in impl_pr_numbers:
                    impl_pr_numbers.add(pr.number)
                    impl_prs.append({
                        "number": pr.number,
                        "url": pr.url,
                        "_state": pr.state,
                        "_merged_at": pr.merged_at,
                    })
        else:
            # Fall back to board_veps (in case VEPInfo doesn't have them)
            board_vep = board_veps.get(vep.tracking_issue_id, {})
            board_impl_prs = board_vep.get("impl_prs", [])
            for pr in board_impl_prs:
                pr_num = pr.get("number")
                pr_url = pr.get("url", "")
                # Skip enhancements PRs
                if "enhancements" in pr_url:
                    continue
                if pr_num and pr_num not in impl_pr_numbers:
                    impl_pr_numbers.add(pr_num)
                    impl_prs.append({
                        "number": pr_num,
                        "url": pr_url,
                        "_state": pr.get("state"),
                        "_merged_at": pr.get("merged_at"),
                    })

        # Source 2: Match from kubevirt PRs by vep_issue_number
        # (PRs that reference this VEP issue in their description)
        kubevirt_prs = indexed_context.get("prs_index", [])
        for pr in kubevirt_prs:
            if pr.get("vep_issue_number") == vep.tracking_issue_id:
                pr_num = pr.get("number")
                pr_url = pr.get("url", f"https://github.com/kubevirt/kubevirt/pull/{pr_num}")
                if "enhancements" in pr_url:
                    continue
                base_ref = pr.get("base_ref")
                if base_ref and base_ref not in ("main", "master"):
                    continue
                if pr_num and pr_num not in impl_pr_numbers:
                    impl_pr_numbers.add(pr_num)
                    impl_prs.append({
                        "number": pr_num,
                        "url": pr_url,
                        "_state": pr.get("state"),
                        "_merged_at": pr.get("merged_at"),
                    })

        # Source 3: Match from vep_to_pr_mappings (PRs with "vep-62" etc. in title/body)
        # Skip PRs whose title clearly indicates a different VEP (body-only mentions
        # are often references/dependencies, not implementations).
        vep_to_pr_mappings = indexed_context.get("vep_to_pr_mappings", {})
        vep_key = str(vep.tracking_issue_id)
        title_vep_pattern = re.compile(r'vep[-\s#]?0*(\d+)', re.IGNORECASE)
        for pr in vep_to_pr_mappings.get(vep_key, []):
            pr_num = pr.get("number")
            pr_url = pr.get("url", f"https://github.com/kubevirt/kubevirt/pull/{pr_num}")
            if "enhancements" in pr_url:
                continue
            base_ref = pr.get("base_ref")
            if not base_ref and pr_num:
                idx = prs_by_number.get(pr_num)
                if idx:
                    base_ref = idx.get("base_ref")
            if base_ref and base_ref not in ("main", "master"):
                continue
            # Skip if PR title references a different VEP number
            pr_title = pr.get("title", "")
            title_vep_matches = title_vep_pattern.findall(pr_title)
            if title_vep_matches and vep_key not in title_vep_matches:
                continue
            if pr_num and pr_num not in impl_pr_numbers:
                impl_pr_numbers.add(pr_num)
                impl_prs.append({
                    "number": pr_num,
                    "url": pr_url,
                    "_state": pr.get("state"),
                    "_merged_at": pr.get("merged_at"),
                })

        # Exclude impl PRs that are actually from the enhancements repo (proposal PRs)
        # Compare by URL since the same PR number can exist in both repos
        impl_prs = [p for p in impl_prs if "enhancements" not in p.get("url", "")]

        # Sort by PR number
        impl_prs = sorted(impl_prs, key=lambda x: x["number"])

        # Filter out backport PRs (targeting release branches, not main)
        before_count = len(impl_prs)
        filtered = []
        for p in impl_prs:
            pr_index_data = prs_by_number.get(p["number"])
            if pr_index_data:
                base_ref = pr_index_data.get("base_ref")
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


def format_pr_links_markdown(prs: List[Dict[str, Any]]) -> str:
    """Format PR list as markdown links.

    Args:
        prs: List of {"number": int, "url": str}

    Returns:
        Comma-separated markdown links or "-" if empty
    """
    if not prs:
        return "-"

    links = [f"[#{pr['number']}]({pr['url']})" for pr in prs]
    return ", ".join(links)


def format_pr_links_slack(prs: List[Dict[str, Any]]) -> str:
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


def format_pr_links_plain(prs: List[Dict[str, Any]]) -> str:
    """Format PR list as plain text.

    Args:
        prs: List of {"number": int, "url": str}

    Returns:
        Comma-separated PR numbers or "-" if empty
    """
    if not prs:
        return "-"

    return ", ".join([f"#{pr['number']}" for pr in prs])


def build_markdown_table(table_rows: List[Dict[str, Any]]) -> str:
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


def build_slack_table(table_rows: List[Dict[str, Any]]) -> str:
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


def get_phase_context_summary(indexed_context: Dict[str, Any] = None) -> Dict[str, Any]:
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
    from datetime import timezone
    now = datetime.now(timezone.utc)
    days_to_ef = None
    days_to_cf = None
    days_to_ga = None

    if deadlines.get("enhancement_freeze"):
        try:
            ef_date = datetime.fromisoformat(deadlines["enhancement_freeze"].replace('Z', '+00:00'))
            if ef_date.tzinfo is None:
                ef_date = ef_date.replace(tzinfo=timezone.utc)
            days_to_ef = (ef_date - now).days
        except:
            pass

    if deadlines.get("code_freeze"):
        try:
            cf_date = datetime.fromisoformat(deadlines["code_freeze"].replace('Z', '+00:00'))
            if cf_date.tzinfo is None:
                cf_date = cf_date.replace(tzinfo=timezone.utc)
            days_to_cf = (cf_date - now).days
        except:
            pass

    if deadlines.get("ga"):
        try:
            ga_date = datetime.fromisoformat(deadlines["ga"].replace('Z', '+00:00'))
            if ga_date.tzinfo is None:
                ga_date = ga_date.replace(tzinfo=timezone.utc)
            days_to_ga = (ga_date - now).days
        except:
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
