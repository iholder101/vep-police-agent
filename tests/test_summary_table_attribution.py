"""End-to-end attribution test for build_vep_summary_table.

Reconstructs the VEP 25 family and the VEP 349/371 impl-PR cluster as a fake
indexed_context and asserts the exact Proposal/Impl PR columns the board should
show after the WS1 fix:

  - VEP 25   keeps proposal #398 (drops #400) and its five impl PRs incl. #18750
  - VEP 25.1 gets proposal #400 (no longer #398), no impl PRs
  - VEP 25.2 gets proposal #417 (no longer #398), no impl PRs (18750 stays on 25)
  - VEP 371  gets the reciprocated impl PRs #18726/#18841
  - VEP 349  keeps the impl PRs that self-declare it (#18727-31), flagged as
    conflicts (PR-wins-on-unreciprocated-conflict decision)
"""

from types import SimpleNamespace

from nodes.alert_formatting import build_vep_summary_table


def _vep(issue_id, name, title):
    return SimpleNamespace(tracking_issue_id=issue_id, name=name, title=title, analysis=None)


def _enh_pr(number, vep_issue_number):
    return {"number": number, "vep_issue_number": vep_issue_number,
            "state": "open", "merged_at": None}


def _kv_pr(number, self_ref):
    return {
        "number": number,
        "vep_issue_number": self_ref,
        "state": "open",
        "merged_at": None,
        "base_ref": "main",
        "url": f"https://github.com/kubevirt/kubevirt/pull/{number}",
        "title": f"kubevirt PR {number}",
        "body_preview": "",
    }


def _kv_url(number):
    return f"https://github.com/kubevirt/kubevirt/pull/{number}"


def _build_context():
    enhancements_prs = [
        _enh_pr(398, 25),
        _enh_pr(400, 399),
        _enh_pr(417, 416),
        _enh_pr(324, 323),
    ]
    prs_index = [
        _kv_pr(18313, 25), _kv_pr(18416, 25), _kv_pr(18537, 25),
        _kv_pr(18725, 25), _kv_pr(18750, 25),
        _kv_pr(18726, 371), _kv_pr(18841, 371),
        _kv_pr(18727, 349), _kv_pr(18728, 349), _kv_pr(18729, 349),
        _kv_pr(18730, 349), _kv_pr(18731, 349),
    ]
    issues_index = [
        {"number": 25, "body": "VEP 25 tracking issue"},
        {"number": 399, "body": "VEP 25.1 tracking issue"},
        # 25.2's body mislabels an impl PR under a checkbox - must NOT steal it
        # from VEP 25 (18750 self-declares issue 25).
        {"number": 416, "body": f"VEP 25.2. VEP PRs: {_kv_url(18750)}"},
        # 349 (EFI/vTPM) does not enumerate any of the cluster PRs.
        {"number": 349, "body": "Block-mode backend storage VEP. No impl PRs listed."},
        # 371 enumerates the whole cluster.
        {"number": 371, "body": "Impl PRs: " + " ".join(
            _kv_url(n) for n in (18726, 18727, 18728, 18729, 18730, 18731, 18841))},
    ]
    return {
        "enhancements_prs": enhancements_prs,
        "prs_index": prs_index,
        "issues_index": issues_index,
        "cycle_start_date": "2026-06-24",
        "release_deadlines": {},
        "current_release": "v1.10",
    }


def _rows_by_vep(veps):
    rows = build_vep_summary_table(veps, indexed_context=_build_context())
    return {r["vep_number"]: r for r in rows}


def _numbers(prs):
    return {p["number"] for p in prs}


def test_proposal_attribution_is_reference_based():
    veps = [
        _vep(25, "vep-0025", "VEP 25"),
        _vep(399, "vep-0025", "VEP 25.1"),
        _vep(416, "vep-0025", "VEP 25.2"),
    ]
    rows = _rows_by_vep(veps)
    # VEP 25 keeps its own design PR #398, and NO LONGER #400.
    assert _numbers(rows[25]["proposal_prs"]) == {398}
    # Sibling sub-VEPs drop the contaminating #398 and get their own PRs.
    assert _numbers(rows[399]["proposal_prs"]) == {400}
    assert _numbers(rows[416]["proposal_prs"]) == {417}


def test_impl_attribution_one_pr_one_vep():
    veps = [
        _vep(25, "vep-0025", "VEP 25"),
        _vep(399, "vep-0025", "VEP 25.1"),
        _vep(416, "vep-0025", "VEP 25.2"),
        _vep(371, "vep-0371", "VEP 371"),
        _vep(349, "vep-0349", "VEP 349"),
    ]
    rows = _rows_by_vep(veps)
    # VEP 25's five impl PRs, including #18750 (which 25.2 mislabels).
    assert _numbers(rows[25]["impl_prs"]) == {18313, 18416, 18537, 18725, 18750}
    assert _numbers(rows[399]["impl_prs"]) == set()
    assert _numbers(rows[416]["impl_prs"]) == set()
    # Reciprocated impl PRs land on 371; the copy-paste-declared ones stay on
    # their self-declared 349 (PR-wins-on-conflict). Each PR appears once.
    assert _numbers(rows[371]["impl_prs"]) == {18726, 18841}
    assert _numbers(rows[349]["impl_prs"]) == {18727, 18728, 18729, 18730, 18731}


def test_widen_attributes_enumerated_pr_absent_from_prs_index():
    """A tracking issue can enumerate an impl PR that never showed up in
    prs_index (e.g. beyond the fetch window). The widen pass must still
    attribute it to the sole enumerating in-scope VEP, but a PR enumerated by
    two in-scope issues (ambiguous, no self-ref to break the tie) must stay
    unlinked rather than being guessed onto either.
    """
    context = {
        "enhancements_prs": [],
        "prs_index": [],
        "issues_index": [
            {"number": 900, "body": f"Impl PRs: {_kv_url(99999)}"},
            {"number": 901, "body": f"Impl PRs: {_kv_url(88888)}"},
            {"number": 902, "body": f"Impl PRs: {_kv_url(88888)}"},
        ],
        "cycle_start_date": "2026-06-24",
        "release_deadlines": {},
        "current_release": "v1.10",
    }
    veps = [
        _vep(900, "vep-0900", "VEP 900"),
        _vep(901, "vep-0901", "VEP 901"),
        _vep(902, "vep-0902", "VEP 902"),
    ]
    rows = {r["vep_number"]: r for r in build_vep_summary_table(veps, indexed_context=context)}

    # Unambiguous: 99999 is enumerated only by 900, though absent from prs_index.
    assert 99999 in _numbers(rows[900]["impl_prs"])

    # Ambiguous: 88888 is enumerated by both 901 and 902, with no self-ref and
    # absent from prs_index -> must not be guessed onto either.
    assert 88888 not in _numbers(rows[901]["impl_prs"])
    assert 88888 not in _numbers(rows[902]["impl_prs"])


def test_widen_fetches_real_metadata_and_applies_cycle_filters():
    """Widened impl PRs (enumerated but absent from prs_index) must be run
    through the same cycle-scoping filters as indexed PRs, using real
    metadata from the injected fetcher - not silently included with
    _state=None/_merged_at=None.
    """
    context = {
        "enhancements_prs": [],
        "prs_index": [],
        "issues_index": [
            {"number": 950, "body": f"Impl PRs: {_kv_url(77001)} {_kv_url(77002)}"},
        ],
        "cycle_start_date": "2026-06-24",
        "release_deadlines": {},
        "current_release": "v1.10",
    }
    veps = [_vep(950, "vep-0950", "VEP 950")]

    def fake_fetcher(nums):
        assert nums == [77001, 77002]
        return {
            # Closed, never merged -> must be filtered out (closed-unmerged filter).
            77001: {"state": "closed", "merged_at": None, "base_ref": "main"},
            # Merged after cycle start on main -> must stay.
            77002: {"state": "merged", "merged_at": "2026-07-01T00:00:00Z", "base_ref": "main"},
        }

    rows = {r["vep_number"]: r for r in build_vep_summary_table(veps, indexed_context=context, pr_metadata_fetcher=fake_fetcher)}
    impl_numbers = _numbers(rows[950]["impl_prs"])
    assert 77001 not in impl_numbers
    assert 77002 in impl_numbers


def test_no_pr_attributed_to_more_than_one_vep():
    veps = [
        _vep(25, "vep-0025", "VEP 25"),
        _vep(399, "vep-0025", "VEP 25.1"),
        _vep(416, "vep-0025", "VEP 25.2"),
        _vep(371, "vep-0371", "VEP 371"),
        _vep(349, "vep-0349", "VEP 349"),
    ]
    rows = _rows_by_vep(veps)
    seen = []
    for r in rows.values():
        seen += [p["number"] for p in r["impl_prs"]]
        seen += [p["number"] for p in r["proposal_prs"]]
    assert len(seen) == len(set(seen)), f"PR attributed to multiple VEPs: {seen}"
