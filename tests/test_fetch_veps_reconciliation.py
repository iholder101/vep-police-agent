"""Regression test: impl-PR reconciliation must not double-list a PR across VEPs.

Bug: nodes/fetch_veps.py built each VEP's implementation_prs straight from its
own board_vep["impl_prs"] (parsed per-issue-body) with no cross-issue
reconciliation, so the same impl PR could land on two different VEPs (e.g.
PR #18750 on both VEP 25 and VEP 416, whose issue body also happens to link
it). The fix wires nodes/fetch_veps.py to the same
services.attribution.resolve_impl_pr_ownership reconciliation the clean
alert/board summary table already used, so each PR resolves to exactly one
owning VEP.
"""

import nodes.fetch_veps as fetch_veps_module

TS = "2026-01-01T00:00:00+00:00"


def _issue(number, title, body):
    return {
        "number": number,
        "title": title,
        "state": "open",
        "body": body,
        "labels": [],
        "assignee": None,
        "author": {"login": "someone"},
        "created_at": TS,
        "updated_at": TS,
    }


def _board_item(title, impl_pr_numbers):
    return {
        "title": title,
        "impl_prs": [
            {"number": n, "url": f"https://github.com/kubevirt/kubevirt/pull/{n}"}
            for n in impl_pr_numbers
        ],
        "fields": {"Status": "Tracked"},
        "assignees": [],
        "labels": [],
    }


def _pr(number, vep_issue_number):
    url = f"https://github.com/kubevirt/kubevirt/pull/{number}"
    return {
        "number": number,
        "vep_issue_number": vep_issue_number,
        "state": "open",
        "merged": False,
        "base_ref": "main",
        "title": f"Impl PR #{number}",
        "url": url,
        "html_url": url,
        "created_at": TS,
        "updated_at": TS,
        "author": "someone",
    }


def _indexed_context():
    return {
        "current_release": "v1.10",
        # VEP 416's issue body enumerates PR #18750 too, even though the PR's
        # own self-ref (in prs_index) points at VEP 25.
        "board_veps": {
            25: _board_item("VEP 25: Foo", [18750]),
            416: _board_item("VEP 25.2: Bar", [18750]),
        },
        "issues_index": [
            _issue(25, "VEP 25: Foo", "no enumeration here"),
            _issue(416, "VEP 25.2: Bar", "https://github.com/kubevirt/kubevirt/pull/18750"),
        ],
        "vep_files_index": [],
        "enhancements_prs": [],
        "prs_index": [_pr(18750, vep_issue_number=25)],
        "vep_to_pr_mappings": {},
    }


def _by_tracking_id(veps, tracking_id):
    return next(v for v in veps if v.tracking_issue_id == tracking_id)


def test_impl_pr_attributed_to_single_vep(monkeypatch):
    monkeypatch.setattr(fetch_veps_module, "create_indexed_context", lambda cache_max_age_minutes=60: _indexed_context())

    result = fetch_veps_module.fetch_veps_node({})
    veps = result["veps"]

    vep_25 = _by_tracking_id(veps, 25)
    vep_416 = _by_tracking_id(veps, 416)

    assert {pr.number for pr in vep_25.implementation_prs} == {18750}
    assert {pr.number for pr in vep_416.implementation_prs} == set()
