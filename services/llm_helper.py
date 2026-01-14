"""Helper functions for creating LLM agents with MCP tools."""

import json
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
        
        # Create LLM with tools bound
        llm = get_model()
        llm_with_tools = llm.bind_tools(tools)
        
        # Build messages
        messages = [SystemMessage(content=system_prompt), HumanMessage(content=user_prompt)]
        
        # First, handle tool calls (if any) - do this without structured output
        # Check for debug mode that limits iterations
        import os
        debug_mode = os.environ.get("DEBUG_MODE")
        if debug_mode == "test-sheets":
            # Increase iterations for test-sheets to allow LLM to complete write operations
            max_iterations = 5
            log(f"Debug mode 'test-sheets' enabled - limiting to {max_iterations} iteration(s)", node=operation_type, level="INFO")
        else:
            # Increased for fetch_veps which may need to read many issue details
            max_iterations = 30 if operation_type == "fetch_veps" else 10
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
                
                # Log tool call details
                log(f"Executing tool: {tool_name} with args: {json.dumps(tool_args, default=str)[:200]}...", node=operation_type, level="DEBUG")
                
                # Find and execute the tool
                tool_result = None
                for tool in tools:
                    if tool.name == tool_name:
                        try:
                            tool_result = tool.func(**tool_args)
                            # Log tool result (truncate if too long)
                            result_str = str(tool_result)
                            if len(result_str) > 500:
                                result_str = result_str[:500] + "... (truncated)"
                            log(f"Tool {tool_name} result: {result_str}", node=operation_type, level="DEBUG")
                            break
                        except Exception as e:
                            tool_result = f"Error: {str(e)}"
                            log(f"Error executing tool {tool_name}: {e}", node=operation_type, level="ERROR")
                            import traceback
                            log(f"Tool error traceback: {traceback.format_exc()}", node=operation_type, level="DEBUG")
                
                if tool_result is None:
                    tool_result = f"Tool {tool_name} not found"
                    log(f"Tool {tool_name} not found in available tools: {[t.name for t in tools]}", node=operation_type, level="WARNING")
                
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
        # Use Pydantic model introspection to provide defaults for required fields
        try:
            # Try to create with empty dict first (works if all fields have defaults)
            return response_model()
        except Exception:
            # Build defaults from model fields
            defaults = {}
            try:
                # Use Pydantic v2 model_fields if available
                if hasattr(response_model, 'model_fields'):
                    for field_name, field_info in response_model.model_fields.items():
                        if field_info.is_required():
                            # Provide sensible defaults based on field type
                            field_type = str(field_info.annotation) if hasattr(field_info, 'annotation') else ''
                            if 'List' in field_type or field_name in ['updated_veps', 'alerts']:
                                defaults[field_name] = []
                            elif field_name == 'success':
                                defaults[field_name] = False
                            elif 'Dict' in field_type:
                                defaults[field_name] = {}
                            elif 'Optional' in field_type or field_name.endswith('_id'):
                                defaults[field_name] = None
                            else:
                                defaults[field_name] = None
                else:
                    # Fallback for Pydantic v1 or models without model_fields
                    # Check common field names
                    if hasattr(response_model, '__annotations__'):
                        annotations = response_model.__annotations__
                        for field_name in annotations:
                            if field_name in ['updated_veps', 'alerts']:
                                defaults[field_name] = []
                            elif field_name == 'success':
                                defaults[field_name] = False
                            else:
                                defaults[field_name] = None
                
                return response_model(**defaults)
            except Exception as final_error:
                log(f"Could not create {response_model.__name__} with defaults: {final_error}", node=operation_type, level="ERROR")
                # Last resort: try with minimal known defaults
                minimal_defaults = {
                    'updated_veps': [],
                    'alerts': [],
                    'success': False,
                }
                try:
                    return response_model(**{k: v for k, v in minimal_defaults.items() if hasattr(response_model, k)})
                except Exception:
                    # This will fail but at least we tried everything
                    raise ValueError(f"Could not create {response_model.__name__} with defaults. Error: {final_error}")


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
