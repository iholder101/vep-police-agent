"""Update sheets node - syncs state to Google Sheets using LLM with MCP tools."""

import json
from datetime import datetime
from typing import Any, List, Dict, Optional
from pydantic import BaseModel
from state import VEPState
from services.utils import log
from services.llm_helper import invoke_llm_with_tools


class UpdateSheetsResponse(BaseModel):
    """Response model for sheet update operation."""
    success: bool  # Whether the update was successful
    sheet_id: Optional[str] = None  # The sheet ID that was updated/created
    schema: Optional[List[Dict[str, str]]] = None  # The schema/columns decided by LLM
    rows_updated: int = 0  # Number of rows updated
    rows_added: int = 0  # Number of rows added
    errors: List[str] = []  # Any errors encountered


def update_sheets_node(state: VEPState) -> Any:
    """Update Google Sheets with current VEP state using LLM with Google Sheets MCP tools.
    
    This node delegates all sheet operations to the LLM:
    1. LLM decides on the table schema/columns based on VEP data
    2. LLM reads current sheet state (if sheet exists)
    3. LLM compares with graph state
    4. LLM updates the sheet (creates if needed, updates existing)
    
    The agent maintains the same sheet and updates it when needed.
    """
    veps = state.get("veps", [])
    sheets_need_update = state.get("sheets_need_update", False)
    sheet_config = state.get("sheet_config", {})
    
    log(f"Updating Google Sheets | VEPs: {len(veps)} | Need update: {sheets_need_update}", node="update_sheets")
    
    last_check_times = state.get("last_check_times", {})
    last_check_times["update_sheets"] = datetime.now()
    
    # Remove current task from queue (it was just completed)
    next_tasks = state.get("next_tasks", [])
    if next_tasks and next_tasks[0] == "update_sheets":
        next_tasks = next_tasks[1:]
    
    if not sheets_need_update:
        log("Sheets update not needed, skipping", node="update_sheets")
        return {
            "last_check_times": last_check_times,
            "sheets_need_update": False,
            "next_tasks": next_tasks,
        }
    
    if not veps:
        log("No VEPs to sync to sheets", node="update_sheets")
        return {
            "last_check_times": last_check_times,
            "sheets_need_update": False,
            "next_tasks": next_tasks,
        }
    
    # Build system prompt
    system_prompt = """You are a VEP governance agent syncing VEP data to Google Sheets.

Your task:
1. Decide on the table schema/columns based on the VEP data structure:
   - Include key fields: VEP number, title, owner, status, compliance flags, activity metrics, deadlines, alerts
   - Make the schema comprehensive but readable
   - Consider what stakeholders need to see
2. Use Google Sheets MCP tools to:
   - Read the current sheet (if sheet_id is provided in config)
   - Create a new sheet if needed (if create_new is True or sheet doesn't exist)
   - Compare current sheet data with the VEP state
   - Update the sheet with new/changed VEP data
   - Maintain data integrity (don't lose existing data)
3. Handle the sheet configuration:
   - sheet_id: The Google Sheets document ID (from URL: https://docs.google.com/spreadsheets/d/{sheet_id}/edit)
   - create_new: If True, create a new sheet; if False, update existing
   - sheet_name: Name for the sheet/tab within the document
4. Return the schema you decided on, the sheet_id used, and update statistics

Use the Google Sheets MCP tools to interact with the sheet. Read the current state first, then update as needed."""
    
    # Prepare context for LLM
    context = {
        "veps": [vep.model_dump(mode='json') for vep in veps],
        "sheet_config": sheet_config,
        "alerts": state.get("alerts", []),
        "current_release": state.get("current_release"),
    }
    
    user_prompt = f"""Here is the current VEP state and sheet configuration:

{json.dumps(context, indent=2, default=str)}

Sync this VEP data to Google Sheets. Decide on the schema, read the current sheet if it exists, and update it with the latest VEP information. If the sheet doesn't exist and create_new is True, create it."""
    
    # Invoke LLM with Google Sheets MCP tools
    try:
        result = invoke_llm_with_tools(
            "update_sheets",
            context,
            system_prompt,
            user_prompt,
            UpdateSheetsResponse,
            mcp_names=("google-sheets",)
        )
        
        if result.success:
            log(f"Successfully updated Google Sheets | Sheet ID: {result.sheet_id} | Rows updated: {result.rows_updated} | Rows added: {result.rows_added}", node="update_sheets")
            
            # Update sheet_config with the sheet_id if it was created/used
            if result.sheet_id:
                sheet_config = sheet_config.copy() if sheet_config else {}
                sheet_config["sheet_id"] = result.sheet_id
                if result.schema:
                    sheet_config["schema"] = result.schema
        else:
            log(f"Sheet update had errors: {result.errors}", node="update_sheets", level="WARNING")
            
            # Log errors to state
            errors = state.get("errors", [])
            for error_msg in result.errors:
                errors.append({
                    "node": "update_sheets",
                    "error": error_msg,
                    "timestamp": datetime.now().isoformat(),
                })
            
            return {
                "last_check_times": last_check_times,
                "sheets_need_update": True,  # Keep flag set if update failed
                "next_tasks": next_tasks,
                "errors": errors,
                "sheet_config": sheet_config,
            }
        
        return {
            "last_check_times": last_check_times,
            "sheets_need_update": False,  # Clear flag after successful update
            "next_tasks": next_tasks,
            "sheet_config": sheet_config,
        }
        
    except Exception as e:
        log(f"Error updating Google Sheets: {e}", node="update_sheets", level="ERROR")
        import traceback
        log(f"Traceback: {traceback.format_exc()}", node="update_sheets", level="ERROR")
        
        # Log error to state
        errors = state.get("errors", [])
        errors.append({
            "node": "update_sheets",
            "error": str(e),
            "timestamp": datetime.now().isoformat(),
        })
        
        return {
            "last_check_times": last_check_times,
            "sheets_need_update": True,  # Keep flag set if update failed
            "next_tasks": next_tasks,
            "errors": errors,
        }
