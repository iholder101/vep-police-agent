"""Merge VEP context node - combines context from parallel fetch nodes.

This is a DETERMINISTIC merge node - no LLM needed.
It applies raw context data from each fetch node to VEP objects.
"""

from typing import Any

from services.utils import log
from state import VEPState


def merge_vep_updates_node(state: VEPState) -> Any:
    """Merge context data from parallel fetch nodes into VEPs.

    This node runs after all fetch nodes complete and deterministically
    applies their context data to VEP objects. No LLM is needed - this
    is a simple merge operation.

    Each fetch node stores data in vep_updates_by_check with format:
    {
        "check_<type>": {
            "context_field": "<field_name>",
            "updates": {tracking_issue_id: context_data}
        }
    }

    This node applies updates to vep.context.<field_name>.
    """
    veps = state.get("veps", [])
    vep_updates_by_check = state.get("vep_updates_by_check", {})

    if not vep_updates_by_check:
        log("No context updates to merge", node="merge_vep_updates")
        return {}

    if not veps:
        log("No VEPs to update", node="merge_vep_updates")
        return {"vep_updates_by_check": {}}

    log(f"Merging context from {len(vep_updates_by_check)} fetch node(s)", node="merge_vep_updates")

    # Build lookup for quick VEP access by tracking_issue_id
    vep_by_id = {vep.tracking_issue_id: vep for vep in veps}

    # Track merge stats
    updates_applied = 0

    # Apply updates from each fetch node
    for check_name, check_data in vep_updates_by_check.items():
        context_field = check_data.get("context_field")
        updates = check_data.get("updates", {})

        if not context_field or not updates:
            log(f"Skipping {check_name}: missing context_field or updates", node="merge_vep_updates", level="DEBUG")
            continue

        for vep_id, context_data in updates.items():
            # Handle both int and string keys (JSON may stringify)
            vep_id_int = int(vep_id) if isinstance(vep_id, str) else vep_id
            vep = vep_by_id.get(vep_id_int)

            if not vep:
                log(f"VEP {vep_id} not found, skipping", node="merge_vep_updates", level="DEBUG")
                continue

            # Apply context data to the appropriate field
            if hasattr(vep.context, context_field):
                setattr(vep.context, context_field, context_data)
                updates_applied += 1
                log(f"Applied {context_field} context to VEP {vep.name}", node="merge_vep_updates", level="DEBUG")
            else:
                log(f"Unknown context field: {context_field}", node="merge_vep_updates", level="WARNING")

    log(f"Applied {updates_applied} context update(s) to {len(veps)} VEP(s)", node="merge_vep_updates")

    return {
        "veps": veps,
        "vep_updates_by_check": {},  # Clear after merging
    }
