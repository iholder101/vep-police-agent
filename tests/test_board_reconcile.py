"""Regression tests for board reconciliation (WS5).

Bug: items that leave scope (e.g. VEP 323 "Removed from Milestone") kept
stale Agent Urgency/Comment values forever, because fetch_veps drops
out-of-scope VEPs before the summary table is built, so
update_project_board_node never revisited them. The fix adds a
reconciliation pass: enumerate ALL board items and clear the 4 agent-owned
fields (Agent Urgency, Agent Comment, Proposal PRs, Impl PRs) on every item
that is not in the in-scope set (derived from vep_summary_table), while
leaving in-scope items' normal writes untouched. Clearing uses the
clearProjectV2ItemFieldValue mutation (via clear_project_item_fields), not
an empty-string write.
"""

import nodes.update_project_board as board_module

PROJECT_ID = "PVT_project"

FIELDS_META = {
    "Agent Urgency": {"id": "PVTF_urgency", "type": "TEXT"},
    "Agent Comment": {"id": "PVTF_comment", "type": "TEXT"},
    "Proposal PRs": {"id": "PVTF_proposal", "type": "TEXT"},
    "Impl PRs": {"id": "PVTF_impl", "type": "TEXT"},
    # Extra board field the agent must never touch.
    "Status": {"id": "PVTF_status", "type": "SINGLE_SELECT"},
}


def _install_common_mocks(monkeypatch, board_veps, update_calls, clear_calls):
    monkeypatch.setattr(board_module, "_resolve_board_number", lambda version: 22)
    monkeypatch.setattr(
        board_module,
        "get_project_field_metadata",
        lambda board_number: {"project_id": PROJECT_ID, "fields": FIELDS_META},
    )
    monkeypatch.setattr(
        board_module,
        "create_indexed_context",
        lambda cache_max_age_minutes=60: {"board_veps": board_veps},
    )

    def _fake_update(project_id, item_id, field_updates, field_metadata):
        update_calls.append((project_id, item_id, dict(field_updates)))
        return len(field_updates)

    def _fake_clear(project_id, item_id, field_names, field_metadata):
        clear_calls.append((project_id, item_id, list(field_names)))
        return len(field_names)

    monkeypatch.setattr(board_module, "update_project_item_fields", _fake_update)
    monkeypatch.setattr(board_module, "clear_project_item_fields", _fake_clear)


def _board_item(item_id):
    return {
        "item_id": item_id,
        "title": "some VEP",
        "url": "https://github.com/kubevirt/enhancements/issues/1",
        "state": "open",
        "body": "",
        "fields": {},
    }


def test_in_scope_item_written_out_of_scope_item_cleared(monkeypatch):
    board_veps = {
        100: _board_item("ITEM_100"),  # in scope
        323: _board_item("ITEM_323"),  # out of scope (e.g. Removed from Milestone)
    }
    update_calls = []
    clear_calls = []
    _install_common_mocks(monkeypatch, board_veps, update_calls, clear_calls)

    state = {
        "current_release": "v1.10",
        "vep_summary_table": [
            {
                "vep_number": 100,
                "urgency": "GREEN",
                "status_comment": "On track",
                "proposal_prs": [],
                "impl_prs": [],
            }
        ],
    }

    board_module.update_project_board_node(state)

    # In-scope item: normal write, all 4 fields, nothing else.
    assert len(update_calls) == 1
    _, item_id, field_updates = update_calls[0]
    assert item_id == "ITEM_100"
    assert set(field_updates.keys()) == {
        "Agent Urgency", "Agent Comment", "Proposal PRs", "Impl PRs",
    }
    assert field_updates["Agent Urgency"] == "GREEN"

    # Out-of-scope item: cleared, exactly the 4 agent-owned fields, no "Status".
    assert len(clear_calls) == 1
    _, item_id, field_names = clear_calls[0]
    assert item_id == "ITEM_323"
    assert set(field_names) == {
        "Agent Urgency", "Agent Comment", "Proposal PRs", "Impl PRs",
    }
    assert "Status" not in field_names


def test_multiple_out_of_scope_items_all_cleared(monkeypatch):
    board_veps = {
        1: _board_item("ITEM_1"),
        2: _board_item("ITEM_2"),  # Complete
        3: _board_item("ITEM_3"),  # next-cycle-only
    }
    update_calls = []
    clear_calls = []
    _install_common_mocks(monkeypatch, board_veps, update_calls, clear_calls)

    state = {
        "current_release": "v1.10",
        "vep_summary_table": [
            {
                "vep_number": 1,
                "urgency": "RED",
                "status_comment": "Needs attention",
                "proposal_prs": [],
                "impl_prs": [],
            }
        ],
    }

    board_module.update_project_board_node(state)

    assert len(update_calls) == 1
    cleared_item_ids = {call[1] for call in clear_calls}
    assert cleared_item_ids == {"ITEM_2", "ITEM_3"}
    for _, _, field_names in clear_calls:
        assert set(field_names) == {
            "Agent Urgency", "Agent Comment", "Proposal PRs", "Impl PRs",
        }


def test_out_of_scope_item_without_item_id_is_skipped_not_errored(monkeypatch):
    board_veps = {
        1: _board_item("ITEM_1"),
        2: {**_board_item("ITEM_2"), "item_id": None},  # no item_id -> can't clear
    }
    update_calls = []
    clear_calls = []
    _install_common_mocks(monkeypatch, board_veps, update_calls, clear_calls)

    state = {
        "current_release": "v1.10",
        "vep_summary_table": [
            {
                "vep_number": 1,
                "urgency": "GREEN",
                "status_comment": "ok",
                "proposal_prs": [],
                "impl_prs": [],
            }
        ],
    }

    # Should not raise despite the missing item_id.
    board_module.update_project_board_node(state)

    assert clear_calls == []


def test_string_keyed_board_veps_match_int_vep_numbers(monkeypatch):
    # After a JSON cache round-trip, board_veps keys become strings.
    board_veps = {
        "100": _board_item("ITEM_100"),
        "323": _board_item("ITEM_323"),
    }
    update_calls = []
    clear_calls = []
    _install_common_mocks(monkeypatch, board_veps, update_calls, clear_calls)

    state = {
        "current_release": "v1.10",
        "vep_summary_table": [
            {
                "vep_number": 100,
                "urgency": "YELLOW",
                "status_comment": "watching",
                "proposal_prs": [],
                "impl_prs": [],
            }
        ],
    }

    board_module.update_project_board_node(state)

    assert len(update_calls) == 1
    assert update_calls[0][1] == "ITEM_100"
    assert len(clear_calls) == 1
    assert clear_calls[0][1] == "ITEM_323"


def test_skip_update_board_flag_short_circuits(monkeypatch):
    update_calls = []
    clear_calls = []
    _install_common_mocks(monkeypatch, {}, update_calls, clear_calls)

    state = {"skip_update_board": True, "vep_summary_table": [{"vep_number": 1}]}
    board_module.update_project_board_node(state)

    assert update_calls == []
    assert clear_calls == []
