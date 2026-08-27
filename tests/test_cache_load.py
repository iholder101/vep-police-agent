"""Regression tests for _load_cached_index resilience.

A stale cache file with a timezone-naive `cached_at` must not crash the whole
run (it did: `datetime.now(UTC) - naive` raised TypeError, which the except
clause failed to catch, taking the agent down on container restart).
"""

import json
from datetime import UTC, datetime

from services.indexer import _load_cached_index


def test_naive_cached_at_regenerates_instead_of_crashing(tmp_path):
    cache = tmp_path / "index_cache.json"
    # tz-naive timestamp (no offset) - the shape that crashed startup.
    cache.write_text(json.dumps({"cached_at": "2026-08-26T12:00:00", "foo": "bar"}))
    assert _load_cached_index(cache, max_age_minutes=60) is None


def test_fresh_aware_cache_loads(tmp_path):
    cache = tmp_path / "index_cache.json"
    cache.write_text(json.dumps({"cached_at": datetime.now(UTC).isoformat(), "foo": "bar"}))
    result = _load_cached_index(cache, max_age_minutes=60)
    assert result is not None
    assert result.get("foo") == "bar"
    assert "cached_at" not in result  # metadata stripped


def test_missing_cache_returns_none(tmp_path):
    assert _load_cached_index(tmp_path / "nope.json", max_age_minutes=60) is None
