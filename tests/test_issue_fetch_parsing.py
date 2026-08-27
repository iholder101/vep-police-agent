"""Unit tests for the MCP issue/page response parsing helpers in services.indexer.

These are pure functions that normalize the various shapes MCP GitHub tools
return (native dicts, JSON strings, double-encoded JSON strings, content
wrappers, bare lists) into a consistent list-of-issues or dict form.
"""

import json

from services.indexer import _coerce_page_to_dict, _unwrap_issues_dict


class TestUnwrapIssuesDict:
    def test_issues_key(self):
        assert _unwrap_issues_dict({"issues": [{"number": 1}]}) == [{"number": 1}]

    def test_items_key(self):
        assert _unwrap_issues_dict({"items": [{"number": 1}]}) == [{"number": 1}]

    def test_data_key(self):
        assert _unwrap_issues_dict({"data": [{"number": 1}]}) == [{"number": 1}]

    def test_content_json_string(self):
        d = {"content": json.dumps([{"number": 2}])}
        assert _unwrap_issues_dict(d) == [{"number": 2}]

    def test_content_list_of_text_blocks(self):
        d = {"content": [{"type": "text", "text": json.dumps([{"number": 3}])}]}
        assert _unwrap_issues_dict(d) == [{"number": 3}]

    def test_single_issue_object(self):
        d = {"number": 7, "title": "x"}
        assert _unwrap_issues_dict(d) == [d]

    def test_unrecognized_shape_returns_none(self):
        assert _unwrap_issues_dict({"foo": "bar"}) is None


class TestCoercePageToDict:
    def test_dict_passthrough(self):
        d = {"issues": [{"number": 1}], "totalCount": 1}
        assert _coerce_page_to_dict(d) is d

    def test_json_string_of_dict(self):
        d = {"issues": [{"number": 1}]}
        assert _coerce_page_to_dict(json.dumps(d)) == d

    def test_double_encoded_json_string(self):
        d = {"issues": [{"number": 1}]}
        double_encoded = json.dumps(json.dumps(d))
        assert _coerce_page_to_dict(double_encoded) == d

    def test_bare_list_wrapped(self):
        items = [{"number": 1}, {"number": 2}]
        assert _coerce_page_to_dict(items) == {"issues": items}

    def test_plain_error_string_returns_none(self):
        assert _coerce_page_to_dict("failed: 403") is None
