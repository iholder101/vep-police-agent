"""Unit tests for services.indexer.derive_pr_conversation_signals.

WS3: grounds reviewer sentiment in real review states/comments instead of
counts. All date arithmetic below uses real datetimes (no hardcoded day
counts), per repo test convention. New feature - red-green discipline does
not apply (see tests/test_indexer_phase_detail.py for the mirrored style).
"""

from datetime import UTC, datetime, timedelta

from services.indexer import derive_pr_conversation_signals

NOW = datetime(2026, 8, 31, 12, 0, 0, tzinfo=UTC)


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _review(author: str, state: str, submitted_at: datetime | None) -> dict:
    return {
        "author": {"login": author},
        "state": state,
        "submittedAt": _iso(submitted_at) if submitted_at else None,
    }


def _commit_node(committed_at: datetime, pushed_at: datetime | None = None) -> dict:
    return {
        "commit": {
            "committedDate": _iso(committed_at),
            "pushedDate": _iso(pushed_at) if pushed_at else None,
        }
    }


def _comment(author: str, created_at: datetime) -> dict:
    return {"author": {"login": author}, "createdAt": _iso(created_at)}


def _dismissal(created_at: datetime) -> dict:
    return {"__typename": "ReviewDismissedEvent", "createdAt": _iso(created_at)}


class TestChangesRequestedAddressed:
    def test_changes_requested_then_newer_commit_is_addressed(self):
        cr_at = NOW - timedelta(days=5)
        commit_at = NOW - timedelta(days=2)
        payload = {
            "latestReviews": {"nodes": [_review("alice", "CHANGES_REQUESTED", cr_at)]},
            "commits": {"nodes": [_commit_node(commit_at)]},
        }

        signals = derive_pr_conversation_signals(payload, NOW)

        assert signals["changes_requested_unaddressed"] is False
        assert signals["unaddressed_by"] == []
        assert signals["changes_requested_count"] == 1

    def test_changes_requested_no_later_commit_or_dismissal_is_unaddressed(self):
        cr_at = NOW - timedelta(days=5)
        old_commit_at = NOW - timedelta(days=10)
        payload = {
            "latestReviews": {"nodes": [_review("alice", "CHANGES_REQUESTED", cr_at)]},
            "commits": {"nodes": [_commit_node(old_commit_at)]},
        }

        signals = derive_pr_conversation_signals(payload, NOW)

        assert signals["changes_requested_unaddressed"] is True
        assert signals["unaddressed_by"] == ["alice"]

    def test_changes_requested_later_dismissed_is_addressed(self):
        cr_at = NOW - timedelta(days=5)
        dismissed_at = NOW - timedelta(days=3)
        payload = {
            "latestReviews": {"nodes": [_review("alice", "CHANGES_REQUESTED", cr_at)]},
            "timelineItems": {"nodes": [_dismissal(dismissed_at)]},
        }

        signals = derive_pr_conversation_signals(payload, NOW)

        assert signals["changes_requested_unaddressed"] is False
        assert signals["unaddressed_by"] == []


class TestCountsAndAggregate:
    def test_mixed_approved_and_changes_requested_counts(self):
        cr_at = NOW - timedelta(days=5)
        payload = {
            "latestReviews": {
                "nodes": [
                    _review("alice", "CHANGES_REQUESTED", cr_at),
                    _review("bob", "APPROVED", NOW - timedelta(days=1)),
                    _review("carol", "APPROVED", NOW - timedelta(days=1)),
                ]
            },
        }

        signals = derive_pr_conversation_signals(payload, NOW)

        assert signals["approved_count"] == 2
        assert signals["changes_requested_count"] == 1
        assert signals["review_count"] == 3
        assert signals["changes_requested_unaddressed"] is True
        assert signals["unaddressed_by"] == ["alice"]


class TestLastCommentAndStaleness:
    def test_last_comment_is_max_across_comments_and_reviews_bots_excluded(self):
        comment_at = NOW - timedelta(days=4)
        review_at = NOW - timedelta(days=1)
        bot_comment_at = NOW - timedelta(hours=1)
        payload = {
            "comments": {
                "nodes": [
                    _comment("dave", comment_at),
                    _comment("kubevirt-bot", bot_comment_at),
                ]
            },
            "latestReviews": {"nodes": [_review("erin", "APPROVED", review_at)]},
        }

        signals = derive_pr_conversation_signals(payload, NOW)

        assert signals["last_comment_author"] == "erin"
        assert signals["last_comment_date"] == review_at.isoformat()
        expected_days = (NOW - review_at).days
        assert signals["days_since_last_human_activity"] == expected_days

    def test_all_bot_activity_yields_no_human_signal(self):
        payload = {
            "comments": {
                "nodes": [
                    _comment("kubevirt-bot", NOW - timedelta(hours=1)),
                    _comment("some-app[bot]", NOW - timedelta(hours=2)),
                    _comment("k8s-ci-robot", NOW - timedelta(hours=3)),
                ]
            },
        }

        signals = derive_pr_conversation_signals(payload, NOW)

        assert signals["last_comment_author"] is None
        assert signals["last_comment_date"] is None
        assert signals["days_since_last_human_activity"] is None


class TestEmptyAndMissingNodes:
    def test_empty_payload_yields_safe_defaults(self):
        signals = derive_pr_conversation_signals({}, NOW)

        assert signals["reviews"] == []
        assert signals["approved_count"] == 0
        assert signals["changes_requested_count"] == 0
        assert signals["review_count"] == 0
        assert signals["changes_requested_unaddressed"] is False
        assert signals["unaddressed_by"] == []
        assert signals["last_comment_author"] is None
        assert signals["last_comment_date"] is None
        assert signals["days_since_last_human_activity"] is None
        assert signals["last_commit_pushed_at"] is None

    def test_none_payload_yields_safe_defaults(self):
        signals = derive_pr_conversation_signals(None, NOW)

        assert signals["reviews"] == []
        assert signals["changes_requested_unaddressed"] is False

    def test_missing_nested_nodes_do_not_raise(self):
        payload = {
            "latestReviews": {},
            "commits": None,
            "comments": {"nodes": None},
            "timelineItems": {"nodes": [{"__typename": "IssueComment"}]},
        }

        signals = derive_pr_conversation_signals(payload, NOW)

        assert signals["reviews"] == []
        assert signals["last_commit_pushed_at"] is None

    def test_falls_back_to_committed_date_when_pushed_date_missing(self):
        committed_at = NOW - timedelta(days=1)
        payload = {"commits": {"nodes": [_commit_node(committed_at, pushed_at=None)]}}

        signals = derive_pr_conversation_signals(payload, NOW)

        assert signals["last_commit_pushed_at"] == committed_at.isoformat()
