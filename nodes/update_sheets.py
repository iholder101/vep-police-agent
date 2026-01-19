"""Update sheets node - syncs state to Google Sheets using LLM with MCP tools."""

import json
from datetime import datetime
from typing import Any, List, Dict, Optional
from pydantic import BaseModel
from state import VEPState
from services.utils import log
from services.llm_helper import invoke_llm_with_tools, NoToolsCalledException


class UpdateSheetsResponse(BaseModel):
    """Response model for sheet update operation."""
    success: bool = False  # Whether the update was successful
    sheet_id: Optional[str] = None  # The sheet ID that was updated/created
    table_schema: Optional[List[Dict[str, str]]] = None  # The schema/columns decided by LLM (renamed from 'schema' to avoid shadowing BaseModel.schema)
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

    # Check for force flag (set by scheduler on first cache cycle, cleared after use)
    force_sheets_update = state.get("_force_sheets_update", False)
    if force_sheets_update:
        sheets_need_update = True

    skip_sheets = state.get("skip_sheets", False)
    
    # Check if sheets should be skipped
    if skip_sheets:
        log("Skip-sheets mode enabled, skipping Google Sheets update", node="update_sheets")
        last_check_times = state.get("last_check_times", {})
        last_check_times["update_sheets"] = datetime.now()
        next_tasks = state.get("next_tasks", [])
        if next_tasks and next_tasks[0] == "update_sheets":
            next_tasks = next_tasks[1:]
        return {
            "last_check_times": last_check_times,
            "sheets_need_update": False,
            "next_tasks": next_tasks,
        }
    
    # Log sheet URL if already configured
    existing_sheet_id = sheet_config.get("sheet_id")
    if existing_sheet_id:
        sheet_url = f"https://docs.google.com/spreadsheets/d/{existing_sheet_id}/edit"
        log(f"Updating Google Sheets | VEPs: {len(veps)} | Need update: {sheets_need_update} | Sheet URL: {sheet_url}", node="update_sheets")
    else:
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
            "_force_sheets_update": False,  # Clear force flag
            "next_tasks": next_tasks,
        }
    
    if not veps:
        log("No VEPs to sync to sheets", node="update_sheets")
        # Signal scheduler to fetch VEPs
        if "fetch_veps" not in next_tasks:
            next_tasks.append("fetch_veps")
        return {
            "last_check_times": last_check_times,
            "sheets_need_update": False,
            "_force_sheets_update": False,  # Clear force flag
            "next_tasks": next_tasks,  # Signal to fetch VEPs
        }
    
    # Build system prompt
    system_prompt = """You are a VEP governance agent syncing VEP data to Google Sheets.

CRITICAL: You MUST use the provided Google Sheets tools to write data. Do NOT skip tool calls or assume the sheet is already updated. You MUST call write_range to write the data.

REQUIREMENTS:
1. ONE ROW PER VEP - no skipping, filtering, or excluding
2. Column A = "VEP ID" containing tracking_issue_id (GitHub issue number)
3. Row count (excluding header) must equal VEP count

SCHEMA: Design columns for stakeholders - include VEP ID, name, title, owner, status, compliance, activity, deadlines.

WORKFLOW (you MUST execute all these steps using tools):
1. Verify access: get_spreadsheet(spreadsheetId) - if "not found", return error (needs sharing)
2. Read existing: get_sheet_data or read_range
3. Write all data: write_range with header row + all VEP rows - THIS IS REQUIRED
4. Format as proper table:
   - format_cells: header bold + gray background (range "Sheet1!A1:N1", format: {"textFormat":{"bold":true},"backgroundColor":{"red":0.9,"green":0.9,"blue":0.9}})
   - freeze_rows: frozenRowCount=1
   - create_filter: range covering header + all data rows

ERROR HANDLING:
- "Requested entity was not found" → spreadsheet not shared with service account
- "Drive storage quota exceeded" → use existing spreadsheet

Return: table_schema, sheet_id, rows_updated, rows_added."""
    
    # Prepare simplified VEP data for the sheet (essential fields + analysis from analyze_combined)
    simplified_veps = []
    for vep in veps:
        simplified_veps.append({
            "tracking_issue_id": vep.tracking_issue_id,
            "name": vep.name,
            "title": vep.title,
            "owner": vep.owner,
            "owning_sig": vep.owning_sig,
            "status": vep.status,
            "target_release": vep.target_release,
            "last_updated": str(vep.last_updated),
            "compliance": {
                "template_complete": vep.compliance.template_complete,
                "all_sigs_signed_off": vep.compliance.all_sigs_signed_off,
                "vep_merged": vep.compliance.vep_merged,
                "prs_linked": vep.compliance.prs_linked,
                "docs_pr_created": vep.compliance.docs_pr_created,
            },
            "activity_days_since_update": vep.activity.days_since_update,
            "milestone_status": vep.current_milestone.status if vep.current_milestone else None,
            # Analysis from analyze_combined - important for sheet columns
            "analysis": vep.analysis if vep.analysis else {},
        })

    context = {
        "veps": simplified_veps,
        "sheet_config": sheet_config,
        "current_release": state.get("current_release"),
    }
    
    vep_count = len(veps)
    sheet_id = sheet_config.get('sheet_id', 'NOT PROVIDED')

    user_prompt = f"""SHEET ID: {sheet_id}
VEP COUNT: {vep_count}

ACTION REQUIRED: Call the write_range tool NOW to write this data to the sheet.

VEP DATA TO WRITE:
{json.dumps(simplified_veps, indent=2, default=str)}

EXECUTE THESE TOOL CALLS IN ORDER:
1. get_spreadsheet(spreadsheetId="{sheet_id}") - verify access
2. write_range(spreadsheetId="{sheet_id}", range="Sheet1!A1", values=[header_row, ...data_rows...]) - write all {vep_count} VEPs
3. format_cells - make header bold with gray background
4. freeze_rows - freeze header row
5. create_filter - add filters

START BY CALLING get_spreadsheet NOW."""
    
    # Invoke LLM with Google Sheets MCP tools
    # Note: If Google Sheets MCP is not available, this will fail gracefully
    try:
        result = invoke_llm_with_tools(
            "update_sheets",
            context,
            system_prompt,
            user_prompt,
            UpdateSheetsResponse,
            mcp_names=("google-sheets",),
            require_tools=True
        )
        
        # Check if result is valid (not an error response)
        if not result:
            # If result is None/empty, MCP likely failed to load
            log("Google Sheets MCP not available - skipping sheet update. This is expected if mcp-google-sheets package is not installed or credentials are missing.", node="update_sheets", level="WARNING")
            return {
                "last_check_times": last_check_times,
                "sheets_need_update": False,  # Clear flag to prevent infinite retries
                "next_tasks": next_tasks,
            }
        
        # If result exists but success=False and no sheet_id, the operation failed
        if hasattr(result, 'success') and not result.success and not result.sheet_id:
            # MCP loaded but operation failed (likely auth/permissions issue)
            error_msg = f"Google Sheets update failed: {', '.join(result.errors) if hasattr(result, 'errors') and result.errors else 'Unknown error'}"
            log(error_msg, node="update_sheets", level="WARNING")
            
            # Check if it's a permission/API/quota error - don't retry indefinitely
            error_text = error_msg.lower()
            if ("insufficient permission" in error_text or "permission denied" in error_text or 
                "api has not been used" in error_text or "api.*disabled" in error_text or
                "enable it by visiting" in error_text or "quota has been exceeded" in error_text or
                "storage quota" in error_text):
                log("API/permission/quota error detected - clearing sheets_need_update flag to prevent infinite retries. Please check Google Cloud APIs, permissions, and Drive storage quota.", node="update_sheets", level="WARNING")
                return {
                    "last_check_times": last_check_times,
                    "sheets_need_update": False,  # Clear flag for API/permission/quota errors
                    "next_tasks": next_tasks,
                }
            
            # For other errors, keep retrying (might be transient)
            return {
                "last_check_times": last_check_times,
                "sheets_need_update": True,  # Keep flag set for retry
                "next_tasks": next_tasks,
            }
        
        if result.success:
            log(f"Successfully updated Google Sheets | Sheet ID: {result.sheet_id} | Rows updated: {result.rows_updated} | Rows added: {result.rows_added}", node="update_sheets")
            
            # Update sheet_config with the sheet_id if it was created/used
            if result.sheet_id:
                sheet_config = sheet_config.copy() if sheet_config else {}
                previous_sheet_id = sheet_config.get("sheet_id")
                sheet_config["sheet_id"] = result.sheet_id
                if result.table_schema:
                    sheet_config["schema"] = result.table_schema
                
                # Log the sheet URL when sheet_id is set or changed
                sheet_url = f"https://docs.google.com/spreadsheets/d/{result.sheet_id}/edit"
                if previous_sheet_id != result.sheet_id:
                    if previous_sheet_id:
                        log(f"✓ Sheet URL updated: {sheet_url}", node="update_sheets")
                    else:
                        log(f"✓ Sheet created! URL: {sheet_url}", node="update_sheets")
                elif not previous_sheet_id:
                    # First time setting sheet_id
                    log(f"✓ Sheet URL: {sheet_url}", node="update_sheets")
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
        
        # Store success value before overwriting result with dict
        update_success = result.success if hasattr(result, 'success') else False
        
        result = {
            "last_check_times": last_check_times,
            "sheets_need_update": False,  # Clear flag after successful update
            "_force_sheets_update": False,  # Clear force flag
            "next_tasks": next_tasks,
            "sheet_config": sheet_config,
        }
        
        # Check if one-cycle mode is enabled - exit after sheet update
        # But don't clear next_tasks yet - allow alert_summary to run first
        if state.get("one_cycle", False) and update_success:
            log("One-cycle mode: Sheet update successful, will exit after alert_summary completes", node="update_sheets")
            # Don't clear next_tasks - let alert_summary run first
            # Set a flag to signal main loop to exit after alert_summary
            result["_exit_after_sheets"] = True
        
        # Check if test-sheets debug mode is enabled - exit after sheet update
        import os
        debug_mode = os.environ.get("DEBUG_MODE")
        if debug_mode == "test-sheets" and update_success:
            log("Debug mode 'test-sheets': Sheet update successful, setting exit flag", node="update_sheets")
            # Clear next_tasks to prevent further execution
            result["next_tasks"] = []
            # Set a flag to signal main loop to exit
            result["_exit_after_sheets"] = True
        
        return result

    except NoToolsCalledException as e:
        # LLM didn't call any tools - this means the sheet wasn't actually updated
        log(f"Sheet update failed: LLM did not call any tools (likely hallucination)", node="update_sheets", level="ERROR")

        errors = state.get("errors", [])
        errors.append({
            "node": "update_sheets",
            "error": str(e),
            "timestamp": datetime.now().isoformat(),
        })

        # Keep sheets_need_update=True to retry on next cycle
        return {
            "last_check_times": last_check_times,
            "sheets_need_update": True,
            "_force_sheets_update": False,
            "next_tasks": next_tasks,
            "errors": errors,
        }

    except Exception as e:
        log(f"Error updating Google Sheets: {e}", node="update_sheets", level="ERROR")
        import traceback
        log(f"Traceback: {traceback.format_exc()}", node="update_sheets", level="ERROR")

        # Check if this is a known MCP package issue
        error_str = str(e).lower()
        is_mcp_unavailable = (
            "404" in error_str or
            "not found" in error_str or
            "connection closed" in error_str or
            "@modelcontextprotocol/server-google-sheets" in error_str
        )

        # Log error to state
        errors = state.get("errors", [])
        errors.append({
            "node": "update_sheets",
            "error": str(e),
            "timestamp": datetime.now().isoformat(),
        })

        # If MCP is unavailable, clear the flag to prevent infinite retries
        # Otherwise, keep flag set for transient errors
        return {
            "last_check_times": last_check_times,
            "sheets_need_update": False if is_mcp_unavailable else True,
            "_force_sheets_update": False,  # Clear force flag
            "next_tasks": next_tasks,
            "errors": errors,
        }
