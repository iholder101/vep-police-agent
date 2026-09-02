"""MCP (Model Context Protocol) tools integration for agents."""

import asyncio
import os
from contextlib import asynccontextmanager
from typing import Annotated, Any

from langchain_core.tools import StructuredTool
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from pydantic import Field, WithJsonSchema, create_model

from services.utils import log


@asynccontextmanager
async def _safe_stdio_client(server_params):
    """Wrap stdio_client to suppress BrokenResourceError on cleanup.

    The Go-based github-mcp-server closes stdout immediately on exit,
    causing the MCP client's stdout_reader task to raise
    BrokenResourceError during context manager teardown.  The session
    data is already fully read at that point, so it's safe to ignore.
    """
    try:
        async with stdio_client(server_params) as streams:
            yield streams
    except BaseExceptionGroup as eg:
        import anyio
        _, unhandled = eg.split(lambda e: isinstance(e, (anyio.BrokenResourceError, anyio.ClosedResourceError)))
        if unhandled:
            raise unhandled

# Custom types for arrays that produce Gemini-compatible schema but accept any input
# FlexibleArray: for simple arrays (recipients, etc.)
FlexibleArray = Annotated[Any, WithJsonSchema({'type': 'array', 'items': {'type': 'string'}})]
# Flexible2DArray: for 2D arrays like spreadsheet data (array of rows, each row is array of cells)
Flexible2DArray = Annotated[Any, WithJsonSchema({'type': 'array', 'items': {'type': 'array', 'items': {'type': 'string'}}})]


def _fix_schema_for_gemini(schema: dict[str, Any]) -> dict[str, Any]:
    """
    Recursively fix JSON schema to be compatible with Gemini.

    Gemini requires array types to have items with a type field.
    This function replaces empty `items: {}` with `items: {"type": "string"}`.
    """
    if not isinstance(schema, dict):
        return schema

    result = {}
    for key, value in schema.items():
        if key == 'items' and isinstance(value, dict) and not value:
            # Empty items - replace with string type
            result[key] = {"type": "string"}
        elif key == 'items' and isinstance(value, dict) and 'type' not in value:
            # items exists but has no type - add string type
            fixed_items = _fix_schema_for_gemini(value)
            if 'type' not in fixed_items:
                fixed_items['type'] = 'string'
            result[key] = fixed_items
        elif isinstance(value, dict):
            result[key] = _fix_schema_for_gemini(value)
        elif isinstance(value, list):
            result[key] = [_fix_schema_for_gemini(item) if isinstance(item, dict) else item for item in value]
        else:
            result[key] = value

    return result


def _get_tool_input_schema(mcp_tool: Any) -> dict[str, Any] | None:
    """Extract a tool's JSON input schema, tolerating both mcp SDK attribute names.

    The mcp SDK (>=2.0) renamed the Python attribute from camelCase 'inputSchema'
    to snake_case 'input_schema'; 'inputSchema' is only kept as a pydantic
    serialization alias and is not accessible via getattr() on those versions.
    Checking both means this works regardless of the installed SDK version.
    """
    return getattr(mcp_tool, 'input_schema', None) or getattr(mcp_tool, 'inputSchema', None)


def _create_args_schema_from_json_schema(tool_name: str, json_schema: dict[str, Any]) -> type | None:
    """
    Create a Pydantic model from a JSON schema for use as args_schema.

    This ensures proper type information is available for Gemini's function calling.
    """
    if not json_schema or 'properties' not in json_schema:
        log(f"No flat 'properties' in JSON schema for tool '{tool_name}' - cannot build typed args_schema; LLM will get a generic kwargs schema and may send empty args", node="mcp_factory", level="WARNING")
        return None

    # Fix the schema for Gemini compatibility
    fixed_schema = _fix_schema_for_gemini(json_schema)

    properties = fixed_schema.get('properties', {})
    required = set(fixed_schema.get('required', []))

    # Build field definitions for create_model
    field_definitions = {}

    for param_name, param_schema in properties.items():
        param_type = param_schema.get('type', 'string')
        param_desc = param_schema.get('description', '')
        is_required = param_name in required

        # Map JSON schema types to Python types
        # Use Any for complex types to avoid schema validation issues
        if param_type == 'string':
            python_type = str
        elif param_type == 'integer':
            python_type = int
        elif param_type == 'number':
            python_type = float
        elif param_type == 'boolean':
            python_type = bool
        elif param_type == 'array':
            # Check if this is a 2D array (array of arrays) by inspecting nested items
            # Common 2D array fields: data, values (used by Google Sheets for spreadsheet data)
            items_schema = param_schema.get('items', {})
            items_type = items_schema.get('type', '') if isinstance(items_schema, dict) else ''

            # Use Flexible2DArray for nested arrays or known 2D array fields
            if items_type == 'array' or param_name in ('data', 'values'):
                python_type = Flexible2DArray
            else:
                # Use FlexibleArray for simple 1D arrays
                python_type = FlexibleArray
        elif param_type == 'object':
            python_type = dict[str, Any]
        else:
            python_type = Any

        # Create field with default or required
        if is_required:
            field_definitions[param_name] = (python_type, Field(description=param_desc))
        else:
            field_definitions[param_name] = (python_type | None, Field(default=None, description=param_desc))

    if not field_definitions:
        log(f"No field definitions built for tool '{tool_name}' - cannot build typed args_schema; LLM will get a generic kwargs schema and may send empty args", node="mcp_factory", level="WARNING")
        return None

    # Create model with a unique name based on tool name
    model_name = f"{tool_name}_args"
    try:
        return create_model(model_name, **field_definitions)
    except Exception as e:
        log(f"Failed to create args_schema for {tool_name}: {e}", node="mcp_factory", level="WARNING")
        return None

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
        "command": "github-mcp-server",
        # Official Go binary from github/github-mcp-server, installed in Containerfile
        "args": ["stdio", "--read-only"],
        "env": {}
    },

    "google-sheets":
    {
        "name": "google-sheets",
        "command": "sh",
        # Note: @modelcontextprotocol/server-google-sheets doesn't exist
        # Using mcp-google-sheets instead (requires GOOGLE_APPLICATION_CREDENTIALS pointing to service account JSON)
        # Don't redirect stderr - we need to see authentication errors
        "args": ["-c", "exec npx --yes mcp-google-sheets@2.0.1"],
        "env": {}  # Will be populated with GOOGLE_APPLICATION_CREDENTIALS at runtime
    },
}

async def _get_mcp_tools_async(*mcp_configs: dict[str, Any]) -> list[StructuredTool]:
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
        async with _safe_stdio_client(server_params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()

            # List available tools from the MCP server
            tools_result = await session.list_tools()

            # List of write operations to exclude (agent should only read from GitHub)
            # These tools modify GitHub repositories and should not be available to the agent
            write_operations_to_exclude = {
                    "create_or_update_file",
                    "create_issue",
                    "create_pull_request",
                    "push_files",
                    "create_repository",
                    "fork_repository",
                    "create_branch",
                    "update_issue",
                    "add_issue_comment",
                    "create_pull_request_review",
                    "merge_pull_request",
                    "update_pull_request_branch",
            }
            
            # Count tools before filtering for logging
            total_tools = len(tools_result.tools)
            excluded_count = 0
            
            # Convert MCP tools to LangChain tools
            for mcp_tool in tools_result.tools:
                # Skip write operations - agent should only read from GitHub
                if mcp_tool.name in write_operations_to_exclude:
                    excluded_count += 1
                    log(f"Excluding write operation tool: {mcp_tool.name} (agent is read-only)", node="mcp_factory", level="DEBUG")
                    continue
                
                # Get the tool's input schema to extract parameter names
                input_schema = _get_tool_input_schema(mcp_tool)
                
                # Create a closure to capture the tool config and name
                def make_tool_func(tool_name: str, tool_config: dict[str, Any], tool_schema: dict | None = None):
                    async def tool_func_async(**kwargs) -> str:
                        """Async function that creates a session and calls the tool."""
                        # Handle __arg1, __arg2, etc. by mapping to schema parameter names
                        # This is a workaround for LLMs that use positional args
                        if tool_schema and 'properties' in tool_schema:
                            properties = tool_schema['properties']
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
                        
                        async with _safe_stdio_client(server_params) as (read, write), ClientSession(read, write) as sess:
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
                                return f"Error calling tool {tool_name}: {e!s}"
                    
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
                
                # Create Pydantic args_schema for proper Gemini compatibility
                args_schema = None
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

                    # Create args_schema for Gemini compatibility
                    args_schema = _create_args_schema_from_json_schema(mcp_tool.name, input_schema)

                # Use StructuredTool for proper schema handling with Gemini
                langchain_tool = StructuredTool.from_function(
                    func=tool_func,
                    name=mcp_tool.name,
                    description=description,
                    args_schema=args_schema,
                )
                all_tools.append(langchain_tool)
            
            # Log filtering summary for GitHub MCP
            if config.get("name") == "github" and excluded_count > 0:
                log(f"GitHub MCP: Filtered {excluded_count} write operation(s) from {total_tools} total tools ({len(all_tools)} read-only tools available)", node="mcp_factory")
    
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
        
        # Google Sheets MCP tools (mcp-google-sheets@2.0.1 - 8-tool set)
        "list_spreadsheets": """List spreadsheets accessible to the service account.
- Use this to find existing spreadsheets or verify access.""",

        "create_spreadsheet": """Create a new Google Spreadsheet.
- Note: Service accounts have limited Drive storage quota. If you get a quota error, use an existing shared spreadsheet instead.""",

        "get_sheet_data": """Read all data from a specific sheet/tab in a spreadsheet.
- Use this to check existing data before updating.""",

        "update_cells": """Write/overwrite cell values in a range of a sheet.
- Requires: spreadsheet_id (string), sheet (string - the sheet/tab name), range (string, e.g. "A1" or "A1:E10" - WITHOUT the sheet name prefix, since sheet is a separate argument), data (array of arrays - rows of cell values)
- Use this to write the VEP table.""",

        "list_sheets": """List all sheets/tabs in a spreadsheet.
- Use this to see what sheets exist in the spreadsheet.""",

        "create_sheet": """Create a new sheet/tab within an existing spreadsheet.
- Use this if the target sheet/tab does not exist yet.""",

        "share_spreadsheet": """Share a spreadsheet with a given email address/role.
- Use this to grant access to the spreadsheet.""",

        "batch_update_cells": """Update multiple ranges of a sheet in one call.
- Requires: spreadsheet_id (string), sheet (string - the sheet/tab name), ranges (range -> data updates)
- Use this instead of several update_cells calls when writing multiple distinct ranges at once.""",
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


def get_mcp_tools_by_config(*mcp_configs: dict[str, Any]) -> list[StructuredTool]:
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
            log(f"MCP server(s) {', '.join(mcp_names)} failed to start: {first_error}", node="mcp_factory", level="ERROR")
            log(f"All MCP errors: {all_errors}", node="mcp_factory", level="DEBUG")
            return []
        # Re-raise other exceptions
        raise


def get_mcp_tools_by_name(*mcp_names: str) -> list[StructuredTool]:
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
            import json

            # os is imported at module level, ensure it's available here
            import os as os_module
            import tempfile

            from services.utils import get_google_token
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
                        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as temp_file:
                            temp_file.write(token)
                            temp_path = temp_file.name
                        config["env"]["GOOGLE_APPLICATION_CREDENTIALS"] = temp_path
                        log(f"Google credentials written to temporary file: {temp_path}", node="mcp_factory", level="DEBUG")
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
                                log("Google token is not valid JSON and not a valid file path. mcp-google-sheets requires service account JSON. The MCP server will likely fail to start.", node="mcp_factory", level="WARNING")
                            # Don't set GOOGLE_APPLICATION_CREDENTIALS - it will fail anyway
            except FileNotFoundError:
                # If token file doesn't exist, continue without it (will fail at runtime)
                log("GOOGLE_TOKEN not found - Google Sheets MCP will not be available", node="mcp_factory", level="WARNING")
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

def get_all_tools() -> list[StructuredTool]:
    """Get tools from all configured MCP servers."""
    return get_mcp_tools_by_name(*MCP_CONFIGS.keys())
