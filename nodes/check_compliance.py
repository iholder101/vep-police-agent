"""Compliance context node - computes compliance-related data for VEPs.

Deterministic node using indexed context (no LLM). Checks PR status,
labels, template completeness, and implementation PRs for each VEP.
"""

import re
from datetime import datetime
from typing import Any
from state import VEPState
from services.utils import log
from services.indexer import create_indexed_context
from nodes._check_helpers import extract_vep_num, collect_impl_prs


def check_compliance_node(state: VEPState) -> Any:
    """Compute compliance context for VEPs from indexed data.

    Checks enhancement PR status/labels, tracking issue labels,
    template sections, and implementation PRs.
    """
    veps = state.get("veps", [])
    log(f"Computing compliance context for {len(veps)} VEP(s)", node="check_compliance")

    last_check_times = state.get("last_check_times", {})
    last_check_times["check_compliance"] = datetime.now()

    if not veps:
        return {"last_check_times": last_check_times}

    index_cache_minutes = state.get("index_cache_minutes", 60)
    indexed_context = create_indexed_context(cache_max_age_minutes=index_cache_minutes)

    enhancements_prs = indexed_context.get("enhancements_prs", [])
    issues_index = indexed_context.get("issues_index", [])
    vep_files_index = indexed_context.get("vep_files_index", [])
    vep_to_pr_mappings = indexed_context.get("vep_to_pr_mappings", {})
    board_veps = indexed_context.get("board_veps", {})
    prs_index = indexed_context.get("prs_index", [])

    context_by_id = {}
    for vep in veps:
        # 1. Enhancement PR status
        vep_pr = next(
            (pr for pr in enhancements_prs
             if pr.get("vep_issue_number") == vep.tracking_issue_id),
            None,
        )
        pr_labels = [l.lower() for l in vep_pr.get("labels", [])] if vep_pr else []
        has_lgtm = "lgtm" in pr_labels
        has_approved_label = any("approved" in l for l in pr_labels)

        # 2. Tracking issue labels
        issue = next(
            (i for i in issues_index if i.get("number") == vep.tracking_issue_id),
            None,
        )
        labels = issue.get("labels", []) if issue else []
        sig_labels = [l for l in labels if l.startswith("sig/")]
        release_labels = [l for l in labels if re.match(r'^v\d', l)]

        # 3. Implementation PRs (deduplicated from all sources)
        impl_prs = collect_impl_prs(
            vep.tracking_issue_id, board_veps, vep_to_pr_mappings, prs_index
        )

        # 4. Docs PR (heuristic: kubevirt PR title contains 'doc' and references this VEP)
        docs_pr = None
        for p in prs_index:
            if not isinstance(p, dict):
                continue
            if p.get("vep_issue_number") != vep.tracking_issue_id:
                continue
            if "doc" in p.get("title", "").lower():
                docs_pr = {"number": p["number"], "state": p.get("state", "unknown")}
                break

        # 5. Template sections from VEP markdown
        vep_num = extract_vep_num(vep.name)
        vep_file = next(
            (f for f in vep_files_index
             if extract_vep_num(f.get("vep_number")) == vep_num),
            None,
        ) if vep_num is not None else None
        content = vep_file.get("content", "") if vep_file else ""

        has_motivation = bool(re.search(
            r"^##\s*(motivation|goals)", content, re.MULTILINE | re.IGNORECASE
        ))
        has_design = bool(re.search(
            r"^##\s*(design|proposal)", content, re.MULTILINE | re.IGNORECASE
        ))
        has_api = bool(re.search(
            r"^##\s*api", content, re.MULTILINE | re.IGNORECASE
        ))
        has_test_plan = bool(re.search(
            r"^##\s*test\s*plan", content, re.MULTILINE | re.IGNORECASE
        ))

        context_by_id[vep.tracking_issue_id] = {
            "pr_state": vep_pr.get("state") if vep_pr else None,
            "pr_number": vep_pr.get("number") if vep_pr else None,
            "pr_url": vep_pr.get("html_url", vep_pr.get("url")) if vep_pr else None,
            "has_lgtm": has_lgtm,
            "has_approved_label": has_approved_label,
            "reviewers": [],
            "sig_labels": sig_labels,
            "release_labels": release_labels,
            "other_labels": [l for l in labels if l not in sig_labels + release_labels],
            "implementation_prs": impl_prs,
            "docs_pr": docs_pr,
            "has_motivation_section": has_motivation,
            "has_design_section": has_design,
            "has_api_section": has_api,
            "has_test_plan": has_test_plan,
        }

    vep_updates_by_check = state.get("vep_updates_by_check", {})
    vep_updates_by_check["check_compliance"] = {
        "context_field": "compliance",
        "updates": context_by_id,
    }

    log(f"Computed compliance context for {len(context_by_id)} VEP(s)", node="check_compliance")

    return {
        "last_check_times": last_check_times,
        "vep_updates_by_check": vep_updates_by_check,
    }
