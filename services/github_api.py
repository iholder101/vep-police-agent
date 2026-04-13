"""GitHub REST API helpers for write operations and review data.

Provides thin wrappers around the GitHub REST API for controlled write
operations (e.g. posting issue comments) and fetching PR review data.
These bypass the read-only MCP layer so that LLM-driven nodes stay
read-only while deterministic pipeline nodes can perform targeted writes.
"""

import os
from typing import Any, Dict, List, Optional

import requests

from services.utils import log

API_BASE = "https://api.github.com"

# Marker embedded in CC comments posted by the agent so we can detect prior CCs.
CC_REVIEWERS_MARKER = "<!-- vep-police-agent:cc-reviewers -->"


def _headers() -> Dict[str, str]:
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        raise RuntimeError("GITHUB_TOKEN environment variable not set")
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def add_issue_comment(owner: str, repo: str, issue_number: int, body: str) -> Optional[Dict[str, Any]]:
    """Post a comment on a GitHub issue or pull request.

    Args:
        owner: Repository owner (e.g. "kubevirt")
        repo: Repository name (e.g. "kubevirt")
        issue_number: Issue or PR number
        body: Markdown comment body

    Returns:
        Parsed JSON response on success, None on failure
    """
    url = f"{API_BASE}/repos/{owner}/{repo}/issues/{issue_number}/comments"
    try:
        resp = requests.post(url, json={"body": body}, headers=_headers(), timeout=30)
        resp.raise_for_status()
        log(f"Posted comment on {owner}/{repo}#{issue_number}", node="github_api")
        return resp.json()
    except requests.RequestException as e:
        log(f"Failed to post comment on {owner}/{repo}#{issue_number}: {e}",
            node="github_api", level="ERROR")
        return None


def get_issue_comments(owner: str, repo: str, issue_number: int) -> List[Dict[str, Any]]:
    """Fetch all comments on a GitHub issue or pull request.

    Args:
        owner: Repository owner
        repo: Repository name
        issue_number: Issue or PR number

    Returns:
        List of comment dicts (may be empty on error)
    """
    comments: List[Dict[str, Any]] = []
    url = f"{API_BASE}/repos/{owner}/{repo}/issues/{issue_number}/comments"
    params: Dict[str, Any] = {"per_page": 100, "page": 1}

    try:
        while True:
            resp = requests.get(url, params=params, headers=_headers(), timeout=30)
            resp.raise_for_status()
            page = resp.json()
            if not page:
                break
            comments.extend(page)
            if len(page) < 100:
                break
            params["page"] += 1
    except requests.RequestException as e:
        log(f"Failed to fetch comments for {owner}/{repo}#{issue_number}: {e}",
            node="github_api", level="ERROR")

    return comments


def get_pull_request_reviews(owner: str, repo: str, pull_number: int) -> List[Dict[str, Any]]:
    """Fetch all reviews on a pull request.

    Args:
        owner: Repository owner
        repo: Repository name
        pull_number: PR number

    Returns:
        List of review dicts, each containing at least ``user.login`` and ``state``
        (APPROVED, CHANGES_REQUESTED, COMMENTED, DISMISSED).
    """
    reviews: List[Dict[str, Any]] = []
    url = f"{API_BASE}/repos/{owner}/{repo}/pulls/{pull_number}/reviews"
    params: Dict[str, Any] = {"per_page": 100, "page": 1}

    try:
        while True:
            resp = requests.get(url, params=params, headers=_headers(), timeout=30)
            resp.raise_for_status()
            page = resp.json()
            if not page:
                break
            reviews.extend(page)
            if len(page) < 100:
                break
            params["page"] += 1
    except requests.RequestException as e:
        log(f"Failed to fetch reviews for {owner}/{repo}#{pull_number}: {e}",
            node="github_api", level="ERROR")

    return reviews


def has_cc_comment(owner: str, repo: str, issue_number: int,
                   cooldown_days: int = 7) -> bool:
    """Check whether a CC-reviewers comment was already posted recently.

    Looks for the ``CC_REVIEWERS_MARKER`` HTML comment in issue/PR comments.
    If the most recent CC is older than *cooldown_days*, returns False so
    a fresh CC can be sent.

    Args:
        owner: Repository owner
        repo: Repository name
        issue_number: Issue or PR number
        cooldown_days: Minimum days between CC comments

    Returns:
        True if a recent CC exists (skip posting), False otherwise
    """
    from datetime import datetime, timezone, timedelta

    comments = get_issue_comments(owner, repo, issue_number)
    cutoff = datetime.now(timezone.utc) - timedelta(days=cooldown_days)

    for comment in reversed(comments):  # newest first
        body = comment.get("body", "")
        if CC_REVIEWERS_MARKER not in body:
            continue
        created = comment.get("created_at", "")
        try:
            created_dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            # Can't parse date -- treat as recent to be safe
            return True
        if created_dt >= cutoff:
            return True
        # Found the marker but it's older than cooldown -- allow re-CC
        return False

    return False
