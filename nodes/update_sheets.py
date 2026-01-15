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
            "next_tasks": next_tasks,  # Signal to fetch VEPs
        }
    
    # Build system prompt
    system_prompt = """You are a VEP governance agent syncing VEP data to Google Sheets.

CRITICAL REQUIREMENTS:
1. ONE ROW PER VEP: Each VEP in the "veps" array must appear as exactly ONE row in the sheet. Do not skip, filter, or exclude any VEPs. Every VEP must be written.
2. FIRST COLUMN IS VEP ID: The first column (column A) MUST be "VEP ID" and MUST contain the tracking_issue_id for each VEP. This is the GitHub issue number that tracks the VEP and is the primary identifier.
3. ROW COUNT VERIFICATION: After writing, the number of data rows (excluding header) must equal exactly the number of VEPs provided.

Your task:
1. Decide on the table schema/columns based on the VEP data structure:
   - FIRST COLUMN (A): "VEP ID" - MUST be tracking_issue_id (the GitHub issue number)
   - Include other key fields: VEP number/name, title, owner, status, compliance flags, activity metrics, deadlines, alerts
   - Make the schema comprehensive but readable
   - Consider what stakeholders need to see
2. Use Google Sheets MCP tools in this order:
   STEP 1: Verify spreadsheet access
   - If sheet_id is provided in config, use get_spreadsheet(spreadsheetId) to verify the spreadsheet exists and is accessible
   - If get_spreadsheet fails with "Requested entity was not found", the service account doesn't have access
     → You cannot proceed - return an error explaining the spreadsheet needs to be shared with the service account
   - If get_spreadsheet succeeds, proceed to STEP 2
   
   STEP 2: Read existing data (if any)
   - Use get_sheet_data(spreadsheetId, sheetName) or read_range(spreadsheetId, range) to read current data
   - This helps you understand the existing structure
   - If the sheet is empty or doesn't exist, you'll write all data fresh
   
   STEP 3: Write all VEP data
   - Use write_range(spreadsheetId, range, values) to write ALL VEPs as rows
   - Range should be "Sheet1!A1:Z{N}" where N is the number of rows (header + data rows)
   - First row is header: ["VEP ID", "Name", "Title", ...]
   - Each subsequent row is one VEP: [tracking_issue_id, name, title, ...]
   - IMPORTANT: Write ALL {vep_count} VEPs - every VEP must be a row
   - IMPORTANT: Column A (first column) MUST be "VEP ID" containing tracking_issue_id
   
   STEP 4: Format the table (see step 3 below)
   
   ERROR HANDLING:
   - If you get "Requested entity was not found" for get_spreadsheet: The service account doesn't have access to the spreadsheet. Return an error.
   - If you get "Drive storage quota exceeded": Cannot create new spreadsheets. Use existing shared spreadsheet.
   - Do NOT try to create a new spreadsheet if sheet_id is provided - use the existing one or return an error
3. CRITICAL: After writing data, you MUST create a proper Google Sheets table with these steps (in order):
   Step A: Write all data to the sheet (use write_range with all rows including header)
   Step B: Format the header row (row 1):
     - Use format_cells or update_cells to make header row bold
     - Set background color for header row (e.g., {"backgroundColor": {"red": 0.9, "green": 0.9, "blue": 0.9}} for light gray)
     - Range should be "Sheet1!A1:Z1" (adjust Z to match your column count)
   Step C: Freeze the header row:
     - Use freeze_rows tool with rows=1 to keep header visible when scrolling
   Step D: Create filters on the header row:
     - Use create_filter tool with range "Sheet1!A1:Z" (where Z is your last column)
     - This enables filter dropdowns on the header row
   Step E: (Optional) Apply alternating row colors:
     - Use format_cells to set background colors for even/odd rows if desired
   IMPORTANT: You MUST complete ALL steps (A through D) to create a proper table. Do not skip any step.
4. Handle the sheet configuration:
   - sheet_id: The Google Sheets document ID (from URL: https://docs.google.com/spreadsheets/d/{sheet_id}/edit)
   - create_new: If True, create a new sheet; if False, update existing
   - sheet_name: Name for the sheet/tab within the document
5. Return the table_schema you decided on (as table_schema field), the sheet_id used, and update statistics

Use the Google Sheets MCP tools to interact with the sheet. Read the current state first, then update as needed.
Remember: A proper Google Sheets table requires: data + formatted header + frozen header + filters enabled."""
    
    # Prepare context for LLM
    context = {
        "veps": [vep.model_dump(mode='json') for vep in veps],
        "sheet_config": sheet_config,
        "alerts": state.get("alerts", []),
        "current_release": state.get("current_release"),
    }
    
    vep_count = len(veps)
    user_prompt = f"""Here is the current VEP state and sheet configuration:

{json.dumps(context, indent=2, default=str)}

CRITICAL REQUIREMENTS:
1. You have been provided with {vep_count} VEP(s). You MUST write ALL {vep_count} VEP(s) to the Google Sheet.
2. ONE ROW PER VEP: Each VEP must appear as exactly ONE row. Do not skip, filter, or exclude any VEPs.
3. FIRST COLUMN IS VEP ID: Column A must be "VEP ID" and contain the tracking_issue_id (GitHub issue number) for each VEP.
4. After writing, verify that the sheet contains exactly {vep_count} data rows (plus 1 header row).

Sync this VEP data to Google Sheets.

WORKFLOW:
1. If sheet_id is provided ({sheet_config.get('sheet_id', 'NOT PROVIDED')}):
   - First, use get_spreadsheet(spreadsheetId="{sheet_config.get('sheet_id')}") to verify access
   - If access is denied, return an error - the spreadsheet must be shared with the service account
   - If access succeeds, read existing data with get_sheet_data or read_range
   - Then write all {vep_count} VEPs using write_range
   
2. If sheet_id is NOT provided and create_new is True:
   - Try to create a new spreadsheet (may fail if quota exceeded)
   
3. After writing data:
   - Format header row (bold, background color)
   - Freeze header row
   - Create filters on header row
   
CRITICAL: Column A MUST be "VEP ID" with tracking_issue_id values. Every VEP must be exactly one row."""
    
    # Invoke LLM with Google Sheets MCP tools
    # Note: If Google Sheets MCP is not available, this will fail gracefully
    try:
        result = invoke_llm_with_tools(
            "update_sheets",
            context,
            system_prompt,
            user_prompt,
            UpdateSheetsResponse,
            mcp_names=("google-sheets",)
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
        
        result = {
            "last_check_times": last_check_times,
            "sheets_need_update": False,  # Clear flag after successful update
            "next_tasks": next_tasks,
            "sheet_config": sheet_config,
        }
        
        # Check if one-cycle mode is enabled - exit after sheet update
        if state.get("config_cache", {}).get("one_cycle", False) and result.success:
            log("One-cycle mode: Sheet update successful, setting exit flag", node="update_sheets")
            # Clear next_tasks to prevent further execution
            result["next_tasks"] = []
            # Set a flag to signal main loop to exit
            result["_exit_after_sheets"] = True
        
        return result
        
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
            "next_tasks": next_tasks,
            "errors": errors,
        }
