"""MCP (Model Context Protocol) tools integration for agents."""

from typing import List, Any, Dict, Optional
import asyncio
import os
import json
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from langchain_core.tools import Tool
from pydantic import BaseModel, create_model
from services.utils import log

# ExceptionGroup is available in Python 3.11+ as a built-in
# For Python < 3.11, we'll use hasattr checks instead

# Dictionary mapping MCP names to their configurations
# 
# Notes on fixing warnings:
# 1. npm version warning: ✅ FIXED - Updated npm to latest in Containerfile
# 2. Deprecated package warnings: ✅ FIXED - Switched from deprecated @modelcontextprotocol/server-github
#    to @ama-mcp/github (actively maintained, published Dec 2025)
# 3. "GitHub MCP Server running on stdio" messages: Redirected stderr to suppress startup messages;
#    real errors come through MCP protocol (stdin/stdout)
MCP_CONFIGS = {
    "github":
    {
        "name": "github",
        "command": "sh",
        # Note: @modelcontextprotocol/server-github is deprecated but still functional
        # @ama-mcp/github doesn't work (Connection closed errors)
        # Redirect stderr to suppress startup messages; errors come through MCP protocol
        "args": ["-c", "exec npx --yes @modelcontextprotocol/server-github 2>/dev/null"],
        "env": {}  # Add GITHUB_TOKEN to env if needed
    },

    "google-sheets":
    {
        "name": "google-sheets",
        "command": "sh",
        # Note: @modelcontextprotocol/server-google-sheets doesn't exist
        # Using mcp-google-sheets instead (requires GOOGLE_APPLICATION_CREDENTIALS pointing to service account JSON)
        # Don't redirect stderr - we need to see authentication errors
        "args": ["-c", "exec npx --yes mcp-google-sheets"],
        "env": {}  # Will be populated with GOOGLE_APPLICATION_CREDENTIALS at runtime
    },
}

async def _get_mcp_tools_async(*mcp_configs: Dict[str, Any]) -> List[Tool]:
    """
    Retrieve tools from one or more MCP servers (async version).
    
    Args:
        *mcp_configs: Variable number of MCP configuration dictionaries
        
    Returns:
        List of LangChain Tool objects from all MCP servers
    """
    all_tools = []
    
    for config in mcp_configs:
        # Prepare environment - merge custom env with parent environment
        # This ensures the subprocess has access to both custom vars (like GITHUB_TOKEN)
        # and system environment variables
        custom_env = config.get("env", {}).copy()
        
        # Always merge with parent environment to ensure GITHUB_TOKEN and other vars are available
        # custom_env takes precedence over parent environment
        if custom_env:
            # Merge with parent environment - custom_env takes precedence
            env = {**os.environ, **custom_env}
        else:
            # No custom env vars, but still merge to ensure parent env vars (like GITHUB_TOKEN) are available
            env = os.environ.copy()
        
        # Only log if GITHUB_TOKEN is missing (error case)
        if config.get("name") == "github" and env:
            github_token_in_env = env.get("GITHUB_TOKEN")
            if not github_token_in_env:
                log("WARNING: GITHUB_TOKEN not found in environment that will be passed to MCP subprocess", node="mcp_factory", level="WARNING")
        
        server_params = StdioServerParameters(
            command=config["command"],
            args=config.get("args", []),
            env=env
        )
        
        # Use context manager to ensure proper cleanup
        async with stdio_client(server_params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                
                # List available tools from the MCP server
                tools_result = await session.list_tools()
                
                # Convert MCP tools to LangChain tools
                for mcp_tool in tools_result.tools:
                    # Get the tool's input schema to extract parameter names
                    input_schema = None
                    if hasattr(mcp_tool, 'inputSchema') and mcp_tool.inputSchema:
                        input_schema = mcp_tool.inputSchema
                    
                    # Create a closure to capture the tool config and name
                    def make_tool_func(tool_name: str, tool_config: Dict[str, Any], tool_schema: Optional[Dict] = None):
                        async def tool_func_async(**kwargs) -> str:
                            """Async function that creates a session and calls the tool."""
                            # Handle __arg1, __arg2, etc. by mapping to schema parameter names
                            # This is a workaround for LLMs that use positional args
                            if tool_schema and 'properties' in tool_schema:
                                properties = tool_schema['properties']
                                required = tool_schema.get('required', [])
                                param_names = list(properties.keys())
                                
                                # If kwargs has __arg1, __arg2, etc., map them to actual parameter names
                                mapped_kwargs = {}
                                for key, value in kwargs.items():
                                    if key.startswith('__arg') and key[5:].isdigit():
                                        arg_index = int(key[5:]) - 1
                                        if arg_index < len(param_names):
                                            mapped_kwargs[param_names[arg_index]] = value
                                        else:
                                            mapped_kwargs[key] = value  # Keep original if no mapping
                                    else:
                                        mapped_kwargs[key] = value
                                kwargs = mapped_kwargs
                            
                            # Prepare environment - merge custom env with parent environment
                            custom_env = tool_config.get("env", {}).copy()
                            
                            # Always merge with parent environment to ensure GITHUB_TOKEN and other vars are available
                            # custom_env takes precedence over parent environment
                            if custom_env:
                                # Merge with parent environment - custom_env takes precedence
                                env = {**os.environ, **custom_env}
                            else:
                                # No custom env vars, but still merge to ensure parent env vars are available
                                env = os.environ.copy()
                            
                            server_params = StdioServerParameters(
                                command=tool_config["command"],
                                args=tool_config.get("args", []),
                                env=env
                            )
                            
                            async with stdio_client(server_params) as (read, write):
                                async with ClientSession(read, write) as sess:
                                    await sess.initialize()
                                    try:
                                        result = await sess.call_tool(tool_name, arguments=kwargs)
                                        if result.content:
                                            # Extract text from content blocks
                                            text_parts = []
                                            for content_block in result.content:
                                                if hasattr(content_block, 'text'):
                                                    text_parts.append(content_block.text)
                                                elif isinstance(content_block, dict) and 'text' in content_block:
                                                    text_parts.append(content_block['text'])
                                                else:
                                                    text_parts.append(str(content_block))
                                            return "\n".join(text_parts) if text_parts else ""
                                        return ""
                                    except Exception as e:
                                        return f"Error calling tool {tool_name}: {str(e)}"
                        
                        # Wrap async function to be callable synchronously
                        def sync_wrapper(**kwargs) -> str:
                            return asyncio.run(tool_func_async(**kwargs))
                        
                        return sync_wrapper
                    
                    tool_func = make_tool_func(mcp_tool.name, config, input_schema)
                    
                    # Build enhanced description with parameter info and examples
                    description = mcp_tool.description or ""
                    
                    # Add tool-specific documentation and examples
                    tool_docs = _get_tool_documentation(mcp_tool.name)
                    if tool_docs:
                        description += "\n\n" + tool_docs
                    
                    if input_schema and 'properties' in input_schema:
                        param_info = []
                        properties = input_schema['properties']
                        required = input_schema.get('required', [])
                        for param_name, param_schema in properties.items():
                            param_type = param_schema.get('type', 'string')
                            param_desc = param_schema.get('description', '')
                            required_marker = ' (required)' if param_name in required else ' (optional)'
                            param_info.append(f"- {param_name} ({param_type}){required_marker}: {param_desc}")
                        if param_info:
                            description += "\n\nParameters:\n" + "\n".join(param_info)
                    
                    langchain_tool = Tool(
                        name=mcp_tool.name,
                        description=description,
                        func=tool_func,
                    )
                    all_tools.append(langchain_tool)
    
    return all_tools


def _get_tool_documentation(tool_name: str) -> str:
    """
    Get enhanced documentation for specific tools with examples and requirements.
    This helps the LLM understand how to use tools correctly without needing explicit instructions in prompts.
    """
    docs = {
        "search_issues": """CRITICAL REQUIREMENTS:
- The 'q' parameter MUST include either "is:issue" or "is:pull-request" in the query string
- GitHub's search API requires this qualifier to distinguish between issues and pull requests

CORRECT EXAMPLES:
- "repo:kubevirt/enhancements \"VEP 160\" is:issue" (searches for issues)
- "org:kubevirt \"VEP 160\" is:pull-request" (searches for pull requests)
- "repo:kubevirt/enhancements label:vep is:issue" (searches for issues with vep label)
- "repo:kubevirt/enhancements is:issue state:open" (all open issues)

INCORRECT (will fail with 422 error):
- "repo:kubevirt/enhancements \"VEP 160\"" (missing is:issue or is:pull-request)
- "org:kubevirt VEP" (missing is:issue or is:pull-request)

If you need both issues and PRs, make two separate queries.""",
        
        "list_issues": """This tool lists issues in a repository. Use this when you need to get all issues from a specific repo.
- Use search_issues when you need to search with filters
- Use list_issues when you need to enumerate all issues in a repo""",
        
        "get_issue": """Get details of a specific issue by number.
- Requires: owner, repo, issue_number
- Returns full issue details including body, comments, labels, etc.""",
        
        "get_pull_request": """Get details of a specific pull request by number.
- Requires: owner, repo, pull_number
- Returns full PR details including diff, reviews, comments, etc.""",
        
        # Google Sheets MCP tools
        "create_spreadsheet": """Create a new Google Spreadsheet.
- Requires: title (string) - the name of the spreadsheet
- Returns: spreadsheetId and other metadata
- Note: Service accounts have limited Drive storage quota. If you get a quota error, use an existing shared spreadsheet instead.""",
        
        "read_range": """Read data from a specific range in a Google Sheet.
- Requires: spreadsheetId (string), range (string, e.g., "Sheet1!A1:C10")
- Returns: 2D array of cell values
- Use this to check existing data before updating""",
        
        "write_range": """Write data to a specific range in a Google Sheet.
- Requires: spreadsheetId (string), range (string, e.g., "Sheet1!A1:C10"), values (2D array)
- Overwrites existing data in the range
- Use this to write table data. After writing, format the sheet using other tools.""",
        
        "update_cells": """Update specific cells with values and/or formatting.
- Requires: spreadsheetId (string), updates (array of cell update objects)
- More flexible than write_range - can update individual cells with formatting
- Use this for precise cell updates or when you need to set formatting along with values""",
        
        "format_cells": """Apply formatting to a range of cells.
- Requires: spreadsheetId (string), range (string), format (object with formatting properties)
- Format properties can include: backgroundColor, textFormat (bold, italic, etc.), borders, etc.
- Use this to format the header row (bold text, background color) and data rows
- Example format: {"backgroundColor": {"red": 0.9, "green": 0.9, "blue": 0.9}, "textFormat": {"bold": true}}""",
        
        "freeze_rows": """Freeze rows so they stay visible when scrolling.
- Requires: spreadsheetId (string), sheetId (integer, optional), frozenRowCount (integer)
- Use this to freeze the header row (row 1) so it stays visible
- Typical usage: freeze_rows with frozenRowCount=1 to freeze the header""",
        
        "create_filter": """Create a filter on a range (typically the header row).
- Requires: spreadsheetId (string), range (string, e.g., "Sheet1!A1:Z1" for header row)
- Enables filter dropdown arrows in the header row
- Use this after writing data to make the table filterable
- This creates a proper "table" experience in Google Sheets""",
        
        "list_spreadsheets": """List spreadsheets accessible to the service account.
- Requires: query (string, optional) - search query
- Returns: array of spreadsheet metadata
- Use this to find existing spreadsheets or verify access""",
        
        "get_spreadsheet": """Get metadata about a specific spreadsheet.
- Requires: spreadsheetId (string)
- Returns: spreadsheet metadata including sheet names, properties, etc.
- Use this to check if a spreadsheet exists and get its structure
- If this fails with "Requested entity was not found", the service account doesn't have access to the spreadsheet""",
        
        "get_sheet_data": """Read all data from a specific sheet/tab in a spreadsheet.
- Requires: spreadsheetId (string), sheetName (string, optional - defaults to first sheet)
- Returns: 2D array of all cell values in the sheet
- Use this to read existing data from a sheet before updating
- Alternative to read_range when you want all data from a sheet""",
        
        "list_sheets": """List all sheets/tabs in a spreadsheet.
- Requires: spreadsheetId (string)
- Returns: array of sheet names and metadata
- Use this to see what sheets exist in the spreadsheet
- If this fails with "Requested entity was not found", the service account doesn't have access""",
    }
    
    return docs.get(tool_name, "")


def _extract_error_messages(exc: Exception) -> list:
    """Recursively extract error messages from exceptions, including ExceptionGroup."""
    error_messages = []
    
    # Check if it's an ExceptionGroup (Python 3.11+) or has exceptions attribute
    # ExceptionGroup is a built-in in Python 3.11+, but we check hasattr for compatibility
    if hasattr(exc, 'exceptions'):
        # It's an ExceptionGroup or exception group-like object - recursively extract from all nested exceptions
        try:
            for nested_exc in exc.exceptions:
                error_messages.extend(_extract_error_messages(nested_exc))
        except (TypeError, AttributeError):
            # If exceptions is not iterable, just use the exception itself
            error_messages.append(str(exc).lower())
    else:
        # Regular exception - add its message
        error_messages.append(str(exc).lower())
    
    return error_messages


def get_mcp_tools_by_config(*mcp_configs: Dict[str, Any]) -> List[Tool]:
    """
    Retrieve tools from one or more MCP servers using configuration dictionaries.
    
    This is the internal function that handles the complex MCP tool retrieval.
    
    Args:
        *mcp_configs: Variable number of MCP configuration dictionaries
        
    Returns:
        List of LangChain Tool objects from all MCP servers
        
    Raises:
        Exception: If MCP server fails to start (e.g., package not found)
    """
    try:
        return asyncio.run(_get_mcp_tools_async(*mcp_configs))
    except Exception as e:
        # Handle both regular exceptions and ExceptionGroup (Python 3.11+)
        error_messages = _extract_error_messages(e)
        
        # Check if any exception indicates a connection/MCP issue
        all_errors = " ".join(error_messages)
        if any(keyword in all_errors for keyword in ["404", "not found", "connection closed", "mcp", "mcperror"]):
            # This is likely a missing npm package or MCP server failure - log and return empty list
            from services.utils import log
            mcp_names = [config.get("name", "unknown") for config in mcp_configs]
            # Get a simplified error message (first meaningful error)
            first_error = error_messages[0] if error_messages else str(e)
            log(f"MCP server(s) {', '.join(mcp_names)} not available (package may not exist, not installed, or connection failed): {first_error}", node="mcp_factory", level="WARNING")
            return []
        # Re-raise other exceptions
        raise


def get_mcp_tools_by_name(*mcp_names: str) -> List[Tool]:
    """
    Retrieve tools from one or more MCP servers by name.
    
    Convenience function that looks up MCP configurations by name.
    Automatically injects credentials from utils for Google Sheets.
    
    Args:
        *mcp_names: Variable number of MCP names (e.g., "github", "google-sheets")
        
    Returns:
        List of LangChain Tool objects from all MCP servers
        
    Raises:
        KeyError: If an MCP name is not found in MCP_CONFIGS
    """
    configs = []
    for name in mcp_names:
        if name not in MCP_CONFIGS:
            raise KeyError(f"MCP '{name}' not found in MCP_CONFIGS. Available: {list(MCP_CONFIGS.keys())}")
        
        # Create a copy of the config to avoid mutating the original
        config = MCP_CONFIGS[name].copy()
        
        # Inject credentials
        config["env"] = config.get("env", {}).copy()
        
        if name == "google-sheets":
            from services.utils import get_google_token
            import tempfile
            import json
            # os is imported at module level, ensure it's available here
            import os as os_module
            try:
                token = get_google_token()
                if not token or not token.strip():
                    log("GOOGLE_TOKEN is empty - Google Sheets MCP will not be available", node="mcp_factory", level="WARNING")
                else:
                    # mcp-google-sheets uses Application Default Credentials (ADC)
                    # It expects GOOGLE_APPLICATION_CREDENTIALS to point to a JSON file
                    # Write token to a temporary file and set the env var
                    try:
                        # Try to parse as JSON to validate
                        json.loads(token)
                        # Create a temporary file with the credentials
                        temp_file = tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False)
                        temp_file.write(token)
                        temp_file.close()
                        config["env"]["GOOGLE_APPLICATION_CREDENTIALS"] = temp_file.name
                        log(f"Google credentials written to temporary file: {temp_file.name}", node="mcp_factory", level="DEBUG")
                    except (json.JSONDecodeError, ValueError):
                        # If token is not valid JSON, try treating it as a file path
                        token_path = token.strip()
                        if token_path and os_module.path.exists(token_path):
                            config["env"]["GOOGLE_APPLICATION_CREDENTIALS"] = token_path
                            log(f"Using Google credentials from file: {token_path}", node="mcp_factory", level="DEBUG")
                        else:
                            # Token is neither JSON nor a file path - might be an API key
                            # mcp-google-sheets requires service account JSON, not API keys
                            # Skip setting GOOGLE_APPLICATION_CREDENTIALS - let it try ADC (will likely fail)
                            if token_path.startswith("AIza"):
                                log("GOOGLE_TOKEN appears to be an API key, not service account JSON. mcp-google-sheets requires service account JSON credentials. The MCP server will likely fail to start. Please provide service account JSON credentials.", node="mcp_factory", level="WARNING")
                            else:
                                log(f"Google token is not valid JSON and not a valid file path. mcp-google-sheets requires service account JSON. The MCP server will likely fail to start.", node="mcp_factory", level="WARNING")
                            # Don't set GOOGLE_APPLICATION_CREDENTIALS - it will fail anyway
            except FileNotFoundError:
                # If token file doesn't exist, continue without it (will fail at runtime)
                log("GOOGLE_TOKEN not found - Google Sheets MCP will not be available", node="mcp_factory", level="WARNING")
                pass
        elif name == "github":
            # Inject GitHub token from environment if available
            # The MCP server expects GITHUB_PERSONAL_ACCESS_TOKEN, not GITHUB_TOKEN
            import os
            github_token = os.environ.get("GITHUB_TOKEN")
            if github_token:
                # Set both for compatibility (GITHUB_PERSONAL_ACCESS_TOKEN is what the MCP server uses)
                config["env"]["GITHUB_PERSONAL_ACCESS_TOKEN"] = github_token
                config["env"]["GITHUB_TOKEN"] = github_token  # Also set for backward compatibility
            else:
                log("GITHUB_TOKEN not found in environment - API rate limits may apply", node="mcp_factory", level="WARNING")
        
        configs.append(config)
    
    return get_mcp_tools_by_config(*configs)

def get_all_tools() -> List[Tool]:
    """Get tools from all configured MCP servers."""
    return get_mcp_tools_by_name(*MCP_CONFIGS.keys())
