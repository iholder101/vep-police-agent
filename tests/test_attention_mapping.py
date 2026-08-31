"""Unit tests for the WS4 attention contract - replaces merge-probability.

Covers: urgency/color mapping, status comment rendering, the deterministic
prefill/shortcut layers in analyze_combined, snapshot recording + level-
transition anomaly detection, and VEPAttention validation.
"""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from nodes.alert_formatting import get_urgency_level, status_comment
from nodes.analyze_combined import (
    _apply_all_merged_shortcut,
    _prefill_phase_risk_attention,
)
from nodes.snapshot import _build_vep_record, _diff_snapshots
from services.response_models import AttentionLevel, VEPAttention


def _attention(level, reasons=None, health_summary="summary", is_stale=False, phase="development"):
    return {
        "attention_level": level,
        "attention_reasons": reasons or [],
        "health_summary": health_summary,
        "compliance_flags": [],
        "suggested_action": None,
        "staleness": {"days_since_human_activity": 10 if is_stale else 1, "is_stale": is_stale, "stale_reason": None},
        "phase": phase,
    }


def _vep(analysis=None, **kwargs):
    defaults = {
        "tracking_issue_id": 1,
        "name": "vep-0001",
        "title": "Example VEP",
        "owner": "owner1",
        "owning_sig": "compute",
        "target_release": "v1.10",
        "analysis": analysis,
        "implementation_prs": [],
        "enhancement_prs": [],
        "compliance": SimpleNamespace(
            vep_merged=False, template_complete=True, all_sigs_signed_off=True,
            prs_linked=True, docs_pr_created=True, labels_valid=True,
        ),
        "activity": SimpleNamespace(days_since_update=1, review_lag_days=None),
        "context": SimpleNamespace(deadline={}),
        "current_milestone": SimpleNamespace(promotion_phase="Net New", status="Tracked"),
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


class TestGetUrgencyLevel:
    def test_needs_attention_is_red(self):
        vep = _vep(analysis={"attention": _attention("needs_attention")})
        assert get_urgency_level(vep) == ("RED", "red")

    def test_watch_is_yellow(self):
        vep = _vep(analysis={"attention": _attention("watch")})
        assert get_urgency_level(vep) == ("YELLOW", "orange")

    def test_ok_is_green(self):
        vep = _vep(analysis={"attention": _attention("ok")})
        assert get_urgency_level(vep) == ("GREEN", "green")

    def test_no_analysis_is_unknown(self):
        vep = _vep(analysis=None)
        assert get_urgency_level(vep) == ("UNKNOWN", "gray")

    def test_no_attention_key_is_ok_green(self):
        vep = _vep(analysis={"combined_insights": "something"})
        assert get_urgency_level(vep) == ("OK", "green")


class TestStatusComment:
    def test_renders_health_summary_and_top_reason(self):
        vep = _vep(analysis={"attention": _attention(
            "needs_attention",
            reasons=[{"kind": "activity", "text": "Stale for 15 days."}],
            health_summary="Proposal PR stalled.",
        )})
        comment = status_comment(vep)
        assert "Proposal PR stalled." in comment
        assert "Stale for 15 days." in comment

    def test_appends_stale_marker(self):
        vep = _vep(analysis={"attention": _attention("needs_attention", is_stale=True)})
        assert status_comment(vep).endswith("[STALE]")

    def test_no_stale_marker_when_not_stale(self):
        vep = _vep(analysis={"attention": _attention("ok", is_stale=False)})
        assert "[STALE]" not in status_comment(vep)

    def test_no_analysis(self):
        vep = _vep(analysis=None)
        assert status_comment(vep) == "No analysis available"

    def test_no_attention(self):
        vep = _vep(analysis={"combined_insights": "something"})
        assert status_comment(vep) == "No attention assessment"


class TestAllMergedShortcut:
    def test_all_merged_and_past_freeze_forces_ok(self):
        vep = _vep(
            analysis={"attention": _attention("needs_attention")},
            implementation_prs=[SimpleNamespace(state="merged"), SimpleNamespace(state="closed")],
            compliance=SimpleNamespace(vep_merged=True),
            context=SimpleNamespace(deadline={"ef_passed": True, "cf_passed": True}),
        )
        _apply_all_merged_shortcut(vep, "development", None)
        assert vep.analysis["attention"]["attention_level"] == "ok"

    def test_open_pr_does_not_trigger_shortcut(self):
        original = _attention("needs_attention")
        vep = _vep(
            analysis={"attention": original},
            implementation_prs=[SimpleNamespace(state="open")],
        )
        _apply_all_merged_shortcut(vep, "development", None)
        assert vep.analysis["attention"]["attention_level"] == "needs_attention"

    def test_design_phase_is_noop(self):
        vep = _vep(
            analysis={"attention": _attention("needs_attention")},
            implementation_prs=[SimpleNamespace(state="merged")],
        )
        _apply_all_merged_shortcut(vep, "design", None)
        assert vep.analysis["attention"]["attention_level"] == "needs_attention"

    def test_no_impl_prs_is_noop(self):
        vep = _vep(analysis={"attention": _attention("watch")}, implementation_prs=[])
        _apply_all_merged_shortcut(vep, "development", None)
        assert vep.analysis["attention"]["attention_level"] == "watch"


class TestPrefillPhaseRiskAttention:
    def test_stale_proposal_near_ef_is_needs_attention_with_temporal_reason(self):
        vep = _vep(tracking_issue_id=42, name="vep-0042", analysis=None)
        phase_risks = [{
            "has_risks": True,
            "vep_id": 42,
            "vep_name": "vep-0042",
            "phase": "design",
            "proposal_pr": {"number": 100, "review_count": 1, "days_since_update": 15},
            "stale_impl_prs": [],
            "days_to_deadline": 5,
        }]

        prefilled = _prefill_phase_risk_attention([vep], phase_risks, "design", {"fraction_through_phase": 0.9})

        assert prefilled == {"vep-0042"}
        attention = vep.analysis["attention"]
        assert attention["attention_level"] == "needs_attention"
        assert vep.analysis["_deterministic_attention"] is True
        assert any(r["kind"] == "temporal" and "EF deadline" in r["text"] for r in attention["attention_reasons"])

    def test_low_risk_proposal_merged_is_skipped(self):
        vep = _vep(tracking_issue_id=7, name="vep-0007", analysis=None)
        phase_risks = [{
            "has_risks": True,
            "vep_id": 7,
            "vep_name": "vep-0007",
            "phase": "development",
            "proposal_merged": True,
            "risk_level": "low",
            "days_to_deadline": 60,
        }]

        prefilled = _prefill_phase_risk_attention([vep], phase_risks, "development", {"fraction_through_phase": 0.1})

        assert prefilled == set()
        assert vep.analysis is None

    def test_proposal_merged_late_in_phase_is_needs_attention(self):
        vep = _vep(tracking_issue_id=8, name="vep-0008", analysis=None)
        phase_risks = [{
            "has_risks": True,
            "vep_id": 8,
            "vep_name": "vep-0008",
            "phase": "development",
            "proposal_merged": True,
            "risk_level": "medium",
            "days_to_deadline": 3,
        }]

        _prefill_phase_risk_attention([vep], phase_risks, "development", {"fraction_through_phase": 0.9})

        assert vep.analysis["attention"]["attention_level"] == "needs_attention"

    def test_proposal_merged_early_in_phase_is_watch(self):
        vep = _vep(tracking_issue_id=9, name="vep-0009", analysis=None)
        phase_risks = [{
            "has_risks": True,
            "vep_id": 9,
            "vep_name": "vep-0009",
            "phase": "development",
            "proposal_merged": True,
            "risk_level": "medium",
            "days_to_deadline": 40,
        }]

        _prefill_phase_risk_attention([vep], phase_risks, "development", {"fraction_through_phase": 0.2})

        assert vep.analysis["attention"]["attention_level"] == "watch"


class TestSnapshotRecordAndCompare:
    def test_record_has_attention_level_and_staleness(self):
        vep = _vep(analysis={"attention": _attention("watch", is_stale=True)})
        record = _build_vep_record(vep, {}, {}, None, None, datetime.now(UTC))
        assert record["attention_level"] == "watch"
        assert record["is_stale"] is True

    def test_compare_detects_ok_to_needs_attention_regression(self):
        previous = {"veps": [{"vep_number": 7, "attention_level": "ok", "compliance": {}}]}
        current = {"veps": [{"vep_number": 7, "attention_level": "needs_attention", "compliance": {}}]}

        changes, anomalies = _diff_snapshots(previous, current)

        assert any("attention_level ok -> needs_attention" in c for c in changes)
        assert any("attention regressed ok -> needs_attention" in a for a in anomalies)

    def test_compare_does_not_flag_improvement_as_anomaly(self):
        previous = {"veps": [{"vep_number": 7, "attention_level": "needs_attention", "compliance": {}}]}
        current = {"veps": [{"vep_number": 7, "attention_level": "ok", "compliance": {}}]}

        _changes, anomalies = _diff_snapshots(previous, current)

        assert not any("attention regressed" in a for a in anomalies)


class TestVEPAttentionValidation:
    def test_round_trip(self):
        data = {
            "attention_level": "needs_attention",
            "attention_reasons": [{"kind": "temporal", "text": "5 days to freeze."}],
            "health_summary": "Stalled proposal.",
            "compliance_flags": [],
            "suggested_action": "Ping reviewers.",
            "staleness": {"days_since_human_activity": 10, "is_stale": True, "stale_reason": "No activity for 10 days"},
            "phase": "design",
        }

        model = VEPAttention.model_validate(data)

        assert model.attention_level == AttentionLevel.NEEDS_ATTENTION
        dumped = model.model_dump(mode="json")
        assert dumped["attention_level"] == "needs_attention"
        assert dumped["attention_reasons"][0]["kind"] == "temporal"

    def test_rejects_bad_level(self):
        data = {
            "attention_level": "definitely_not_a_level",
            "health_summary": "x",
            "phase": "design",
        }
        with pytest.raises(ValidationError):
            VEPAttention.model_validate(data)

    def test_defaults_are_applied(self):
        model = VEPAttention.model_validate({
            "attention_level": "ok",
            "health_summary": "All good.",
            "phase": "stabilization",
        })
        assert model.attention_reasons == []
        assert model.compliance_flags == []
        assert model.suggested_action is None
        assert model.staleness.is_stale is False
