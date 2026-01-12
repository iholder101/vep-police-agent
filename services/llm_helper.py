"""Helper functions for creating LLM agents with MCP tools."""

from typing import Dict, Any, Type, TypeVar
from pydantic import BaseModel
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from services.utils import get_model, log
from services.mcp_factory import get_mcp_tools_by_name

T = TypeVar('T', bound=BaseModel)


def invoke_llm_with_tools(
    operation_type: str,
    state_context: Dict[str, Any],
    system_prompt: str,
    user_prompt: str,
    response_model: Type[T],
    mcp_names: tuple = ("github",)
) -> T:
    """Invoke LLM with MCP tools using structured output.
    
    Args:
        operation_type: Type of operation (for logging)
        state_context: Current state context
        system_prompt: System prompt describing the task
        user_prompt: User prompt with specific instructions
        response_model: Pydantic model for structured output
        mcp_names: Tuple of MCP server names to load tools from (default: ("github",))
    
    Returns:
        Validated Pydantic model instance
    """
    try:
        # Get MCP tools
        tools = get_mcp_tools_by_name(*mcp_names)
        mcp_list = ", ".join(mcp_names)
        log(f"Loaded {len(tools)} MCP tools ({mcp_list}) for {operation_type}", node=operation_type)
        
        if not tools:
            log(f"No MCP tools available for {operation_type}", node=operation_type, level="ERROR")
            # Return empty response with proper structure
            try:
                return response_model()
            except Exception:
                # If model requires fields, try with empty defaults
                return response_model(**{})
        
        # Create LLM with tools boundיגעכ
        llm = get_model()
        llm_with_tools = llm.bind_tools(tools)
        
        # Build messages
        messages = [SystemMessage(content=system_prompt), HumanMessage(content=user_prompt)]
        
        # First, handle tool calls (if any) - do this without structured output
        max_iterations = 10
        iteration = 0
        while iteration < max_iterations:
            iteration += 1
            log(f"Invoking LLM for {operation_type} (iteration {iteration})", node=operation_type)
            response = llm_with_tools.invoke(messages)
            
            # Check if response has tool calls
            if not (hasattr(response, 'tool_calls') and response.tool_calls):
                # No more tool calls, break and get structured output
                break
            
            log(f"LLM made {len(response.tool_calls)} tool call(s), iteration {iteration}", node=operation_type)
            
            # Execute tool calls
            tool_messages = []
            for tool_call in response.tool_calls:
                tool_name = tool_call.get("name", "")
                tool_args = tool_call.get("args", {})
                
                # Find and execute the tool
                tool_result = None
                for tool in tools:
                    if tool.name == tool_name:
                        try:
                            tool_result = tool.func(**tool_args)
                            break
                        except Exception as e:
                            tool_result = f"Error: {str(e)}"
                            log(f"Error executing tool {tool_name}: {e}", node=operation_type, level="ERROR")
                
                if tool_result is None:
                    tool_result = f"Tool {tool_name} not found"
                
                # Create tool message
                tool_messages.append(ToolMessage(
                    content=str(tool_result),
                    tool_call_id=tool_call.get("id", "")
                ))
            
            # Add tool results and continue
            messages.append(response)
            messages.extend(tool_messages)
        
        # Now get structured output with final messages (including tool results)
        # Add a final message asking for structured output
        messages.append(HumanMessage(content="Based on the information gathered, please provide your response in the required structured format."))
        
        # Use structured output - LLM will return validated Pydantic model
        structured_llm = llm_with_tools.with_structured_output(response_model)
        result = structured_llm.invoke(messages)
        
        # Response is already a validated Pydantic model!
        log(f"Successfully received structured response for {operation_type}", node=operation_type)
        return result
        
    except Exception as e:
        log(f"Error invoking LLM for {operation_type}: {e}", node=operation_type, level="ERROR")
        import traceback
        log(f"Traceback: {traceback.format_exc()}", node=operation_type, level="ERROR")
        # Return empty response with proper structure
        try:
            return response_model()
        except Exception:
            # If model requires fields, try with empty defaults
            return response_model(**{})


def invoke_llm_check(
    check_type: str,
    state_context: Dict[str, Any],
    system_prompt: str,
    user_prompt: str,
    response_model: Type[T]
) -> T:
    """Invoke LLM to perform a check with GitHub MCP tools using structured output.
    
    Convenience wrapper around invoke_llm_with_tools for check nodes.
    
    Args:
        check_type: Type of check ("deadlines", "activity", "compliance", "exceptions")
        state_context: Current state context (veps, release_schedule, etc.)
        system_prompt: System prompt describing the task
        user_prompt: User prompt with specific instructions
        response_model: Pydantic model for structured output
    
    Returns:
        Validated Pydantic model instance
    """
    return invoke_llm_with_tools(check_type, state_context, system_prompt, user_prompt, response_model, mcp_names=("github",))
