"""Unit tests for deterministic PR -> VEP attribution.

Fixtures are grounded in real kubevirt/enhancements + kubevirt/kubevirt data
(the VEP 25 / 25.1 / 25.2 family and the VEP 349 vs 371 impl-PR cluster) that
drove the WS1 attribution redesign.
"""

from services.attribution import (
    extract_tracking_issue,
    parse_enumerated_impl_prs,
    resolve_impl_owner,
)


class TestExtractTrackingIssue:
    def test_markdown_bold_tracking_issue_url(self):
        # PR #398 - the "VEP 25 Extension" design PR. Belongs to VEP 25 only.
        assert extract_tracking_issue(
            "VEP 25 Extension: Improve Pull Mode Design",
            "**Tracking issue**: https://github.com/kubevirt/enhancements/issues/25",
        ) == 25

    def test_tracking_issue_beats_footnote(self):
        # PR #400 - declares tracking issue 399 (VEP 25.1) but also footnotes the
        # parent VEP 25. The explicit tracking-issue line must win over the
        # generic enhancements URL, so this is 399 - not 25.
        body = (
            "**Tracking issue**: https://github.com/kubevirt/enhancements/issues/399\n\n"
            "This is an extension of https://github.com/kubevirt/enhancements/issues/25"
        )
        assert extract_tracking_issue("VEP 25.1: Multi-checkpoint", body) == 399

    def test_sibling_proposal_prs(self):
        assert extract_tracking_issue(
            "VEP 25.2: Auto provision",
            "**Tracking issue**: https://github.com/kubevirt/enhancements/issues/416",
        ) == 416
        assert extract_tracking_issue(
            "VEP 323 proposal",
            "**Tracking issue**: https://github.com/kubevirt/enhancements/issues/323",
        ) == 323

    def test_sub_vep_title_alone_does_not_truncate(self):
        # "VEP 25.1" in a title must NOT collapse to 25 - that was the root of
        # the #398-on-sibling contamination. With no explicit link -> unlinked.
        assert extract_tracking_issue("VEP 25.1: Multi-checkpoint", "") is None

    def test_whole_number_title_still_matches(self):
        assert extract_tracking_issue("VEP 165: Something", "") == 165

    def test_impl_pr_bare_enhancements_issue_url(self):
        # The approved-vep bot's signal: a bare enhancements issue URL in an
        # impl PR body. Kept as the last-resort pattern.
        assert extract_tracking_issue(
            "Add integration test framework",
            "Implements https://github.com/kubevirt/enhancements/issues/371",
        ) == 371

    def test_keyword_inline_number(self):
        assert extract_tracking_issue("Some PR", "Tracking issue: #80") == 80
        assert extract_tracking_issue("Some PR", "VEP tracking issue: #349") == 349

    def test_placeholder_rejected(self):
        assert extract_tracking_issue(
            "Some PR", "Closes kubevirt/enhancements#<vep_tracking_issue_number>"
        ) is None
        assert extract_tracking_issue(
            "Some PR",
            "Tracking issue: https://github.com/kubevirt/enhancements/issues/<vep_tracking_issue_number>",
        ) is None

    def test_empty_and_none(self):
        assert extract_tracking_issue("", "") is None
        assert extract_tracking_issue(None, None) is None


class TestParseEnumeratedImplPrs:
    def test_issue_371_enumerates_full_cluster(self):
        # Real shape of issue #371's body: full kubevirt/kubevirt pull URLs,
        # plus an enhancements proposal-PR URL and a bare #ref that must NOT be
        # picked up as impl PRs.
        body = (
            "Design PR: https://github.com/kubevirt/enhancements/pull/348\n"
            "Related: #18238\n"
            "Impl PRs:\n"
            "- https://github.com/kubevirt/kubevirt/pull/18726\n"
            "- https://github.com/kubevirt/kubevirt/pull/18727\n"
            "- https://github.com/kubevirt/kubevirt/pull/18728\n"
            "- https://github.com/kubevirt/kubevirt/pull/18729\n"
            "- https://github.com/kubevirt/kubevirt/pull/18730\n"
            "- https://github.com/kubevirt/kubevirt/pull/18731\n"
            "- https://github.com/kubevirt/kubevirt/pull/18841\n"
        )
        assert parse_enumerated_impl_prs(body) == {
            18726, 18727, 18728, 18729, 18730, 18731, 18841,
        }

    def test_excludes_enhancements_pull_urls(self):
        assert parse_enumerated_impl_prs(
            "https://github.com/kubevirt/enhancements/pull/350"
        ) == set()

    def test_empty(self):
        assert parse_enumerated_impl_prs("") == set()
        assert parse_enumerated_impl_prs(None) == set()


class TestResolveImplOwner:
    def test_reciprocated(self):
        # 18726 self-refs 371 and 371 lists it back -> confident.
        assert resolve_impl_owner(371, {371}) == (371, False)

    def test_unreciprocated_conflict_pr_wins_and_flags(self):
        # 18727 self-refs 349 (copy-paste), 371 lists it. Neither reciprocates;
        # trust the PR's own claim (349) but flag the conflict.
        assert resolve_impl_owner(349, {371}) == (349, True)

    def test_pr_only_claim_trusted(self):
        # 18750 self-refs 25, no issue enumerates it -> 25, no conflict.
        assert resolve_impl_owner(25, set()) == (25, False)

    def test_pr_claim_beats_competing_enumeration(self):
        # 18750 self-refs 25 even though 25.2 (416) lists it in a stray
        # checkbox -> stays with 25, flagged.
        assert resolve_impl_owner(25, {416}) == (25, True)

    def test_issue_only(self):
        assert resolve_impl_owner(None, {371}) == (371, False)

    def test_multiple_issues_no_self_ref_ambiguous(self):
        assert resolve_impl_owner(None, {371, 349}) == (None, True)

    def test_unlinked(self):
        assert resolve_impl_owner(None, set()) == (None, False)
