"""Pydantic response models for LLM structured output."""

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field

from state import VEPInfo


class AttentionLevel(str, Enum):
    """Attention verdict for a VEP - replaces the old merge-probability score.

    The agent's job is to steer maintainer attention, not predict merge odds.
    """
    NEEDS_ATTENTION = "needs_attention"
    WATCH = "watch"
    OK = "ok"


class Staleness(BaseModel):
    """Grounded staleness signal, derived from PR conversation data when available."""
    days_since_human_activity: int | None = None
    is_stale: bool = False
    stale_reason: str | None = None


class AttentionReason(BaseModel):
    """A single concrete reason backing an attention verdict."""
    kind: Literal["temporal", "review", "coverage", "compliance", "activity", "board"]
    text: str


class VEPAttention(BaseModel):
    """Attention contract for a single VEP - stored under vep.analysis["attention"].

    `analysis` itself stays an untyped dict (to avoid rippling a typed field
    through fetch/merge nodes); this model is used to validate/coerce the
    "attention" sub-object after the LLM call (or when built deterministically).
    """
    attention_level: AttentionLevel
    attention_reasons: list[AttentionReason] = Field(default_factory=list)
    health_summary: str  # ONE sentence -> board Agent Comment
    compliance_flags: list[str] = Field(default_factory=list)  # WS6 fills; WS4 defines field
    suggested_action: str | None = None
    staleness: Staleness = Field(default_factory=Staleness)
    phase: str


class CheckResponse(BaseModel):
    """Base response model for all check nodes (legacy - used by analyze_combined)."""
    updated_veps: list[VEPInfo]  # Full updated VEP objects
    alerts: list[dict[str, Any]]  # Alerts generated during the check


class VEPContextUpdate(BaseModel):
    """Context update for a single VEP from a fetch node."""
    tracking_issue_id: int  # VEP identifier to match
    context_data: dict[str, Any]  # Raw context data to store in vep.context.<node_type>


class FetchResponse(BaseModel):
    """Response model for fetch nodes (lightweight, no analysis).

    Fetch nodes only gather raw data and store it in VEP context fields.
    No analysis is done - that's handled by analyze_combined with full context.
    """
    context_updates: list[VEPContextUpdate]  # Context data per VEP


class VEPAttentionUpdate(BaseModel):
    """A single VEP's attention verdict, keyed by tracking issue id.

    Lean alternative to echoing a full VEPInfo back from the LLM - the
    caller merges `attention` onto the existing in-memory VEP object.
    """
    tracking_issue_id: int
    attention: VEPAttention


class AnalyzeAttentionResponse(BaseModel):
    """Response model for analyze_combined - lean per-VEP attention only.

    Deeply nested response schemas (e.g. list[VEPInfo], with VEPInfo ->
    VEPMilestone/VEPCompliance/VEPActivity/VEPContext/PRInfo/IssueInfo) are
    rejected by Gemini's response_schema validation (400 INVALID_ARGUMENT).
    This model stays shallow: one attention verdict per VEP, matched back
    onto the existing VEP objects by tracking_issue_id.
    """
    analyses: list[VEPAttentionUpdate] = Field(default_factory=list)
    alerts: list[dict[str, Any]] = Field(default_factory=list)
    general_insights: list[str] = Field(default_factory=list)
    sheets_need_update: bool = False
