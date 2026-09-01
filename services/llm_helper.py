"""Helper functions for creating LLM agents with MCP tools."""

import concurrent.futures
import json
import time
from typing import Any, TypeVar

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from pydantic import BaseModel

from config.config import LLM_INITIAL_DELAY, LLM_MAX_RETRIES, LLM_MAX_TIMEOUT
from services.mcp_factory import get_mcp_tools_by_name
from services.utils import get_model, log

T = TypeVar('T', bound=BaseModel)


def _invoke_with_timeout(llm, messages, timeout_seconds):
    """Invoke LLM with a per-call timeout to prevent indefinite hangs."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(llm.invoke, messages)
        return future.result(timeout=timeout_seconds)


def _invoke_with_retry(llm, messages, operation_type: str):
    """Invoke LLM with per-call timeout and exponential backoff on retriable errors.

    Retriable errors: rate limits (429/RESOURCE_EXHAUSTED) and call timeouts.
    Returns the LLM response or raises if max retries/timeout exceeded.
    """
    start_time = time.time()
    delay = LLM_INITIAL_DELAY

    for attempt in range(LLM_MAX_RETRIES + 1):
        try:
            return _invoke_with_timeout(llm, messages, LLM_MAX_TIMEOUT)
        except concurrent.futures.TimeoutError:
            elapsed = time.time() - start_time
            if attempt >= LLM_MAX_RETRIES:
                log(f"LLM call timed out after {LLM_MAX_TIMEOUT}s, max retries exceeded",
                    node=operation_type, level="ERROR")
                raise
            log(f"LLM call timed out after {LLM_MAX_TIMEOUT}s, retrying in {delay}s "
                f"(attempt {attempt+1}/{LLM_MAX_RETRIES})",
                node=operation_type, level="WARNING")
            time.sleep(delay)
            delay = min(delay * 2, 60)
        except Exception as e:
            error_str = str(e)
            # Check if it's a rate limit error
            if "RESOURCE_EXHAUSTED" not in error_str and "429" not in error_str:
                raise  # Not a rate limit error, re-raise immediately

            elapsed = time.time() - start_time
            if elapsed + delay > LLM_MAX_TIMEOUT:
                log(f"Rate limit retry would exceed max timeout ({LLM_MAX_TIMEOUT}s), aborting",
                    node=operation_type, level="ERROR")
                raise

            log(f"Rate limited, retrying in {delay}s (attempt {attempt+1}/{LLM_MAX_RETRIES})",
                node=operation_type, level="WARNING")
            time.sleep(delay)
            delay = min(delay * 2, 60)  # Cap at 60s per retry

    raise RuntimeError(f"Max retries ({LLM_MAX_RETRIES}) exceeded for {operation_type}")


class NoToolsCalledException(Exception):
    """Raised when LLM completes without calling any tools but tools were required."""


class ToolCallFailedException(Exception):
    """Raised when tools were required, at least one tool call was attempted,
    but every attempted tool call failed (e.g. the MCP server returned an error
    for each call). Without this, a run where every write errored would still
    be reported as a success by the LLM's self-reported structured output."""


def _is_tool_call_error(result: Any) -> bool:
    """Conservatively detect whether a tool call's result string indicates failure.

    Only matches clear, unambiguous error markers - as opposed to any output that
    merely happens to mention the word "error" - to avoid false positives on
    legitimate tool output.
    """
    if not isinstance(result, str):
        return False
    return result.startswith(("Error", "error")) or "MCP error" in result


def invoke_llm_with_tools(
    operation_type: str,
    state_context: dict[str, Any],
    system_prompt: str,
    user_prompt: str,
    response_model: type[T],
    mcp_names: tuple = ("github",),
    require_tools: bool = False
) -> T:
    """Invoke LLM with MCP tools using structured output.

    Args:
        operation_type: Type of operation (for logging)
        state_context: Current state context
        system_prompt: System prompt describing the task
        user_prompt: User prompt with specific instructions
        response_model: Pydantic model for structured output
        mcp_names: Tuple of MCP server names to load tools from (default: ("github",))
        require_tools: If True, raises NoToolsCalledException if LLM doesn't call any tools

    Returns:
        Validated Pydantic model instance

    Raises:
        NoToolsCalledException: If require_tools=True and no tools were called
        ToolCallFailedException: If require_tools=True, at least one tool call
            was attempted, and every attempted tool call failed
    """
    try:
        # Get MCP tools
        tools = get_mcp_tools_by_name(*mcp_names)
        mcp_list = ", ".join(mcp_names)
        tool_names = [t.name for t in tools] if tools else []
        log(f"Loaded {len(tools)} MCP tools ({mcp_list}) for {operation_type}: {tool_names}", node=operation_type)
        
        if not tools:
            log(f"No MCP tools available for {operation_type}", node=operation_type, level="ERROR")
            # Return empty response with proper structure
            try:
                return response_model()
            except Exception as e:
                # If model requires fields, try with empty defaults
                log(f"Could not create empty {response_model.__name__}: {e}", node=operation_type, level="WARNING")
                return response_model()

        # Get model for this operation type (node)
        import config
        model_name = config.get_model_for_node(operation_type)
        log(f"Using model {model_name} for {operation_type}", node=operation_type, level="DEBUG")

        # Create LLM with tools bound
        # Use tool_choice="any" when require_tools is True to force LLM to call at least one tool
        llm = get_model(model_name=model_name)
        if require_tools:
            llm_with_tools = llm.bind_tools(tools, tool_choice="any")
            log("Tool choice set to 'any' - forcing LLM to use tools", node=operation_type, level="DEBUG")
        else:
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
        tool_call_counts = {}  # Track tool calls for summary
        tool_call_failures = 0  # Track how many attempted tool calls errored
        while iteration < max_iterations:
            iteration += 1
            log(f"Invoking LLM for {operation_type} (iteration {iteration}/{max_iterations})...", node=operation_type)
            response = _invoke_with_retry(llm_with_tools, messages, operation_type)
            log(f"LLM invocation completed for {operation_type} (iteration {iteration})", node=operation_type, level="DEBUG")

            # Check if response has tool calls
            if not (hasattr(response, 'tool_calls') and response.tool_calls):
                # No more tool calls, break and get structured output
                # Debug: log what the response looks like
                log(f"Response type: {type(response).__name__}, has tool_calls attr: {hasattr(response, 'tool_calls')}", node=operation_type, level="DEBUG")
                if hasattr(response, 'content'):
                    content_preview = str(response.content)[:200] if response.content else "(empty)"
                    log(f"Response content preview: {content_preview}", node=operation_type, level="DEBUG")
                break

            log(f"LLM made {len(response.tool_calls)} tool call(s), iteration {iteration}", node=operation_type)

            # Execute tool calls
            tool_messages = []
            for tool_call in response.tool_calls:
                tool_name = tool_call.get("name", "")
                tool_args = tool_call.get("args", {})

                # Track tool call for summary
                tool_call_counts[tool_name] = tool_call_counts.get(tool_name, 0) + 1

                # Log tool call details (full args for debugging parameter issues)
                log(f"Executing tool: {tool_name} with args: {json.dumps(tool_args, default=str)}", node=operation_type, level="DEBUG")
                
                # Find and execute the tool
                tool_result = None
                for tool in tools:
                    if tool.name == tool_name:
                        try:
                            tool_result = tool.func(**tool_args)
                            # Log tool result (truncate if too long)
                            result_str = str(tool_result)
                            if len(result_str) > 200:
                                result_str = result_str[:200] + "... (truncated)"
                            log(f"Tool {tool_name} result: {result_str}", node=operation_type, level="DEBUG")
                            break
                        except Exception as e:
                            tool_result = f"Error: {e!s}"
                            log(f"Error executing tool {tool_name}: {e}", node=operation_type, level="ERROR")
                            import traceback
                            log(f"Tool error traceback: {traceback.format_exc()}", node=operation_type, level="DEBUG")
                
                if tool_result is None:
                    tool_result = f"Tool {tool_name} not found"
                    log(f"Tool {tool_name} not found in available tools: {[t.name for t in tools]}", node=operation_type, level="WARNING")

                if _is_tool_call_error(tool_result):
                    tool_call_failures += 1

                # Create tool message
                tool_messages.append(ToolMessage(
                    content=str(tool_result),
                    tool_call_id=tool_call.get("id", "")
                ))
            
            # Add tool results and continue
            messages.append(response)
            messages.extend(tool_messages)

        # Now get structured output with final messages (including tool results)
        messages.append(HumanMessage(content="Based on the information gathered, please provide your response in the required structured format."))
        # Bind the schema on the TOOL-FREE llm: langchain-google-genai merges the
        # response schema onto any bound tools, and Gemini rejects tools+response_schema
        # in one call (400). The tool loop is done, so no tools are needed here.
        log(f"Requesting structured output for {operation_type}...", node=operation_type, level="DEBUG")
        structured_llm = llm.with_structured_output(response_model)
        result = _invoke_with_retry(structured_llm, messages, operation_type)
        log(f"Structured output received for {operation_type}", node=operation_type, level="DEBUG")
        
        # Response is already a validated Pydantic model!
        log(f"Successfully received structured response for {operation_type}", node=operation_type)

        # Log tool call summary (helpful for debugging sheets updates, etc.)
        if tool_call_counts:
            summary = ", ".join(f"{name}: {count}" for name, count in sorted(tool_call_counts.items()))
            log(f"Tool calls summary: {summary}", node=operation_type)

            total_tool_calls = sum(tool_call_counts.values())
            if require_tools and total_tool_calls > 0 and tool_call_failures == total_tool_calls:
                log(f"WARNING: All {total_tool_calls} tool call(s) failed for {operation_type} - "
                    "treating as failure instead of reporting false success",
                    node=operation_type, level="WARNING")
                raise ToolCallFailedException(f"All {total_tool_calls} tool call(s) failed for {operation_type}")
        else:
            # No tools were called - this is often a sign of LLM hallucination
            log(f"WARNING: LLM completed {operation_type} without calling any tools!", node=operation_type, level="WARNING")
            if require_tools:
                raise NoToolsCalledException(f"LLM completed {operation_type} without calling any tools")

        return result

    except (NoToolsCalledException, ToolCallFailedException):
        # These signal a specific, actionable failure mode to the caller (e.g.
        # update_sheets.py retries the update) - let them propagate instead of
        # being swallowed by the generic fallback below.
        raise
    except Exception as e:
        log(f"Error invoking LLM for {operation_type}: {e}", node=operation_type, level="ERROR")
        import traceback
        log(f"Traceback: {traceback.format_exc()}", node=operation_type, level="ERROR")
        # Return empty response with proper structure
        # Use Pydantic model introspection to provide defaults for required fields
        try:
            # Try to create with empty dict first (works if all fields have defaults)
            return response_model()
        except (TypeError, ValueError, AttributeError):
            # Build defaults from model fields
            defaults = {}
            try:
                # Use Pydantic v2 model_fields if available
                if hasattr(response_model, 'model_fields'):
                    for field_name, field_info in response_model.model_fields.items():
                        if field_info.is_required():
                            # Provide sensible defaults based on field type
                            field_type = str(field_info.annotation) if hasattr(field_info, 'annotation') else ''
                            if 'list' in field_type.lower() or field_name in ['updated_veps', 'alerts']:
                                defaults[field_name] = []
                            elif field_name == 'success':
                                defaults[field_name] = False
                            elif 'dict' in field_type.lower():
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
            except (OSError, ValueError, TypeError, KeyError, RuntimeError) as final_error:
                log(f"Could not create {response_model.__name__} with defaults: {final_error}", node=operation_type, level="ERROR")
                # Last resort: try with minimal known defaults
                minimal_defaults = {
                    'updated_veps': [],
                    'alerts': [],
                    'success': False,
                }
                try:
                    return response_model(**{k: v for k, v in minimal_defaults.items() if hasattr(response_model, k)})
                except (TypeError, ValueError, AttributeError):
                    # This will fail but at least we tried everything
                    raise ValueError(f"Could not create {response_model.__name__} with defaults. Error: {final_error}")


def invoke_llm_check(
    check_type: str,
    state_context: dict[str, Any],
    system_prompt: str,
    user_prompt: str,
    response_model: type[T]
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


def invoke_llm_fetch(
    fetch_type: str,
    state_context: dict[str, Any],
    system_prompt: str,
    user_prompt: str,
    response_model: type[T]
) -> T:
    """Invoke lightweight LLM to fetch context data with GitHub MCP tools.

    Used by fetch nodes (check_deadlines, check_activity, check_compliance, check_exceptions)
    to gather raw data without analysis. The LLM uses GitHub MCP tools to fetch data and
    returns it in a structured format for later analysis by analyze_combined.

    Args:
        fetch_type: Type of fetch ("deadlines", "activity", "compliance", "exceptions")
        state_context: Current state context (veps, release_schedule, etc.)
        system_prompt: System prompt describing what data to fetch
        user_prompt: User prompt with specific instructions
        response_model: Pydantic model for structured output (typically FetchResponse)

    Returns:
        Validated Pydantic model instance with context data
    """
    return invoke_llm_with_tools(fetch_type, state_context, system_prompt, user_prompt, response_model, mcp_names=("github",))
