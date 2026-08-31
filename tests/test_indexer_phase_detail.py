"""Unit tests for services.indexer.compute_phase_detail.

Uses a realistic v1.10 schedule: cycle_start (previous release's Code
Freeze) = 2026-06-24, VEP/enhancement freeze = 2026-09-02, code freeze =
2026-10-21, GA = 2026-11-18. All expected day-counts are computed with real
date arithmetic in the assertions (not hardcoded), per test convention.
"""

from datetime import date

from services.indexer import compute_phase_detail

CYCLE_START = "2026-06-24"
EF = "2026-09-02"
CF = "2026-10-21"
GA = "2026-11-18"

DEADLINES = {"enhancement_freeze": EF, "code_freeze": CF, "ga": GA}


class TestComputePhaseDetail:
    def test_design_phase_mid(self):
        today = date(2026, 7, 24)
        detail = compute_phase_detail("design", DEADLINES, CYCLE_START, today=today)

        start = date(2026, 6, 24)
        end = date(2026, 9, 2)
        expected_days_into = (today - start).days
        expected_days_left = (end - today).days
        expected_fraction = round(expected_days_into / (end - start).days, 3)

        assert detail["phase_start"] == "2026-06-24"
        assert detail["phase_end"] == "2026-09-02"
        assert detail["days_into_phase"] == expected_days_into
        assert detail["days_left_in_phase"] == expected_days_left
        assert detail["fraction_through_phase"] == expected_fraction
        assert detail["next_freeze"] == {"name": "VEP Freeze", "date": "2026-09-02"}

    def test_development_phase(self):
        today = date(2026, 9, 20)
        detail = compute_phase_detail("development", DEADLINES, CYCLE_START, today=today)

        end = date(2026, 10, 21)
        expected_days_left = (end - today).days

        assert detail["phase_start"] == "2026-09-02"
        assert detail["phase_end"] == "2026-10-21"
        assert detail["next_freeze"] == {"name": "Code Freeze", "date": "2026-10-21"}
        assert detail["days_left_in_phase"] == expected_days_left
        assert 0.0 < detail["fraction_through_phase"] < 1.0

    def test_stabilization_phase(self):
        today = date(2026, 11, 1)
        detail = compute_phase_detail("stabilization", DEADLINES, CYCLE_START, today=today)

        start = date(2026, 10, 21)
        end = date(2026, 11, 18)

        assert detail["phase_start"] == "2026-10-21"
        assert detail["phase_end"] == "2026-11-18"
        assert detail["next_freeze"] == {"name": "GA", "date": "2026-11-18"}
        assert detail["days_left_in_phase"] == (end - today).days
        assert 0.0 < detail["fraction_through_phase"] < 1.0
        assert start < today < end

    def test_post_release_phase(self):
        today = date(2026, 12, 1)
        detail = compute_phase_detail("post_release", DEADLINES, CYCLE_START, today=today)

        ga_date = date(2026, 11, 18)

        assert detail["phase_start"] == "2026-11-18"
        assert detail["phase_end"] is None
        assert detail["days_into_phase"] == (today - ga_date).days
        assert detail["days_left_in_phase"] is None
        assert detail["fraction_through_phase"] is None
        assert detail["next_freeze"] is None

    def test_missing_enhancement_freeze(self):
        today = date(2026, 7, 24)
        deadlines = {"enhancement_freeze": None}
        detail = compute_phase_detail("design", deadlines, CYCLE_START, today=today)

        start = date(2026, 6, 24)

        assert detail["phase_start"] == "2026-06-24"
        assert detail["phase_end"] is None
        assert detail["days_left_in_phase"] is None
        assert detail["fraction_through_phase"] is None
        assert detail["next_freeze"] is None
        assert detail["days_into_phase"] == (today - start).days

    def test_missing_cycle_start(self):
        today = date(2026, 7, 24)
        detail = compute_phase_detail("design", DEADLINES, None, today=today)

        assert detail["phase_start"] is None
        assert detail["days_into_phase"] is None
        # phase_end still resolvable from enhancement_freeze
        assert detail["phase_end"] == "2026-09-02"

    def test_clamping_past_phase_end(self):
        today = date(2026, 9, 10)  # past EF (2026-09-02) but phase still "design"
        detail = compute_phase_detail("design", DEADLINES, CYCLE_START, today=today)

        assert detail["fraction_through_phase"] == 1.0
        assert detail["days_left_in_phase"] <= 0

    def test_default_today_does_not_raise(self):
        detail = compute_phase_detail("design", DEADLINES, CYCLE_START)

        assert detail["phase"] == "design"
        assert "days_into_phase" in detail
