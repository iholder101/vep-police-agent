"""Regression tests for scheduler starvation of update_project_board/alert_summary.

Bug: the graph router runs only next_tasks[0] per tick, then scheduler_node
rebuilds next_tasks from scratch. update_sheets used to be appended before
board/alerts and was re-queued unconditionally whenever sheets_need_update was
set - a persistently-failing sheets write kept it at index 0 forever, starving
update_project_board and alert_summary. The fix reorders the queue (board,
alerts, then sheets) and gates update_sheets to once per analyze_combined
epoch.
"""

from datetime import UTC, datetime, timedelta

from nodes.scheduler import scheduler_node

NOW = datetime.now(UTC)
ANALYZE_TIME = NOW - timedelta(minutes=5)
FETCH_TIME = ANALYZE_TIME - timedelta(minutes=10)  # fetch ran before analyze -> no re-analysis needed


def _base_state(**overrides):
    state = {
        "immediate_start": True,
        "veps": [{"number": 1}],
        "last_check_times": {
            "fetch_veps": FETCH_TIME,
            "analyze_combined": ANALYZE_TIME,
        },
    }
    state.update(overrides)
    return state


def test_board_scheduled_before_sheets_when_sheets_pending():
    state = _base_state(sheets_need_update=True)
    result = scheduler_node(state)
    next_tasks = result["next_tasks"]

    assert "update_project_board" in next_tasks
    assert "alert_summary" in next_tasks
    assert "update_sheets" in next_tasks
    assert next_tasks.index("update_project_board") < next_tasks.index("update_sheets")
    assert next_tasks.index("alert_summary") < next_tasks.index("update_sheets")


def test_failing_sheets_does_not_requeue_after_it_ran_this_epoch():
    last_check_times = {
        "fetch_veps": FETCH_TIME,
        "analyze_combined": ANALYZE_TIME,
        "update_sheets": ANALYZE_TIME + timedelta(minutes=1),
        "update_project_board": ANALYZE_TIME + timedelta(minutes=1),
        "alert_summary": ANALYZE_TIME + timedelta(minutes=1),
    }
    state = _base_state(last_check_times=last_check_times, sheets_need_update=True)
    result = scheduler_node(state)
    next_tasks = result["next_tasks"]

    assert "update_sheets" not in next_tasks


def test_sheets_scheduled_once_per_epoch_when_not_yet_run():
    last_check_times = {
        "fetch_veps": FETCH_TIME,
        "analyze_combined": ANALYZE_TIME,
        "update_project_board": ANALYZE_TIME + timedelta(minutes=1),
        "alert_summary": ANALYZE_TIME + timedelta(minutes=1),
    }
    state = _base_state(last_check_times=last_check_times)
    result = scheduler_node(state)
    next_tasks = result["next_tasks"]

    assert "update_sheets" in next_tasks


def test_board_not_scheduled_when_skip_update_board():
    state = _base_state(sheets_need_update=True, skip_update_board=True)
    result = scheduler_node(state)
    next_tasks = result["next_tasks"]

    assert "update_project_board" not in next_tasks
    assert "update_sheets" in next_tasks
    assert "alert_summary" in next_tasks


def test_skip_monitoring_uses_flag_path():
    last_check_times = {
        "fetch_veps": FETCH_TIME,
    }
    state = _base_state(
        last_check_times=last_check_times,
        skip_monitoring=True,
        sheets_need_update=True,
    )
    result = scheduler_node(state)
    next_tasks = result["next_tasks"]

    assert "update_sheets" in next_tasks
