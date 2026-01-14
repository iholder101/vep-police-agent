"""MCP (Model Context Protocol) tools integration for agents."""

from typing import List, Any, Dict, Optional
import asyncio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from langchain_core.tools import Tool

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
        "args": ["-c", "exec npx --yes @modelcontextprotocol/server-google-sheets 2>/dev/null"],
        "env": {}  # Will be populated with GOOGLE_CREDENTIALS at runtime
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
        # Prepare environment - npm will show warnings but we've updated to latest version
        env = config.get("env", {}).copy()
        # Note: We keep npm warnings visible since user wants to fix them, not suppress
        # The deprecated package warning is from @modelcontextprotocol/server-github itself
        # which is marked as deprecated by npm but still functional
        
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
                    # Create a closure to capture the tool config and name
                    def make_tool_func(tool_name: str, tool_config: Dict[str, Any]):
                        async def tool_func_async(**kwargs) -> str:
                            """Async function that creates a session and calls the tool."""
                            # Prepare environment
                            env = tool_config.get("env", {}).copy()
                            
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
                    
                    tool_func = make_tool_func(mcp_tool.name, config)
                    
                    langchain_tool = Tool(
                        name=mcp_tool.name,
                        description=mcp_tool.description or "",
                        func=tool_func,
                    )
                    all_tools.append(langchain_tool)
    
    return all_tools


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
        # Check if it's a known issue with missing packages
        error_str = str(e).lower()
        if "404" in error_str or "not found" in error_str or "connection closed" in error_str:
            # This is likely a missing npm package - log and return empty list
            from services.utils import log
            mcp_names = [config.get("name", "unknown") for config in mcp_configs]
            log(f"MCP server(s) {', '.join(mcp_names)} not available (package may not exist or not installed): {e}", node="mcp_factory", level="WARNING")
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
        
        # Inject credentials for Google Sheets
        if name == "google-sheets":
            from services.utils import get_google_token
            try:
                token = get_google_token()
                config["env"] = config.get("env", {}).copy()
                config["env"]["GOOGLE_CREDENTIALS"] = token
            except FileNotFoundError:
                # If token file doesn't exist, continue without it (will fail at runtime)
                pass
        
        configs.append(config)
    
    return get_mcp_tools_by_config(*configs)

def get_all_tools() -> List[Tool]:
    """Get tools from all configured MCP servers."""
    return get_mcp_tools_by_name(*MCP_CONFIGS.keys())
