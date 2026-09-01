"""Regression test for invoke_llm_with_tools' except-path default builder.

When the LLM call throws, invoke_llm_with_tools falls back to constructing
the response_model with synthesized defaults for its required fields. That
fallback string-matched the field's type annotation for 'List'/'Dict'
(capitalized) to decide when to default to []/{}. Pydantic v2 stringifies
list[str] annotations in lowercase, so any required list field not literally
named 'updated_veps' or 'alerts' (e.g. FetchResponse.context_updates) fell
through to None, which then failed Pydantic validation and crashed the node.
"""

from unittest.mock import MagicMock, patch

import pytest

from services.llm_helper import (
    ToolCallFailedException,
    _is_tool_call_error,
    invoke_llm_with_tools,
)
from services.response_models import FetchResponse


def test_exception_path_defaults_required_list_field_to_empty_list():
    with patch("services.llm_helper.get_mcp_tools_by_name", side_effect=RuntimeError("boom")):
        result = invoke_llm_with_tools(
            "check_deadlines", {}, "system prompt", "user prompt", FetchResponse
        )

    assert result.context_updates == []


def test_is_tool_call_error_detects_unambiguous_error_markers():
    assert _is_tool_call_error("Error calling tool write_range: boom")
    assert _is_tool_call_error("error: something bad happened")
    assert _is_tool_call_error("Something went wrong. MCP error -32603: bad params")


def test_is_tool_call_error_does_not_flag_normal_output():
    assert not _is_tool_call_error("Wrote 5 rows successfully")
    assert not _is_tool_call_error("No errors found in the sheet")
    assert not _is_tool_call_error(123)


def _fake_tool(name, func):
    tool = MagicMock()
    tool.name = name
    tool.func = func
    return tool


def _fake_llm(responses):
    """Build a fake llm where .invoke() returns each response in order and
    .bind_tools()/.with_structured_output() return usable stand-ins."""
    llm = MagicMock()
    responses_iter = iter(responses)
    llm.invoke.side_effect = lambda messages: next(responses_iter)
    llm.bind_tools.return_value = llm
    llm.with_structured_output.return_value = MagicMock(invoke=MagicMock(return_value=MagicMock()))
    return llm


def test_all_tool_calls_failing_raises_tool_call_failed_exception():
    """Bug B: every attempted sheets write errors - this must be a failure,
    not a silently-reported success."""
    failing_tool = _fake_tool(
        "write_range",
        lambda **kwargs: "Error calling tool write_range: MCP error -32603: Missing required parameters",
    )
    llm = _fake_llm([
        MagicMock(tool_calls=[{"name": "write_range", "args": {"spreadsheetId": "x"}, "id": "1"}], content=""),
        MagicMock(tool_calls=[], content="done"),
    ])

    with patch("services.llm_helper.get_mcp_tools_by_name", return_value=[failing_tool]), \
         patch("services.llm_helper.get_model", return_value=llm), \
         patch("config.get_model_for_node", return_value="fake-model"), \
         pytest.raises(ToolCallFailedException):
        invoke_llm_with_tools(
            "update_sheets", {}, "system prompt", "user prompt", FetchResponse,
            mcp_names=("google-sheets",), require_tools=True,
        )


def test_successful_tool_call_does_not_raise():
    succeeding_tool = _fake_tool("write_range", lambda **kwargs: "Wrote 5 rows successfully")
    llm = _fake_llm([
        MagicMock(tool_calls=[{"name": "write_range", "args": {"spreadsheetId": "x"}, "id": "1"}], content=""),
        MagicMock(tool_calls=[], content="done"),
    ])

    with patch("services.llm_helper.get_mcp_tools_by_name", return_value=[succeeding_tool]), \
         patch("services.llm_helper.get_model", return_value=llm), \
         patch("config.get_model_for_node", return_value="fake-model"):
        # Should not raise - at least one tool call succeeded.
        invoke_llm_with_tools(
            "update_sheets", {}, "system prompt", "user prompt", FetchResponse,
            mcp_names=("google-sheets",), require_tools=True,
        )
