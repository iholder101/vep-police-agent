"""Unit tests for analyze_combined's lean per-VEP attention merge.

Gemini rejects the deeply-nested list[VEPInfo] response schema that
analyze_combined used to request (400 INVALID_ARGUMENT), so analyze_combined
now asks the LLM for a lean AnalyzeAttentionResponse (tracking_issue_id +
attention per VEP) and merges it onto the existing in-memory VEP objects.

Covers: attention merged onto the correct original VEP by tracking_issue_id,
a deterministic-prefill VEP preserved against LLM overwrite, and a VEP the
LLM omitted getting fallback attention.
"""

from datetime import UTC, datetime
from unittest.mock import patch

from nodes.analyze_combined import analyze_combined_node
from services.response_models import (
    AnalyzeAttentionResponse,
    AttentionLevel,
    VEPAttention,
    VEPAttentionUpdate,
)
from state import VEPActivity, VEPCompliance, VEPInfo, VEPMilestone


def _make_vep(tracking_issue_id: int, name: str, deterministic: bool = False) -> VEPInfo:
    analysis = {}
    if deterministic:
        analysis = {
            "_deterministic_attention": True,
            "attention": VEPAttention(
                attention_level=AttentionLevel.NEEDS_ATTENTION,
                health_summary="Pre-assessed deterministically.",
                phase="development",
            ).model_dump(mode="json"),
        }
    return VEPInfo(
        tracking_issue_id=tracking_issue_id,
        name=name,
        title=f"{name} title",
        owner="owner1",
        owning_sig="compute",
        status="Tracked",
        last_updated=datetime.now(UTC),
        created_at=datetime.now(UTC),
        current_milestone=VEPMilestone(
            version="v1.10", status="Tracked", promotion_phase="Net New",
            exception_phase="None", target_stage="Beta", all_code_prs_merged=False,
        ),
        compliance=VEPCompliance(
            template_complete=True, all_sigs_signed_off=True, vep_merged=False,
            prs_linked=True, docs_pr_created=True, labels_valid=True,
        ),
        activity=VEPActivity(last_activity=datetime.now(UTC), days_since_update=1),
        tracking_issue=None,
        analysis=analysis,
    )


def _indexed_context():
    return {
        "release_phase": "development",
        "release_deadlines": {},
        "board_veps": {},
        "phase_detail": {"fraction_through_phase": 0.5},
        "cycle_start_date": None,
    }


def test_llm_attention_merged_onto_correct_vep_by_id():
    vep_a = _make_vep(1, "vep-0001")
    vep_b = _make_vep(2, "vep-0002")

    llm_response = AnalyzeAttentionResponse(
        analyses=[
            VEPAttentionUpdate(
                tracking_issue_id=1,
                attention=VEPAttention(
                    attention_level=AttentionLevel.WATCH,
                    health_summary="Watch vep-0001.",
                    phase="development",
                ),
            ),
            VEPAttentionUpdate(
                tracking_issue_id=2,
                attention=VEPAttention(
                    attention_level=AttentionLevel.OK,
                    health_summary="vep-0002 is fine.",
                    phase="development",
                ),
            ),
        ],
        alerts=[],
        general_insights=["all good"],
        sheets_need_update=False,
    )

    with patch("nodes.analyze_combined.create_indexed_context", return_value=_indexed_context()), \
         patch("nodes.analyze_combined.invoke_llm_check", return_value=llm_response):
        result = analyze_combined_node({"veps": [vep_a, vep_b], "last_check_times": {}})

    updated = {v.tracking_issue_id: v for v in result["veps"]}
    assert updated[1].analysis["attention"]["attention_level"] == "watch"
    assert updated[1].analysis["attention"]["health_summary"] == "Watch vep-0001."
    assert updated[2].analysis["attention"]["attention_level"] == "ok"


def test_deterministic_attention_is_preserved_against_llm_overwrite():
    vep = _make_vep(3, "vep-0003", deterministic=True)

    llm_response = AnalyzeAttentionResponse(
        analyses=[
            VEPAttentionUpdate(
                tracking_issue_id=3,
                attention=VEPAttention(
                    attention_level=AttentionLevel.OK,
                    health_summary="LLM thinks it's fine.",
                    phase="development",
                ),
            ),
        ],
        alerts=[],
        general_insights=[],
        sheets_need_update=False,
    )

    with patch("nodes.analyze_combined.create_indexed_context", return_value=_indexed_context()), \
         patch("nodes.analyze_combined.invoke_llm_check", return_value=llm_response):
        result = analyze_combined_node({"veps": [vep], "last_check_times": {}})

    updated = result["veps"][0]
    assert updated.analysis["attention"]["attention_level"] == "needs_attention"
    assert updated.analysis["attention"]["health_summary"] == "Pre-assessed deterministically."


def test_vep_missing_from_llm_response_gets_fallback_attention():
    vep = _make_vep(4, "vep-0004")

    llm_response = AnalyzeAttentionResponse(
        analyses=[],  # LLM produced nothing for this VEP
        alerts=[],
        general_insights=[],
        sheets_need_update=False,
    )

    with patch("nodes.analyze_combined.create_indexed_context", return_value=_indexed_context()), \
         patch("nodes.analyze_combined.invoke_llm_check", return_value=llm_response):
        result = analyze_combined_node({"veps": [vep], "last_check_times": {}})

    updated = result["veps"][0]
    # development phase, no implementation PRs, mid-phase (fraction 0.5 < 0.6) -> watch
    assert updated.analysis["attention"]["attention_level"] == "watch"
