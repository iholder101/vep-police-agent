"""Pydantic response models for LLM structured output."""

from typing import Any

from pydantic import BaseModel

from state import VEPInfo


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
