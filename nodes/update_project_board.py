"""Update project board node - writes VEP summary data back to GitHub Project V2 fields.

Reads the vep_summary_table from state (built by analyze_combined) and writes
computed fields (Agent Urgency, Agent Comment, Proposal PRs, Impl PRs) back to
the GitHub Project V2 board via GraphQL mutations.

No LLM involved - pure data mapping and GraphQL.
"""

from datetime import datetime
from typing import Any

from state import VEPState
from services.utils import log
from services.indexer import create_indexed_context
from services.graphql_client import (
    get_project_field_metadata,
    update_project_item_fields,
)
from nodes.alert_formatting import format_pr_links_markdown


def _resolve_board_number(version: str) -> int | None:
    """Resolve the project board number for a release version.

    Uses the same resolution logic as index_project_board_items:
    auto-discovery first, then config mapping fallback.

    Args:
        version: Release version string (e.g., "v1.8" or "1.8")

    Returns:
        Project board number or None
    """
    from services.graphql_client import find_project_by_title
    from config import get_project_board_for_version, board_search_patterns

    if not version:
        return None

    # Try auto-discovery by title matching (progressively looser patterns)
    for pattern in board_search_patterns(version):
        try:
            board_num = find_project_by_title(org_name="kubevirt", title_pattern=pattern)
            if board_num:
                return board_num
        except Exception:
            pass

    # Fallback to config mapping
    return get_project_board_for_version(version)


def update_project_board_node(state: VEPState) -> Any:
    """Write VEP summary data back to GitHub Project V2 board fields.

    For each VEP in the summary table, updates four text fields on the board:
    - Agent Urgency: RED / YELLOW / GREEN
    - Agent Comment: Status comment from risk assessment
    - Proposal PRs: Clickable markdown PR links (e.g., "[#215](url), [#220](url)")
    - Impl PRs: Clickable markdown PR links (e.g., "[#16909](url), [#16832](url)")

    Respects skip_update_board state flag.
    """
    last_check_times = state.get("last_check_times", {})

    if state.get("skip_update_board", False):
        log("skip_update_board enabled, skipping board update", node="update_board")
        last_check_times["update_project_board"] = datetime.now()
        return {"last_check_times": last_check_times}

    table_rows = state.get("vep_summary_table", [])
    if not table_rows:
        log("No vep_summary_table in state, skipping board update", node="update_board")
        last_check_times["update_project_board"] = datetime.now()
        return {"last_check_times": last_check_times}

    # Resolve the project board number from the current release version
    current_release = state.get("current_release")
    board_number = _resolve_board_number(current_release)
    if board_number is None:
        log(f"Cannot resolve project board for release '{current_release}', skipping board update",
            node="update_board", level="WARNING")
        last_check_times["update_project_board"] = datetime.now()
        return {"last_check_times": last_check_times}

    # Fetch field metadata (project ID + field IDs)
    metadata = get_project_field_metadata(board_number)
    if not metadata:
        log("Failed to fetch project field metadata, skipping board update",
            node="update_board", level="WARNING")
        last_check_times["update_project_board"] = datetime.now()
        return {"last_check_times": last_check_times}

    project_id = metadata["project_id"]
    fields_meta = metadata["fields"]

    # Verify the expected fields exist on the board
    expected_fields = ["Agent Urgency", "Agent Comment", "Proposal PRs", "Impl PRs"]
    # "VEP approver"/"VEP reviewer" are optional — only written when present on the board
    missing = [f for f in expected_fields if f not in fields_meta]
    if missing:
        log(f"Missing expected fields on project board: {missing}. "
            "Create them as Text fields on the board first.",
            node="update_board", level="WARNING")
        last_check_times["update_project_board"] = datetime.now()
        return {"last_check_times": last_check_times}

    # Get board_veps from indexed context for item_id lookup
    index_cache_minutes = state.get("index_cache_minutes", 60)
    indexed_context = create_indexed_context(cache_max_age_minutes=index_cache_minutes)
    board_veps = indexed_context.get("board_veps", {})

    updated = 0
    skipped = 0
    total = len(table_rows)

    for row in table_rows:
        vep_number = row.get("vep_number")
        # board_veps keys may be int (in-memory) or str (after JSON cache round-trip)
        board_vep = board_veps.get(vep_number) or board_veps.get(str(vep_number)) or {}
        item_id = board_vep.get("item_id")

        if not item_id:
            log(f"VEP #{vep_number}: no item_id found on board, skipping",
                node="update_board", level="WARNING")
            skipped += 1
            continue

        # Build field updates
        field_updates = {
            "Agent Urgency": row.get("urgency", ""),
            "Agent Comment": row.get("status_comment", ""),
            "Proposal PRs": format_pr_links_markdown(row.get("proposal_prs", [])),
            "Impl PRs": format_pr_links_markdown(row.get("impl_prs", [])),
        }

        # Write VEP approver/reviewer when the fields exist on the board
        if "VEP approver" in fields_meta:
            approvers = row.get("approvers", [])
            field_updates["VEP approver"] = ", ".join(f"@{u}" for u in approvers) if approvers else ""
        if "VEP reviewer" in fields_meta:
            reviewers = row.get("reviewers", [])
            field_updates["VEP reviewer"] = ", ".join(f"@{u}" for u in reviewers) if reviewers else ""

        count = update_project_item_fields(project_id, item_id, field_updates, fields_meta)
        if count > 0:
            updated += 1
        else:
            skipped += 1

    log(f"Board update complete: {updated}/{total} items updated, {skipped} skipped",
        node="update_board")

    last_check_times["update_project_board"] = datetime.now()
    return {"last_check_times": last_check_times}
