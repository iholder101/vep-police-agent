"""Pydantic response models for LLM structured output."""

from typing import List, Dict, Any
from pydantic import BaseModel
from state import VEPInfo


class CheckResponse(BaseModel):
    """Base response model for all check nodes."""
    updated_veps: List[VEPInfo]  # Full updated VEP objects
    alerts: List[Dict[str, Any]]  # Alerts generated during the check
