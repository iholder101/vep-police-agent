"""Deterministic PR -> VEP attribution.

Pure, side-effect-free helpers for mapping GitHub PRs to VEP tracking issues.
Kept separate from services.indexer so the logic stays unit-testable without the
network / MCP import cost that services.indexer carries.

Attribution rules (WS1 redesign):
- One PR -> at most one VEP, keyed on the VEP's tracking-issue number.
- Proposal PRs (kubevirt/enhancements): attributed solely by the PR's own
  explicit tracking-issue reference (see ``extract_tracking_issue``).
- Impl PRs (kubevirt/kubevirt): a reciprocity model between the PR's
  self-declared reference and the tracking issue's own enumerated PR list.
  When only one side speaks it is trusted; on an unreciprocated conflict the
  PR's own reference wins and the conflict is surfaced (see ``resolve_impl_owner``).
- Title-based "VEP N" matching is NOT used for attribution: it truncates
  sub-VEPs (e.g. "VEP 25.1" -> 25) and collides siblings onto one number.
"""

import re

# Literal placeholder left in unfilled PR templates - never a real reference.
PLACEHOLDER_REF = "<vep_tracking_issue_number>"


def _strip_markdown(text: str) -> str:
    """Drop markdown emphasis / backtick chars that break keyword regexes.

    Real-world PR bodies write "**Tracking issue**: ..." - the surrounding
    asterisks otherwise sit between the keyword and its value and defeat the
    keyword patterns below.
    """
    return re.sub(r"[*`]", "", text or "")


def extract_tracking_issue(title: str, body: str) -> int | None:
    """Return the enhancements tracking-issue number a PR references, or None.

    Priority order (explicit declarations beat generic URL scanning):
      1. Keyword declaration with inline number:  "Tracking issue: #399"
      2. Keyword declaration with an issue URL:   "Tracking issue: .../issues/399"
      3. "VEP N" in the title, whole numbers only (sub-VEPs "VEP N.M" skipped)
      4. Generic close keyword:                   "closes #399"
      5. Any enhancements issue URL (least reliable; this is the signal the
         kubevirt approved-vep bot keys on for implementation PRs)

    Markdown emphasis around keywords is tolerated; the literal
    ``<vep_tracking_issue_number>`` template placeholder is rejected.
    """
    clean_title = _strip_markdown(title)
    text = _strip_markdown(f"{title or ''} {body or ''}").replace(PLACEHOLDER_REF, " ")
    if not text.strip():
        return None

    # 1. Keyword declaration with an inline number (most intentional).
    for pattern in (
        r"tracking\s+issue[\s:]+#?(\d+)",
        r"vep\s+tracker[\s:]+#?(\d+)",
        r"tracker[\s:]+#?(\d+)",
        r"vep\s+issue[\s:]+#?(\d+)",
    ):
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return int(match.group(1))

    # 2. Keyword declaration followed by an issue URL.
    match = re.search(
        r"(?:tracking\s+issue|vep\s+tracker|tracker|vep\s+issue)"
        r"[\s:]+\S*github\.com/kubevirt/enhancements/issues/(\d+)",
        text,
        re.IGNORECASE,
    )
    if match:
        return int(match.group(1))

    # 3. "VEP N" in the title - whole numbers only. The negative lookahead
    #    skips sub-VEPs ("VEP 25.1"), which would otherwise truncate to 25 and
    #    collide the sibling onto the parent VEP.
    if clean_title:
        match = re.search(r"VEP[- #]*0*(\d+)(?![.\d])", clean_title, re.IGNORECASE)
        if match:
            return int(match.group(1))

    # 4. Generic close keywords.
    match = re.search(r"(?:fixes|closes)[\s:]+#?(\d+)", text, re.IGNORECASE)
    if match:
        return int(match.group(1))

    # 5. Any enhancements issue URL (footnotes/deps possible - last resort).
    match = re.search(r"github\.com/kubevirt/enhancements/issues/(\d+)", text)
    if match:
        return int(match.group(1))

    return None


def parse_enumerated_impl_prs(issue_body: str) -> set[int]:
    """Return kubevirt/kubevirt PR numbers a tracking issue body enumerates.

    Matches full kubevirt/kubevirt pull URLs only (the form issues use to list
    their implementation PRs). Enhancements PR URLs and bare ``#N`` references
    are intentionally ignored: the former are proposal PRs, the latter are
    ambiguous within an enhancements-repo issue.
    """
    if not issue_body:
        return set()
    text = _strip_markdown(issue_body)
    return {int(m) for m in re.findall(r"kubevirt/kubevirt/pull/(\d+)", text)}


def resolve_impl_owner(
    self_ref: int | None,
    enumerating_issues: set[int],
) -> tuple[int | None, bool]:
    """Resolve the single owning VEP tracking issue for one implementation PR.

    Args:
        self_ref: the in-scope tracking issue the PR declares in its body, or
            None (caller passes None when the PR declares nothing in scope).
        enumerating_issues: the in-scope tracking issues whose body enumerates
            this PR.

    Returns:
        ``(owner_issue_number | None, conflict)``. ``owner`` is None when the PR
        cannot be attributed (unlinked). ``conflict`` is True when the two
        signals disagreed or were ambiguous and the result should be surfaced.

    Reciprocity rules:
      - self_ref reciprocated (self_ref enumerates the PR back) -> confident.
      - self_ref set, no issue enumerates it -> trust the PR's own claim.
      - self_ref set, competing issue(s) enumerate it instead -> trust the
        PR's claim but flag the conflict.
      - no self_ref, exactly one issue enumerates it -> that issue.
      - no self_ref, several issues enumerate it -> unlinked + flag.
    """
    enum = set(enumerating_issues)
    if self_ref is not None:
        if self_ref in enum:
            return self_ref, False          # reciprocated -> confident
        if not enum:
            return self_ref, False          # PR-only claim -> trust it
        # self_ref not reciprocated, but other issue(s) enumerate this PR: an
        # unreciprocated conflict. Trust the PR's own declaration (usually more
        # reliable than an issue's hand-maintained list) but flag it for review.
        return self_ref, True
    if len(enum) == 1:
        return next(iter(enum)), False      # issue-only, unambiguous
    if len(enum) >= 2:
        return None, True                   # several issues, no self-ref -> ambiguous
    return None, False                      # unlinked


def resolve_impl_pr_ownership(
    issue_bodies_by_id: dict[int, str],
    pr_self_refs: dict[int, int | None],
) -> dict[int, tuple[int | None, bool]]:
    """Resolve the single owning VEP for a batch of implementation PRs.

    This is the shared reconciliation step behind both the alert/board
    summary table (``nodes.alert_formatting.build_vep_summary_table``) and
    VEP discovery (``nodes.fetch_veps``). Both need the same answer to "which
    VEP owns impl PR N" - keeping it in one place prevents the two call sites
    from drifting apart and re-introducing cross-VEP double-listing (e.g. the
    same impl PR appearing on two different tracking issues because each
    issue's body happens to link it).

    Args:
        issue_bodies_by_id: in-scope tracking-issue id -> issue body text.
            Used to build the global "which issues enumerate this PR" map
            (see ``parse_enumerated_impl_prs``).
        pr_self_refs: impl PR number -> the PR's own declared tracking-issue
            reference, or None if the PR declares nothing in scope. PRs with
            no self-ref info at all (not present in the caller's PR index)
            should simply be omitted from this dict - they are still resolved
            below via the enumerated-issues map alone.

    Returns:
        dict mapping every PR number that appears in ``pr_self_refs`` and/or
        is enumerated by an issue body, to ``(owner_issue_id | None,
        conflict)`` per ``resolve_impl_owner``'s semantics.
    """
    in_scope_issues = set(issue_bodies_by_id.keys())
    enumerated_by_pr: dict[int, set[int]] = {}
    for issue_id, body in issue_bodies_by_id.items():
        for pr_num in parse_enumerated_impl_prs(body):
            enumerated_by_pr.setdefault(pr_num, set()).add(issue_id)

    all_pr_numbers = set(pr_self_refs.keys()) | set(enumerated_by_pr.keys())
    ownership: dict[int, tuple[int | None, bool]] = {}
    for pr_num in all_pr_numbers:
        self_ref = pr_self_refs.get(pr_num)
        if self_ref not in in_scope_issues:
            self_ref = None
        enumerating = enumerated_by_pr.get(pr_num, set()) & in_scope_issues
        ownership[pr_num] = resolve_impl_owner(self_ref, enumerating)
    return ownership
