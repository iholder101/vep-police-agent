"""Regression test for invoke_llm_with_tools' except-path default builder.

When the LLM call throws, invoke_llm_with_tools falls back to constructing
the response_model with synthesized defaults for its required fields. That
fallback string-matched the field's type annotation for 'List'/'Dict'
(capitalized) to decide when to default to []/{}. Pydantic v2 stringifies
list[str] annotations in lowercase, so any required list field not literally
named 'updated_veps' or 'alerts' (e.g. FetchResponse.context_updates) fell
through to None, which then failed Pydantic validation and crashed the node.
"""

from unittest.mock import patch

from services.llm_helper import invoke_llm_with_tools
from services.response_models import FetchResponse


def test_exception_path_defaults_required_list_field_to_empty_list():
    with patch("services.llm_helper.get_mcp_tools_by_name", side_effect=RuntimeError("boom")):
        result = invoke_llm_with_tools(
            "check_deadlines", {}, "system prompt", "user prompt", FetchResponse
        )

    assert result.context_updates == []
