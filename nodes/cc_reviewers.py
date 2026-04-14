"""CC reviewers node - CCs VEP approvers/reviewers into open implementation PRs.

For each VEP with known approvers or reviewers (extracted from enhancement PR
reviews), checks open implementation PRs for an existing CC comment and, if
none is found within the cooldown window, posts a comment @-mentioning them
for visibility.

No LLM involved - pure data checks and GitHub REST API calls.
"""

from datetime import datetime
from typing import Any, Dict, List

from state import VEPState
from services.utils import log
from services.github_api import (
    add_issue_comment,
    has_cc_comment,
    CC_REVIEWERS_MARKER,
)
from config.config import CC_REVIEWERS_COOLDOWN_DAYS

# Repository hosting implementation PRs
KUBEVIRT_OWNER = "kubevirt"
KUBEVIRT_REPO = "kubevirt"


def _build_cc_comment(vep, people: List[str]) -> str:
    """Build the Markdown CC comment body for an implementation PR."""
    mentions = " ".join(f"@{h}" for h in people)
    lines = [
        f"This PR implements **{vep.name}** "
        f"([tracking issue](https://github.com/kubevirt/enhancements/issues/{vep.tracking_issue_id})). "
        f"CC'ing the VEP approvers and reviewers for visibility:",
        "",
        f"cc {mentions}",
        "",
        CC_REVIEWERS_MARKER,
    ]
    return "\n".join(lines)


def cc_reviewers_node(state: VEPState) -> Any:
    """Post CC comments on open implementation PRs for VEPs with known reviewers.

    Skipped when ``skip_cc_reviewers`` is set in state.
    """
    last_check_times: Dict[str, datetime] = state.get("last_check_times", {})

    if state.get("skip_cc_reviewers", False):
        log("skip_cc_reviewers enabled, skipping", node="cc_reviewers")
        last_check_times["cc_reviewers"] = datetime.now()
        return {"last_check_times": last_check_times}

    veps = state.get("veps", [])
    if not veps:
        log("No VEPs to check for CC", node="cc_reviewers")
        last_check_times["cc_reviewers"] = datetime.now()
        return {"last_check_times": last_check_times}

    # Collect VEPs that have people to CC and open impl PRs
    candidates = []
    for vep in veps:
        people = sorted(set(getattr(vep, "approvers", []) + getattr(vep, "reviewers", [])))
        if not people:
            continue
        # Exclude the VEP owner from the CC list — they already know about the PR
        owner = getattr(vep, "owner", "")
        people = [p for p in people if p != owner]
        if not people:
            continue

        open_impl_prs = [
            pr for pr in getattr(vep, "implementation_prs", [])
            if pr.state == "open"
        ]
        if open_impl_prs:
            candidates.append((vep, people, open_impl_prs))

    log(f"{len(candidates)}/{len(veps)} VEP(s) have reviewers and open impl PRs",
        node="cc_reviewers")

    commented = 0
    skipped_cooldown = 0

    for vep, people, open_prs in candidates:
        for pr in open_prs:
            pr_number = pr.number
            if has_cc_comment(
                KUBEVIRT_OWNER,
                KUBEVIRT_REPO,
                pr_number,
                cooldown_days=CC_REVIEWERS_COOLDOWN_DAYS,
            ):
                log(f"VEP {vep.name} PR #{pr_number}: recent CC exists, skipping",
                    node="cc_reviewers", level="DEBUG")
                skipped_cooldown += 1
                continue

            comment_body = _build_cc_comment(vep, people)
            result = add_issue_comment(
                KUBEVIRT_OWNER, KUBEVIRT_REPO, pr_number, comment_body,
            )
            if result:
                commented += 1
                log(f"VEP {vep.name} PR #{pr_number}: CC'd {len(people)} reviewer(s)",
                    node="cc_reviewers")

    log(f"CC complete: {commented} PR(s) commented, "
        f"{skipped_cooldown} skipped (cooldown)",
        node="cc_reviewers")

    last_check_times["cc_reviewers"] = datetime.now()
    return {"last_check_times": last_check_times}
