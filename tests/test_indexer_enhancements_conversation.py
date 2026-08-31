"""Unit tests for conversation-signal enrichment on kubevirt/enhancements PRs.

WS3 originally grounded reviewer sentiment only for implementation PRs
(kubevirt/kubevirt). This extends the same enrichment to proposal PRs
(kubevirt/enhancements) since design-phase attention lives there. Heavy
MCP-tool plumbing (list_pull_requests) is stubbed with a minimal duck-typed
object exposing just `.name`/`.func`, matching what index_enhancements_prs
actually touches - the pure signal derivation itself
(derive_pr_conversation_signals) is already covered elsewhere.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from services import indexer


@dataclass
class _FakeTool:
    name: str
    func: Callable[..., Any]


def _make_list_prs_tool(prs: list[dict[str, Any]]) -> _FakeTool:
    def _func(**kwargs):
        # First page returns the fixture PRs; further pages must stop
        # pagination by returning empty (len(prs) < 30 already does this,
        # but be explicit for page > 1 to avoid infinite loops in tests).
        if kwargs.get("page", 1) > 1:
            return []
        return prs

    return _FakeTool(name="list_pull_requests", func=_func)


def _open_vep_pr(number: int = 62) -> dict[str, Any]:
    return {
        "number": number,
        "title": f"VEP {number}: Some proposal",
        "state": "open",
        "body": "Design proposal body.",
        "html_url": f"https://github.com/kubevirt/enhancements/pull/{number}",
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-02T00:00:00Z",
        "merged": False,
        "merged_at": None,
        "review_comments": 0,
        "comments": 0,
    }


class TestIndexEnhancementsPrsConversationEnrichment:
    def test_fetch_reviews_true_attaches_conversation(self, monkeypatch):
        pr = _open_vep_pr()
        monkeypatch.setattr(
            indexer, "get_mcp_tools_by_name", lambda *names: [_make_list_prs_tool([pr])]
        )
        monkeypatch.setattr(indexer, "API_DELAY", 0)

        conversation_payload = {
            "latestReviews": {"nodes": [{"author": {"login": "alice"}, "state": "APPROVED", "submittedAt": "2026-01-03T00:00:00Z"}]},
            "commits": {"nodes": []},
            "comments": {"nodes": []},
            "timelineItems": {"nodes": []},
        }
        monkeypatch.setattr(
            indexer, "fetch_pr_conversation",
            lambda owner, repo, number: conversation_payload,
        )

        result = indexer.index_enhancements_prs(days_back=None, fetch_reviews=True)

        assert len(result) == 1
        assert result[0]["vep_issue_number"] == 62
        assert "conversation" in result[0]
        assert result[0]["conversation"]["approved_count"] == 1

    def test_fetch_reviews_false_skips_conversation(self, monkeypatch):
        pr = _open_vep_pr()
        monkeypatch.setattr(
            indexer, "get_mcp_tools_by_name", lambda *names: [_make_list_prs_tool([pr])]
        )

        def _fail_if_called(*args, **kwargs):
            raise AssertionError("fetch_pr_conversation should not be called when fetch_reviews=False")

        monkeypatch.setattr(indexer, "fetch_pr_conversation", _fail_if_called)

        result = indexer.index_enhancements_prs(days_back=None, fetch_reviews=False)

        assert len(result) == 1
        assert "conversation" not in result[0]
