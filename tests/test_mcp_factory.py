"""Regression test for MCP tool input-schema extraction (Bug B, fix 1).

The installed mcp SDK (>=2.0) renamed the Python attribute exposing a tool's
JSON input schema from camelCase 'inputSchema' to snake_case 'input_schema';
'inputSchema' survives only as a pydantic serialization alias and is not
reachable via getattr() on those versions. The old code only checked
'inputSchema', so on the new SDK the schema was never read, args_schema stayed
None, and StructuredTool fell back to a generic **kwargs-only signature -
causing the LLM to emit empty {"kwargs": {}} calls that fail with "Missing
required parameters".

_get_tool_input_schema() must find the schema under either attribute name.
"""

from services.mcp_factory import (
    _create_args_schema_from_json_schema,
    _get_tool_input_schema,
)

_WRITE_RANGE_SCHEMA = {
    "type": "object",
    "properties": {
        "spreadsheetId": {"type": "string", "description": "The spreadsheet ID"},
        "range": {"type": "string", "description": "The A1 range to write"},
        "values": {"type": "array", "items": {"type": "array", "items": {"type": "string"}}},
    },
    "required": ["spreadsheetId", "range", "values"],
}


class _FakeMCPToolSnakeCase:
    """Mimics the mcp>=2.0 SDK: schema only reachable via 'input_schema'."""
    name = "write_range"
    description = "Write data to a range"
    input_schema = _WRITE_RANGE_SCHEMA


class _FakeMCPToolCamelCase:
    """Mimics the old mcp SDK: schema only reachable via 'inputSchema'."""
    name = "write_range"
    description = "Write data to a range"
    inputSchema = _WRITE_RANGE_SCHEMA


def test_get_tool_input_schema_reads_snake_case_attribute():
    schema = _get_tool_input_schema(_FakeMCPToolSnakeCase())
    assert schema == _WRITE_RANGE_SCHEMA


def test_get_tool_input_schema_reads_camel_case_attribute():
    schema = _get_tool_input_schema(_FakeMCPToolCamelCase())
    assert schema == _WRITE_RANGE_SCHEMA


def test_get_tool_input_schema_returns_none_when_absent():
    class _NoSchemaTool:
        name = "mystery_tool"

    assert _get_tool_input_schema(_NoSchemaTool()) is None


def test_args_schema_built_from_snake_case_schema_has_required_field():
    schema = _get_tool_input_schema(_FakeMCPToolSnakeCase())
    args_schema = _create_args_schema_from_json_schema("write_range", schema)

    assert args_schema is not None
    assert "spreadsheetId" in args_schema.model_fields
    assert args_schema.model_fields["spreadsheetId"].is_required()
    # Not a generic kwargs-only schema - it has the real, typed fields.
    assert set(args_schema.model_fields.keys()) == {"spreadsheetId", "range", "values"}


def test_args_schema_built_from_camel_case_schema_has_required_field():
    schema = _get_tool_input_schema(_FakeMCPToolCamelCase())
    args_schema = _create_args_schema_from_json_schema("write_range", schema)

    assert args_schema is not None
    assert "spreadsheetId" in args_schema.model_fields
    assert args_schema.model_fields["spreadsheetId"].is_required()
