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
            # Debug: verify GITHUB_TOKEN is in merged env if it was in custom_env
            if "GITHUB_TOKEN" in custom_env:
                if "GITHUB_TOKEN" in env:
                    token_preview = env["GITHUB_TOKEN"][:10] + "..." if len(env["GITHUB_TOKEN"]) > 10 else "***"
                    log(f"GITHUB_TOKEN verified in merged environment (token: {token_preview})", node="mcp_factory", level="DEBUG")
        else:
            # No custom env vars, but still merge to ensure parent env vars (like GITHUB_TOKEN) are available
            # Only use None if we explicitly want to inherit (but we want to ensure token is passed)
            env = os.environ.copy()
        
        # Debug: Log environment info for GitHub MCP
        if config.get("name") == "github" and env:
            github_token_in_env = env.get("GITHUB_TOKEN")
            if github_token_in_env:
                token_preview = github_token_in_env[:10] + "..." if len(github_token_in_env) > 10 else "***"
                log(f"GITHUB_TOKEN will be passed to MCP subprocess (token: {token_preview}, env keys: {len(env)})", node="mcp_factory", level="DEBUG")
            else:
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
                    
                    # Build enhanced description with parameter info
                    description = mcp_tool.description or ""
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
                # Log first 10 chars for verification (don't log full token for security)
                token_preview = github_token[:10] + "..." if len(github_token) > 10 else "***"
                log(f"GitHub token injected as GITHUB_PERSONAL_ACCESS_TOKEN (token: {token_preview})", node="mcp_factory", level="DEBUG")
                # Verify token will be in final env
                if config.get("env", {}).get("GITHUB_PERSONAL_ACCESS_TOKEN"):
                    log("GitHub token verified in config.env as GITHUB_PERSONAL_ACCESS_TOKEN", node="mcp_factory", level="DEBUG")
            else:
                log("GITHUB_TOKEN not found in environment - API rate limits may apply", node="mcp_factory", level="WARNING")
        
        configs.append(config)
    
    return get_mcp_tools_by_config(*configs)

def get_all_tools() -> List[Tool]:
    """Get tools from all configured MCP servers."""
    return get_mcp_tools_by_name(*MCP_CONFIGS.keys())
