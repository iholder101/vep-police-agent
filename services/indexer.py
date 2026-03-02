"""Indexing service to pre-fetch key information for VEP discovery.

This module provides functions to index critical information before LLM processing,
ensuring the LLM has a complete picture of what exists rather than having to discover it.
"""

import re
import json
import os
import time
from pathlib import Path
from typing import Dict, List, Any, Optional
from datetime import datetime, timedelta, timezone
from services.utils import log
from services.mcp_factory import get_mcp_tools_by_name

# Cache file path (relative to project root)
CACHE_FILE = Path(__file__).parent.parent / "cache" / "index_cache.json"


def _get_api_delay() -> float:
    """Get appropriate API delay based on authentication status.

    With GITHUB_TOKEN: 5000 requests/hour limit -> 0.1s delay is safe
    Without token: 60 requests/hour limit -> 2.0s delay needed

    Returns:
        Delay in seconds between API calls
    """
    github_token = os.environ.get("GITHUB_TOKEN")
    if github_token:
        return 0.1  # 100ms delay with token (safe for 5000/hour)
    else:
        return 2.0  # 2s delay without token (conservative for 60/hour)


# Dynamic API delay based on authentication
API_DELAY = _get_api_delay()


def _call_with_retry(tool_func, max_retries=3, delay=5, **kwargs):
    """Call a tool function with retry logic for rate limit errors.

    Args:
        tool_func: The tool function to call
        max_retries: Maximum number of retries
        delay: Initial delay in seconds (doubles on each retry)
        **kwargs: Arguments to pass to tool_func

    Returns:
        Result from tool_func, or None if all retries fail
    """
    for attempt in range(max_retries):
        try:
            result = tool_func(**kwargs)
            return result
        except Exception as e:
            error_str = str(e).lower()
            # Check if it's a rate limit error
            if "rate limit" in error_str or "rate_limit" in error_str:
                if attempt < max_retries - 1:
                    # For IP-based rate limits (60/hour), wait longer
                    # Calculate wait time: if reset is on the hour, wait until next hour
                    wait_time = delay * (2 ** attempt)  # Exponential backoff: 5s, 10s, 20s
                    # For IP-based limits, also add a base delay to get closer to next hour
                    if "62." in str(e) or "79." in str(e) or "ip" in error_str:
                        # IP-based rate limit - wait longer (typically resets on the hour)
                        current_minute = datetime.now().minute
                        minutes_until_hour = 60 - current_minute
                        if minutes_until_hour < 60 and minutes_until_hour > 0:
                            # Add extra wait to get closer to hour boundary
                            wait_time = max(wait_time, minutes_until_hour * 60 - 30)  # Wait until 30s before next hour
                    
                    log(f"Rate limit hit, waiting {wait_time}s before retry {attempt + 1}/{max_retries}", node="indexer", level="WARNING")
                    time.sleep(wait_time)
                    continue
                else:
                    log(f"Rate limit error after {max_retries} retries. Error: {str(e)[:200]}", node="indexer", level="ERROR")
                    log("Rate limit typically resets on the hour. Consider waiting or ensuring GITHUB_TOKEN is being used.", node="indexer", level="WARNING")
                    return None
            else:
                # Not a rate limit error, re-raise
                raise
    return None


def _parse_version(version_str: str) -> tuple:
    """Parse version string (e.g., 'v1.11') into tuple for numerical sorting.
    
    Returns:
        Tuple (major, minor) for sorting, e.g., ('v1', 11) for 'v1.11'
    """
    match = re.match(r'v(\d+)\.(\d+)', version_str)
    if match:
        return (int(match.group(1)), int(match.group(2)))
    return (0, 0)


def _sort_versions_numerically(versions: List[str]) -> List[str]:
    """Sort version strings numerically (v1.11 > v1.8, not alphabetically)."""
    return sorted(versions, key=_parse_version, reverse=True)

def _process_schedule_content(schedule_content: Any, version: str, sorted_versions: List[str] | None = None) -> Dict[str, Any]:
    """Common processing for schedule content from MCP tool."""
    # MCP GitHub tool returns plain markdown in "content"
    # Extract and unescape for proper table parsing
    markdown_content = ""
    if isinstance(schedule_content, dict):
        markdown_content = schedule_content.get("content", "")
        if not markdown_content:
            # Fallback regex extraction from JSON string (rare case)
            json_str = str(schedule_content)
            content_match = re.search(r'"content"\s*:\s*"([^"]+)"', json_str, re.DOTALL)
            if content_match:
                markdown_content = content_match.group(1)
    else:
        markdown_content = str(schedule_content)

    # Unescape JSON escapes (newlines, pipes, quotes)
    full_content = markdown_content.replace('\\n', '\n').replace('\\|', '|').replace('\\"', '"')

    log(f"Extracted markdown length for {version}: {len(full_content)}", node="indexer")

    content_for_context = full_content[:10000] if len(full_content) > 10000 else full_content

    phase, parsed_deadlines = compute_release_phase({"schedule_content": full_content})

    return {
        "current_release": version,
        "schedule_path": f"releases/{version}/schedule.md",
        "schedule_content": content_for_context,
        "all_versions_found": sorted_versions or [],
        "release_deadlines": parsed_deadlines,
        "release_phase": phase,
    }

def index_release_schedule() -> Optional[Dict[str, Any]]:
    """Index the current release schedule from kubevirt/sig-release.
    
    Lists the releases directory, finds all versions, sorts numerically,
    and fetches the schedule for the newest release.
    
    Returns:
        Dict with current_release version and release_schedule data, or None if not found
    """
    log("Indexing release schedule from kubevirt/sig-release", node="indexer")
    
    try:
        tools = get_mcp_tools_by_name("github")
        
        # Find tools for directory listing and file reading
        list_dir_tool = None
        get_file_tool = None
        
        # Look for directory listing tool first
        for tool in tools:
            tool_name_lower = tool.name.lower()
            if "list" in tool_name_lower and ("directory" in tool_name_lower or "contents" in tool_name_lower or "dir" in tool_name_lower):
                list_dir_tool = tool
                break
        
        # Also find file reading tool for later
        tool_names_to_try = [
            "get_file_contents",
            "read_file",
            "get_file",
            "read_file_contents",
            "mcp_GitHub_get_file_contents",
        ]
        
        for tool in tools:
            if any(name.lower() in tool.name.lower() for name in tool_names_to_try):
                get_file_tool = tool
                break
        
        if not get_file_tool:
            log(f"Could not find file reading tool. Available tools: {[t.name for t in tools]}", node="indexer", level="WARNING")
            return None
        
        # First, try to list the releases directory
        found_versions = []
        
        if list_dir_tool:
            try:
                log("Using directory listing tool to get releases", node="indexer")
                # Try different parameter formats for directory listing
                try:
                    dir_listing = list_dir_tool.func(
                        owner="kubevirt",
                        repo="sig-release",
                        path="releases"
                    )
                except TypeError:
                    try:
                        dir_listing = list_dir_tool.func(
                            path="kubevirt/sig-release/releases"
                        )
                    except TypeError:
                        dir_listing = list_dir_tool.func(
                            owner="kubevirt",
                            repo="sig-release",
                            path="releases",
                            branch="main"
                        )
                
                # Parse directory listing - could be JSON, string, etc.
                listing_str = str(dir_listing)
                log(f"Directory listing received (type: {type(dir_listing)}, length: {len(listing_str)})", node="indexer")
                log(f"Directory listing content (first 2000 chars): {listing_str[:2000]}", node="indexer", level="DEBUG")
                
                # Try to parse as JSON first (GitHub API often returns JSON)
                listing_data = None
                try:
                    if isinstance(dir_listing, str):
                        listing_data = json.loads(dir_listing)
                    elif isinstance(dir_listing, (list, dict)):
                        listing_data = dir_listing
                    
                    # If it's a list of file/dir objects, extract names
                    if isinstance(listing_data, list):
                        log(f"Parsed as JSON list with {len(listing_data)} items", node="indexer")
                        for item in listing_data:
                            if isinstance(item, dict):
                                # Try various field names that might contain the directory name
                                name = (item.get("name") or item.get("path") or 
                                       item.get("filename") or item.get("file_name") or "")
                                if name:
                                    # Extract version from name (e.g., "v1.8" from "v1.8" or "releases/v1.8")
                                    version_match = re.search(r'v\d+\.\d+', name)
                                    if version_match:
                                        found_versions.append(version_match.group())
                            elif isinstance(item, str):
                                version_match = re.search(r'v\d+\.\d+', item)
                                if version_match:
                                    found_versions.append(version_match.group())
                    elif isinstance(listing_data, dict):
                        # Might be a dict with a "tree" or "items" key
                        for key in ["tree", "items", "contents", "files"]:
                            if key in listing_data and isinstance(listing_data[key], list):
                                for item in listing_data[key]:
                                    if isinstance(item, dict):
                                        name = (item.get("name") or item.get("path") or 
                                               item.get("filename") or "")
                                        if name:
                                            version_match = re.search(r'v\d+\.\d+', name)
                                            if version_match:
                                                found_versions.append(version_match.group())
                except (json.JSONDecodeError, TypeError, AttributeError) as e:
                    log(f"Could not parse as JSON: {e}", node="indexer", level="DEBUG")
                
                # Extract version patterns from string (fallback for non-JSON responses)
                version_pattern = r'v\d+\.\d+'
                string_versions = re.findall(version_pattern, listing_str)
                found_versions.extend(string_versions)
                found_versions = list(set(found_versions))  # Remove duplicates
                
                log(f"Extracted {len(found_versions)} unique versions: {found_versions}", node="indexer")
                
            except Exception as e:
                log(f"Error listing releases directory: {e}", node="indexer", level="DEBUG")
        
        # Fallback: try to get directory as file (some APIs return directory contents)
        if not found_versions:
            try:
                log("Trying to get releases directory as file content", node="indexer")
                releases_dir_content = get_file_tool.func(
                    owner="kubevirt",
                    repo="sig-release",
                    path="releases"
                )
                
                content_str = str(releases_dir_content)
                log(f"Directory content (first 500 chars): {content_str[:500]}", node="indexer", level="DEBUG")
                
                # Extract version patterns
                version_pattern = r'v\d+\.\d+'
                found_versions = list(set(re.findall(version_pattern, content_str)))
                
            except Exception as e:
                log(f"Error reading releases directory as file: {e}", node="indexer", level="DEBUG")
        
        if found_versions:
            # Sort numerically (v1.11 > v1.8)
            sorted_versions = _sort_versions_numerically(found_versions)
            log(f"Found {len(sorted_versions)} release versions: {sorted_versions[:5]}...", node="indexer")

            # Try the newest versions first
            in_main_loop = True
            for version in sorted_versions:
                try:
                    schedule_path = f"releases/{version}/schedule.md"
                    log(f"Trying to fetch schedule for {version}", node="indexer")
                    
                    # Try different parameter formats
                    try:
                        schedule_content = get_file_tool.func(
                            owner="kubevirt",
                            repo="sig-release",
                            path=schedule_path
                        )
                    except TypeError:
                        try:
                            schedule_content = get_file_tool.func(
                                path=f"kubevirt/sig-release/{schedule_path}"
                            )
                        except TypeError:
                            schedule_content = get_file_tool.func(
                                owner="kubevirt",
                                repo="sig-release",
                                path=schedule_path,
                                branch="main"
                            )
                    
                    if schedule_content and len(str(schedule_content)) > 100:
                        log(f"Found release schedule for {version} ({'newest available' if in_main_loop else 'fallback'})", node="indexer")
                        return _process_schedule_content(schedule_content, version, sorted_versions if in_main_loop else None)
                except Exception as e:
                    log(f"Error fetching schedule for {version}: {e}", node="indexer", level="DEBUG")
                    continue
        else:
            log("Could not extract version numbers from releases directory", node="indexer", level="WARNING")

        # Fallback: try common recent versions if directory listing failed
        log("Falling back to trying common recent versions", node="indexer")
        fallback_versions = _sort_versions_numerically(["v1.11", "v1.10", "v1.9", "v1.8", "v1.7"])

        in_main_loop = False
        for version in fallback_versions:
            try:
                schedule_path = f"releases/{version}/schedule.md"
                schedule_content = get_file_tool.func(
                    owner="kubevirt",
                    repo="sig-release",
                    path=schedule_path
                )
                
                if schedule_content and len(str(schedule_content)) > 100:
                    log(f"Found release schedule for {version} ({'newest available' if in_main_loop else 'fallback'})", node="indexer")
                    return _process_schedule_content(schedule_content, version, sorted_versions if in_main_loop else None)
            except Exception:
                continue
                    
    except Exception as e:
        log(f"Error in index_release_schedule: {e}", node="indexer", level="WARNING")
    
    log("Could not determine current release schedule", node="indexer", level="WARNING")
    return None


def _filter_by_date(items: List[Dict[str, Any]], days: int = 365) -> List[Dict[str, Any]]:
    """Filter items to only include those from the last N days.
    
    IMPORTANT: Always includes open items regardless of date, as they may be active VEPs.
    
    Args:
        items: List of items with 'created_at' or 'updated_at' fields, and optionally 'state'
        days: Number of days to look back (default 365)
    
    Returns:
        Filtered list of items (all open items + closed items from last N days)
    """
    cutoff_date = datetime.now() - timedelta(days=days)
    filtered = []
    
    for item in items:
        # Always include open items (they may be active VEPs without files yet)
        state = item.get("state", "").lower()
        if state == "open":
            filtered.append(item)
            continue
        
        # For closed items, apply date filter
        # Try created_at first, then updated_at
        date_str = item.get("created_at") or item.get("updated_at")
        if not date_str:
            # If no date, include it (better to include than exclude)
            filtered.append(item)
            continue
        
        # Parse date (could be ISO string, timestamp, etc.)
        try:
            if isinstance(date_str, str):
                # Try ISO format
                item_date = datetime.fromisoformat(date_str.replace('Z', '+00:00'))
            elif isinstance(date_str, (int, float)):
                # Timestamp
                item_date = datetime.fromtimestamp(date_str)
            else:
                # Unknown format, include it
                filtered.append(item)
                continue
            
            # Compare dates (handle timezone-aware dates)
            if item_date.replace(tzinfo=None) >= cutoff_date:
                filtered.append(item)
        except Exception:
            # If parsing fails, include it
            filtered.append(item)
    
    return filtered


def index_enhancements_issues(days_back: Optional[int] = 365) -> List[Dict[str, Any]]:
    """Index issues in kubevirt/enhancements repository.
    
    Args:
        days_back: Only include issues from last N days (None = all issues)
    
    Returns:
        List of issue summaries (number, title, labels, state) for context
    """
    log(f"Indexing issues from kubevirt/enhancements (days_back={days_back})", node="indexer")
    
    try:
        tools = get_mcp_tools_by_name("github")
        log(f"Available GitHub tools: {[t.name for t in tools]}", node="indexer", level="DEBUG")
        
        # Prefer search_issues over list_issues for comprehensive results
        # search_issues can get all issues matching criteria, while list_issues may be paginated
        search_issues_tool = None
        list_issues_tool = None
        
        # Look for search_issues first (better for getting all issues)
        for tool in tools:
            if "search_issues" in tool.name.lower():
                search_issues_tool = tool
                log(f"Found search_issues tool: {tool.name}", node="indexer", level="DEBUG")
                break
        
        # Fallback to list_issues
        if not search_issues_tool:
            tool_names_to_try = [
                "mcp_GitHub_list_issues",
                "list_issues",
                "get_issues",
            ]
            for tool in tools:
                if tool.name in tool_names_to_try:
                    list_issues_tool = tool
                    log(f"Found list_issues tool: {tool.name}", node="indexer", level="DEBUG")
                    break
            if not list_issues_tool:
                for tool in tools:
                    tool_name_lower = tool.name.lower()
                    if any(name.lower() in tool_name_lower for name in tool_names_to_try):
                        list_issues_tool = tool
                        log(f"Found list_issues tool (partial match): {tool.name}", node="indexer", level="DEBUG")
                        break
        
        if not search_issues_tool and not list_issues_tool:
            log(f"Could not find issues listing tool. Available tools: {[t.name for t in tools]}", node="indexer", level="WARNING")
            return []
        
        try:
            # Use search_issues if available (more comprehensive)
            if search_issues_tool:
                log("Using search_issues to get all issues from kubevirt/enhancements", node="indexer", level="DEBUG")
                # Build date filter for search query (if days_back specified)
                date_filter = ""
                if days_back is not None:
                    from datetime import datetime, timedelta
                    cutoff_date = datetime.now() - timedelta(days=days_back)
                    date_str = cutoff_date.strftime("%Y-%m-%d")
                    date_filter = f" updated:>={date_str}"
                
                def fetch_all_pages(query: str, state: str) -> list:
                    """Fetch all pages of search results for a query."""
                    all_items = []
                    page = 1
                    per_page = 100  # GitHub Search API max is 100 per page
                    total_count = None
                    
                    while True:
                        # Try to pass pagination parameters (some MCP tools may not support this)
                        try:
                            result = _call_with_retry(
                                search_issues_tool.func,
                                query=query,
                                per_page=per_page,
                                page=page,
                            )
                        except TypeError:
                            # Tool doesn't support pagination params, try without
                            result = _call_with_retry(
                                search_issues_tool.func,
                                query=query,
                            )
                            # If we already got results, break (no pagination support)
                            if all_items:
                                break
                        
                        # Parse result
                        items = []
                        result_dict = None
                        
                        if isinstance(result, str):
                            try:
                                result_dict = json.loads(result)
                            except:
                                pass
                        elif isinstance(result, dict):
                            result_dict = result
                        elif isinstance(result, list):
                            items = result
                        
                        if result_dict:
                            if "items" in result_dict:
                                items = result_dict["items"]
                            if "total_count" in result_dict:
                                total_count = result_dict["total_count"]
                            if "incomplete_results" in result_dict and result_dict["incomplete_results"]:
                                log(f"Warning: Search results for {state} issues may be incomplete", node="indexer", level="WARNING")
                        
                        if not items:
                            break
                        
                        all_items.extend(items)
                        
                        # Check if we've got all results
                        if total_count is not None and len(all_items) >= total_count:
                            break
                        
                        # If we got fewer than per_page, we're done
                        if len(items) < per_page:
                            break
                        
                        # If we can't paginate (no per_page support), break after first page
                        if page == 1 and total_count is None:
                            # Check if we got a full page - if not, we're done
                            if len(items) < 30:  # Default page size
                                break
                            # Otherwise, log that we might be missing results
                            log(f"Warning: Search API may have more results but pagination not supported. Got {len(items)} items for {state} issues.", node="indexer", level="WARNING")
                            break
                        
                        page += 1
                        # Safety limit: don't fetch more than 10 pages (1000 items max)
                        if page > 10:
                            log(f"Warning: Reached pagination limit (10 pages) for {state} issues. There may be more results.", node="indexer", level="WARNING")
                            break
                    
                    if total_count is not None and len(all_items) < total_count:
                        log(f"Warning: Expected {total_count} {state} issues but only retrieved {len(all_items)}", node="indexer", level="WARNING")
                    
                    return all_items
                
                # Search for VEP-related issues: use "VEP in:title" to match user's query
                # This is more specific and matches what the user sees on GitHub
                open_query = f"repo:kubevirt/enhancements is:issue is:open VEP in:title{date_filter}"
                closed_query = f"repo:kubevirt/enhancements is:issue is:closed VEP in:title{date_filter}"
                
                # Also search for issues without "VEP" in title but with VEP-related labels
                # This catches VEPs that might not have "VEP" in the title
                open_query_all = f"repo:kubevirt/enhancements is:issue is:open{date_filter}"
                closed_query_all = f"repo:kubevirt/enhancements is:issue is:closed{date_filter}"
                
                log(f"Fetching open issues with query: {open_query}", node="indexer", level="DEBUG")
                open_issues_vep = fetch_all_pages(open_query, "open (VEP in title)")
                
                log(f"Fetching all open issues with query: {open_query_all}", node="indexer", level="DEBUG")
                open_issues_all = fetch_all_pages(open_query_all, "open (all)")
                
                # Merge results, deduplicating by issue number
                open_issues_dict = {}
                for issue in open_issues_vep + open_issues_all:
                    if isinstance(issue, dict):
                        issue_num = issue.get("number")
                        if issue_num:
                            open_issues_dict[issue_num] = issue
                open_issues = list(open_issues_dict.values())
                
                log(f"Fetching closed issues with query: {closed_query}", node="indexer", level="DEBUG")
                closed_issues_vep = fetch_all_pages(closed_query, "closed (VEP in title)")
                
                log(f"Fetching all closed issues with query: {closed_query_all}", node="indexer", level="DEBUG")
                closed_issues_all = fetch_all_pages(closed_query_all, "closed (all)")
                
                # Merge results, deduplicating by issue number
                closed_issues_dict = {}
                for issue in closed_issues_vep + closed_issues_all:
                    if isinstance(issue, dict):
                        issue_num = issue.get("number")
                        if issue_num:
                            closed_issues_dict[issue_num] = issue
                closed_issues = list(closed_issues_dict.values())
                
                # Combine lists
                issues_result = open_issues + closed_issues
                log(f"Retrieved {len(open_issues)} open issues and {len(closed_issues)} closed issues via search_issues (total: {len(issues_result)})", node="indexer")
            else:
                # Fallback to list_issues
                log("Using list_issues to get issues from kubevirt/enhancements", node="indexer", level="DEBUG")
                try:
                    issues_result = _call_with_retry(
                        list_issues_tool.func,
                        owner="kubevirt",
                        repo="enhancements",
                        state="all"
                    )
                except TypeError:
                    issues_result = _call_with_retry(
                        list_issues_tool.func,
                        repo="kubevirt/enhancements",
                        state="all"
                    )
            
            # Parse result
            if isinstance(issues_result, str):
                # Check if it's a rate limit error
                if "rate limit" in issues_result.lower() or "rate_limit" in issues_result.lower():
                    log(f"GitHub API rate limit exceeded. Error: {issues_result[:300]}", node="indexer", level="ERROR")
                    log("Rate limit typically resets on the hour. Please wait and try again, or ensure GITHUB_TOKEN is being used correctly.", node="indexer", level="WARNING")
                    return []
                # Check if it's an error message
                if len(issues_result) < 500 or issues_result.lower().startswith(("error", "failed", "cannot", "unable")):
                    log(f"Received error or suspiciously short response (length: {len(issues_result)}): {issues_result[:500]}", node="indexer", level="WARNING")
                    log(f"Available tools: {[t.name for t in tools]}", node="indexer", level="DEBUG")
                    return []
                log(f"Retrieved issues data as string (length: {len(issues_result)})", node="indexer")
                # Try to parse as JSON
                try:
                    parsed_issues = json.loads(issues_result)
                    if isinstance(parsed_issues, list):
                        # Process as list
                        issues = []
                        for issue in parsed_issues:
                            if isinstance(issue, dict):
                                labels = [l.get("name") if isinstance(l, dict) else l for l in issue.get("labels", [])]
                                title = issue.get("title", "")
                                body = issue.get("body", "") or ""
                                
                                # In kubevirt/enhancements repo, assume ALL issues are VEP-related by default
                                # Only exclude obvious non-VEP issues (bugs, typos, CI, etc.)
                                is_vep_related = True  # Default to True - be inclusive
                                
                                # Exclude obvious non-VEP issues
                                exclude_patterns = [
                                    "bug", "bugfix", "typo", "documentation fix", "spelling",
                                    "ci", "test", "chore", "maintenance", "infrastructure",
                                    "dependabot", "renovate"
                                ]
                                title_lower = title.lower()
                                body_preview = (body[:500] or "").lower()
                                
                                # If it's clearly a bug/typo/CI issue, exclude it
                                if any(pattern in title_lower or pattern in body_preview for pattern in exclude_patterns):
                                    # But still include if it has VEP-related labels or mentions VEP numbers
                                    has_vep_label = any("vep" in str(l).lower() or "enhancement" in str(l).lower() for l in labels)
                                    has_vep_number = re.search(r'vep-?\s*\d+|VEP\s*#?\s*\d+', title + " " + body_preview, re.IGNORECASE)
                                    if not (has_vep_label or has_vep_number):
                                        is_vep_related = False
                                
                                # Positive indicators (strengthen confidence, but don't require them)
                                # Check labels - expanded patterns for VEP detection
                                vep_label_patterns = ["kind/vep", "vep", "area/enhancement", "enhancement", "sig/", "kind/enhancement", "area/feature"]
                                if any(pattern.lower() in str(label).lower() for label in labels for pattern in vep_label_patterns):
                                    is_vep_related = True  # Definitely VEP-related
                                
                                # Check title/body for VEP references
                                vep_patterns = [
                                    r'vep-?\s*\d+',  # vep-123, vep 123, VEP-123
                                    r'VEP\s*#?\s*\d+',  # VEP #123, VEP 123
                                    r'enhancement\s*#?\s*\d+',  # Enhancement #123
                                ]
                                for pattern in vep_patterns:
                                    if re.search(pattern, title, re.IGNORECASE) or re.search(pattern, body[:1000], re.IGNORECASE):
                                        is_vep_related = True
                                        break
                                
                                # SIG labels - issues with SIG labels in enhancements repo are likely VEPs
                                sig_labels = [l for l in labels if "sig/" in str(l).lower()]
                                if sig_labels:
                                    is_vep_related = True
                                
                                # Release/milestone labels - VEPs often have these
                                release_labels = [l for l in labels if "release/" in str(l).lower() or "target/" in str(l).lower() or "milestone" in str(l).lower()]
                                if release_labels:
                                    is_vep_related = True
                                
                                # Extract assignee and author information
                                assignee = None
                                if issue.get("assignee"):
                                    if isinstance(issue.get("assignee"), dict):
                                        assignee = issue.get("assignee", {}).get("login")
                                    else:
                                        assignee = issue.get("assignee")
                                
                                # Extract author/creator (user who opened the issue)
                                author = None
                                if issue.get("user"):
                                    if isinstance(issue.get("user"), dict):
                                        author = issue.get("user", {}).get("login")
                                    else:
                                        author = issue.get("user")
                                
                                issues.append({
                                    "number": issue.get("number"),
                                    "title": issue.get("title"),
                                    "labels": labels,
                                    "state": issue.get("state"),
                                    "url": issue.get("html_url") or issue.get("url"),
                                    "created_at": issue.get("created_at"),
                                    "updated_at": issue.get("updated_at"),
                                    "is_vep_related": is_vep_related,
                                    "body": body if body else "",  # Store full body for PR URL extraction
                                    "body_preview": body[:500] if body else "",  # Keep preview for debugging
                                    "assignee": assignee,  # Person assigned to the issue (primary owner)
                                    "author": author,  # Person who created/opened the issue (fallback owner)
                                })
                        
                        # Filter by date if requested
                        if days_back is not None:
                            original_count = len(issues)
                            issues = _filter_by_date(issues, days_back)
                            log(f"Filtered issues: {original_count} -> {len(issues)} (last {days_back} days)", node="indexer")
                        
                        # Count VEP-related issues
                        vep_related_count = sum(1 for issue in issues if issue.get("is_vep_related", False))
                        log(f"Parsed {len(issues)} issues from JSON string ({vep_related_count} VEP-related)", node="indexer")
                        return issues
                except json.JSONDecodeError:
                    log("Could not parse issues string as JSON, returning raw data", node="indexer", level="DEBUG")
                return [{"raw_data": issues_result[:15000]}]
            elif isinstance(issues_result, list):
                issues = []
                for issue in issues_result:
                    if isinstance(issue, dict):
                        labels = [l.get("name") if isinstance(l, dict) else l for l in issue.get("labels", [])]
                        title = issue.get("title", "")
                        body = issue.get("body", "") or ""
                        
                        # Check if this is a VEP-related issue
                        # In kubevirt/enhancements repo, most issues are VEP-related by default
                        # Be more inclusive - only exclude obvious non-VEP issues
                        is_vep_related = True  # Default to True for enhancements repo
                        
                        # Exclude obvious non-VEP issues
                        exclude_patterns = [
                            "bug", "bugfix", "typo", "documentation fix", "spelling",
                            "ci", "test", "chore", "maintenance", "infrastructure"
                        ]
                        title_lower = title.lower()
                        body_preview = (body[:500] or "").lower()
                        
                        # If it's clearly a bug/typo/CI issue, exclude it
                        if any(pattern in title_lower or pattern in body_preview for pattern in exclude_patterns):
                            # But still include if it has VEP-related labels or mentions VEP numbers
                            has_vep_label = any("vep" in str(l).lower() or "enhancement" in str(l).lower() for l in labels)
                            has_vep_number = re.search(r'vep-?\s*\d+|VEP\s*#?\s*\d+', title + " " + body_preview, re.IGNORECASE)
                            if not (has_vep_label or has_vep_number):
                                is_vep_related = False
                        
                        # Additional positive indicators (strengthen confidence)
                        # Check labels - expanded patterns for VEP detection
                        vep_label_patterns = ["kind/vep", "vep", "area/enhancement", "enhancement", "sig/", "kind/enhancement", "area/feature"]
                        if any(pattern.lower() in str(label).lower() for label in labels for pattern in vep_label_patterns):
                            is_vep_related = True  # Definitely VEP-related
                        
                        # Check title/body for VEP references (vep-123, VEP-123, vep123, etc.)
                        vep_patterns = [
                            r'vep-?\s*\d+',  # vep-123, vep 123, VEP-123
                            r'VEP\s*#?\s*\d+',  # VEP #123, VEP 123
                            r'enhancement\s*#?\s*\d+',  # Enhancement #123
                        ]
                        for pattern in vep_patterns:
                            if re.search(pattern, title, re.IGNORECASE) or re.search(pattern, body[:1000], re.IGNORECASE):
                                is_vep_related = True
                                break
                        
                        # Check for SIG labels - issues with SIG labels in enhancements repo are likely VEPs
                        sig_labels = [l for l in labels if "sig/" in str(l).lower()]
                        if sig_labels:
                            is_vep_related = True
                        
                        # Check for release/milestone labels - VEPs often have these
                        release_labels = [l for l in labels if "release/" in str(l).lower() or "target/" in str(l).lower() or "milestone" in str(l).lower()]
                        if release_labels:
                            is_vep_related = True
                        
                        # Extract assignee and author information
                        assignee = None
                        if issue.get("assignee"):
                            if isinstance(issue.get("assignee"), dict):
                                assignee = issue.get("assignee", {}).get("login")
                            else:
                                assignee = issue.get("assignee")
                        
                        # Extract author/creator (user who opened the issue)
                        author = None
                        if issue.get("user"):
                            if isinstance(issue.get("user"), dict):
                                author = issue.get("user", {}).get("login")
                            else:
                                author = issue.get("user")
                        
                        # Include all issues for now, but mark VEP-related ones
                        issues.append({
                            "number": issue.get("number"),
                            "title": issue.get("title"),
                            "labels": labels,
                            "state": issue.get("state"),
                            "url": issue.get("html_url") or issue.get("url"),
                            "created_at": issue.get("created_at"),
                            "updated_at": issue.get("updated_at"),
                            "is_vep_related": is_vep_related,
                            "body": body if body else "",  # Store full body for PR URL extraction
                            "body_preview": body[:500] if body else "",  # Keep preview for debugging
                            "assignee": assignee,  # Person assigned to the issue (primary owner)
                            "author": author,  # Person who created/opened the issue (fallback owner)
                        })
                
                # Filter by date if requested
                if days_back is not None:
                    original_count = len(issues)
                    issues = _filter_by_date(issues, days_back)
                    log(f"Filtered issues: {original_count} -> {len(issues)} (last {days_back} days)", node="indexer")
                
                # Count VEP-related issues
                vep_related_count = sum(1 for issue in issues if issue.get("is_vep_related", False))
                log(f"Indexed {len(issues)} issues ({vep_related_count} VEP-related)", node="indexer")
                return issues
            else:
                return [{"raw_data": str(issues_result)[:15000]}]
                
        except Exception as e:
            log(f"Error listing issues: {e}", node="indexer", level="WARNING")
            return []
            
    except Exception as e:
        log(f"Error in index_enhancements_issues: {e}", node="indexer", level="WARNING")
        return []
    
    return []


def _extract_vep_issue_number(title: str, body: str) -> Optional[int]:
    """Extract VEP tracking issue number from PR title and body.

    Searches for patterns like:
    - https://github.com/kubevirt/enhancements/issues/80
    - Tracking issue: #80
    - VEP Tracker: #21
    - Tracker: #80
    - fixes #N, closes #N
    - VEP number in title (e.g., "VEP 165: ..." → issue 165)

    Returns:
        Issue number or None
    """
    text_to_search = f"{title} {body}"
    if not text_to_search.strip():
        return None

    # Pattern 1: Full issue URL (most reliable)
    issue_url_match = re.search(r'github\.com/kubevirt/enhancements/issues/(\d+)', text_to_search)
    if issue_url_match:
        return int(issue_url_match.group(1))

    # Pattern 2: Specific tracking issue keywords (ordered by specificity)
    specific_patterns = [
        r'tracking\s+issue[\s:]+#?(\d+)',
        r'vep\s+tracker[\s:]+#?(\d+)',
        r'tracker[\s:]+#?(\d+)',
        r'vep\s+issue[\s:]+#?(\d+)',
    ]
    for pattern in specific_patterns:
        match = re.search(pattern, text_to_search, re.IGNORECASE)
        if match:
            return int(match.group(1))

    # Pattern 3: Generic keywords
    generic_match = re.search(r'(?:fixes|closes)[\s:]+#?(\d+)', text_to_search, re.IGNORECASE)
    if generic_match:
        return int(generic_match.group(1))

    # Pattern 4: VEP number in title as fallback
    # In the enhancements repo, VEP numbers typically match issue numbers
    # e.g., "VEP 165: ContainerPath Volumes" → issue #165
    # e.g., "VEP-183: NetworkDevicesWithDRA" → issue #183
    # e.g., "VEP #156: Expose Memory Overhead" → issue #156
    # e.g., "VEP0176: kubevirt-redfish" → issue #176
    if title:
        vep_title_match = re.search(r'VEP[- #]*0*(\d+)', title, re.IGNORECASE)
        if vep_title_match:
            return int(vep_title_match.group(1))

    return None


def index_enhancements_prs(days_back: Optional[int] = 365) -> List[Dict[str, Any]]:
    """Index PRs in kubevirt/enhancements repository.

    Indexes proposal PRs that create or modify VEP documents.
    Extracts VEP issue references from PR bodies to link PRs to tracking issues.

    Args:
        days_back: Only include PRs from last N days (None = all PRs)

    Returns:
        List of PR summaries with VEP issue references
    """
    log(f"Indexing PRs from kubevirt/enhancements (days_back={days_back})", node="indexer")

    try:
        tools = get_mcp_tools_by_name("github")

        # Find list_pull_requests tool
        list_prs_tool = None
        tool_names_to_try = [
            "mcp_GitHub_list_pull_requests",
            "list_pull_requests",
            "list_pulls",
            "search_pull_requests",
        ]

        for tool in tools:
            if tool.name in tool_names_to_try:
                list_prs_tool = tool
                break

        if not list_prs_tool:
            for tool in tools:
                tool_name_lower = tool.name.lower()
                if any(name.lower() in tool_name_lower and ("pull" in tool_name_lower or "pr" in tool_name_lower) for name in tool_names_to_try):
                    list_prs_tool = tool
                    break

        if not list_prs_tool:
            log("Could not find PR listing tool for enhancements repo", node="indexer", level="WARNING")
            return []

        try:
            # Fetch all pages of PRs
            import json
            all_pr_items = []
            page = 1
            max_pages = 10  # Safety limit

            while page <= max_pages:
                log(f"Fetching enhancements PRs page {page}", node="indexer", level="DEBUG")
                try:
                    prs_result = _call_with_retry(
                        list_prs_tool.func,
                        owner="kubevirt",
                        repo="enhancements",
                        state="all",
                        page=page,
                        per_page=100,
                    )
                except TypeError:
                    # Tool may not support page/per_page, try without
                    if page > 1:
                        break
                    try:
                        prs_result = _call_with_retry(
                            list_prs_tool.func,
                            owner="kubevirt",
                            repo="enhancements",
                            state="all",
                        )
                    except TypeError:
                        prs_result = _call_with_retry(
                            list_prs_tool.func,
                            repo="kubevirt/enhancements",
                            state="all",
                        )

                # Parse result
                if isinstance(prs_result, str):
                    try:
                        prs_result = json.loads(prs_result)
                    except json.JSONDecodeError:
                        log("Could not parse PRs result", node="indexer", level="WARNING")
                        break

                if isinstance(prs_result, list):
                    if not prs_result:
                        break  # No more results
                    all_pr_items.extend(prs_result)
                    log(f"  Got {len(prs_result)} PRs on page {page} (total so far: {len(all_pr_items)})", node="indexer", level="DEBUG")
                    if len(prs_result) < 30:
                        break  # Last page
                    page += 1
                else:
                    break

            log(f"Fetched {len(all_pr_items)} total PRs from kubevirt/enhancements across {page} page(s)", node="indexer")

            # Extract PR data
            prs = []
            if all_pr_items:
                prs_result = all_pr_items
            if isinstance(prs_result, list):
                for pr in prs_result:
                    if not isinstance(pr, dict):
                        continue

                    # Get basic fields
                    pr_number = pr.get("number")
                    title = pr.get("title", "")
                    state = pr.get("state", "")
                    body = pr.get("body", "")
                    url = pr.get("html_url") or pr.get("url", "")
                    created_at = pr.get("created_at")
                    updated_at = pr.get("updated_at")

                    # Extract VEP issue number from body and title
                    vep_issue_num = _extract_vep_issue_number(title, body)
                    log(f"  Enhancements PR #{pr_number}: title='{title[:60]}', body_len={len(body)}, vep_issue={vep_issue_num}", node="indexer", level="DEBUG")

                    # Get review count (approximation from review_comments count)
                    review_count = pr.get("review_comments", 0)
                    if pr.get("comments"):
                        review_count = max(review_count, pr.get("comments", 0) // 2)

                    prs.append({
                        "number": pr_number,
                        "title": title,
                        "state": state,
                        "body": body,  # Full body for tracking issue reference extraction
                        "url": url,
                        "html_url": pr.get("html_url", url),
                        "created_at": created_at,
                        "updated_at": updated_at,
                        "vep_issue_number": vep_issue_num,
                        "review_count": review_count,
                    })

                # Filter by date if requested
                if days_back is not None:
                    original_count = len(prs)
                    prs = _filter_by_date(prs, days_back)
                    log(f"Filtered enhancements PRs: {original_count} -> {len(prs)} (last {days_back} days)", node="indexer")

                # Log VEP-linked PRs
                vep_linked = [p for p in prs if p.get("vep_issue_number")]
                no_vep = [p for p in prs if not p.get("vep_issue_number")]
                log(f"Indexed {len(prs)} PRs from kubevirt/enhancements ({len(vep_linked)} linked to VEP issues)", node="indexer")
                for p in vep_linked:
                    log(f"  Enhancements PR #{p['number']} -> VEP issue #{p['vep_issue_number']}", node="indexer", level="DEBUG")
                if no_vep:
                    log(f"  Unlinked enhancements PRs: {[p['number'] for p in no_vep]}", node="indexer", level="DEBUG")
                return prs

        except Exception as e:
            log(f"Error listing enhancements PRs: {e}", node="indexer", level="WARNING")
            return []

    except Exception as e:
        log(f"Error in index_enhancements_prs: {e}", node="indexer", level="WARNING")
        return []


def _search_kubevirt_prs_referencing_veps() -> List[Dict[str, Any]]:
    """Search kubevirt/kubevirt PRs that reference enhancements tracking issues.

    Uses GitHub search API which indexes PR bodies, making it reliable
    for finding VEP references even when list_pull_requests doesn't
    return full bodies.

    Returns:
        List of PR dicts with vep_issue_number set
    """
    log("Searching kubevirt PRs referencing VEP tracking issues", node="indexer")

    try:
        tools = get_mcp_tools_by_name("github")
        search_tool = None
        for tool in tools:
            if "search_issues" in tool.name.lower():
                search_tool = tool
                break

        if not search_tool:
            log("search_issues tool not found, skipping VEP PR search", node="indexer", level="WARNING")
            return []

        all_prs = []

        # Search for PRs that reference enhancements issues
        search_queries = [
            'repo:kubevirt/kubevirt is:pr "kubevirt/enhancements/issues"',
            'repo:kubevirt/kubevirt is:pr "Tracking issue:"',
            'repo:kubevirt/kubevirt is:pr "VEP Tracker:"',
        ]

        seen_numbers = set()

        for search_query in search_queries:
            page = 1
            max_pages = 25  # Need enough pages to cover all VEP-referencing PRs (~655 total, ~30/page)

            while page <= max_pages:
                try:
                    result = _call_with_retry(
                        search_tool.func,
                        query=search_query,
                        per_page=100,
                        page=page,
                    )
                except TypeError:
                    try:
                        result = _call_with_retry(search_tool.func, query=search_query)
                    except Exception:
                        break
                    if page > 1:
                        break

                # Parse result
                items = []
                if isinstance(result, str):
                    try:
                        result = json.loads(result)
                    except json.JSONDecodeError:
                        break

                if isinstance(result, dict):
                    items = result.get("items", [])
                elif isinstance(result, list):
                    items = result

                if not items:
                    break

                for item in items:
                    if not isinstance(item, dict):
                        continue
                    pr_num = item.get("number")
                    if not pr_num or pr_num in seen_numbers:
                        continue
                    seen_numbers.add(pr_num)

                    body = item.get("body") or ""
                    title = item.get("title") or ""
                    vep_issue_num = _extract_vep_issue_number(title, body)

                    if vep_issue_num:
                        is_merged = "pull_request" in item and item.get("pull_request", {}).get("merged_at") is not None
                        all_prs.append({
                            "number": pr_num,
                            "title": title,
                            "state": "merged" if is_merged else item.get("state", ""),
                            "merged": is_merged,
                            "url": item.get("html_url") or item.get("url", ""),
                            "html_url": item.get("html_url", ""),
                            "created_at": item.get("created_at"),
                            "updated_at": item.get("updated_at"),
                            "body_preview": body[:500] if body else "",
                            "vep_issue_number": vep_issue_num,
                        })

                if len(items) < 30:
                    break
                page += 1

        log(f"Found {len(all_prs)} kubevirt PRs referencing VEP issues via search", node="indexer")
        for pr in sorted(all_prs, key=lambda x: x["number"], reverse=True)[:30]:
            log(f"  Search: kubevirt PR #{pr['number']} -> VEP issue #{pr['vep_issue_number']} (merged={pr['merged']})", node="indexer", level="DEBUG")

        return all_prs

    except Exception as e:
        log(f"Error searching kubevirt PRs: {e}", node="indexer", level="WARNING")
        return []


def index_kubevirt_prs(days_back: Optional[int] = 365, fetch_reviews: bool = True) -> List[Dict[str, Any]]:
    """Index PRs in kubevirt/kubevirt repository.

    Args:
        days_back: Only include PRs from last N days (None = all PRs)
        fetch_reviews: If True, fetch review counts for open PRs (default: True)

    Returns:
        List of PR summaries (number, title, state, labels, review_count) for context
    """
    log(f"Indexing PRs from kubevirt/kubevirt (days_back={days_back}, fetch_reviews={fetch_reviews})", node="indexer")

    try:
        tools = get_mcp_tools_by_name("github")

        # Find list_pull_requests tool - try exact matches first
        list_prs_tool = None
        tool_names_to_try = [
            "mcp_GitHub_list_pull_requests",  # Try full name first
            "list_pull_requests",
            "list_pulls",
            "search_pull_requests",
        ]

        # First try exact match
        for tool in tools:
            if tool.name in tool_names_to_try:
                list_prs_tool = tool
                log(f"Found PR tool (exact match): {tool.name}", node="indexer", level="DEBUG")
                break

        # If no exact match, try partial
        if not list_prs_tool:
            for tool in tools:
                tool_name_lower = tool.name.lower()
                if any(name.lower() in tool_name_lower and ("pull" in tool_name_lower or "pr" in tool_name_lower) for name in tool_names_to_try):
                    list_prs_tool = tool
                    log(f"Found PR tool (partial match): {tool.name}", node="indexer", level="DEBUG")
                    break

        if not list_prs_tool:
            log(f"Could not find PR listing tool. Available tools: {[t.name for t in tools]}", node="indexer", level="WARNING")
            return []

        # Find get_pull_request_reviews tool for review counts
        get_reviews_tool = None
        if fetch_reviews:
            reviews_tool_names = [
                "mcp_GitHub_get_pull_request_reviews",
                "get_pull_request_reviews",
            ]
            for tool in tools:
                if tool.name in reviews_tool_names or "review" in tool.name.lower():
                    get_reviews_tool = tool
                    log(f"Found reviews tool: {tool.name}", node="indexer", level="DEBUG")
                    break
            if not get_reviews_tool:
                log("Could not find PR reviews tool, review counts will be None", node="indexer", level="DEBUG")

        try:
            # Fetch all pages of PRs
            all_pr_items = []
            page = 1
            max_pages = 40  # kubevirt/kubevirt has ~13k PRs, ~30/page from MCP → ~1200 PRs

            while page <= max_pages:
                log(f"Fetching kubevirt PRs page {page}", node="indexer", level="DEBUG")
                try:
                    prs_result = _call_with_retry(
                        list_prs_tool.func,
                        owner="kubevirt",
                        repo="kubevirt",
                        state="all",
                        page=page,
                        per_page=100,
                    )
                except TypeError:
                    # Tool may not support page/per_page, try without
                    if page > 1:
                        break
                    try:
                        prs_result = _call_with_retry(
                            list_prs_tool.func,
                            owner="kubevirt",
                            repo="kubevirt",
                            state="all",
                        )
                    except TypeError:
                        prs_result = _call_with_retry(
                            list_prs_tool.func,
                            repo="kubevirt/kubevirt",
                            state="all",
                        )

                # Parse JSON string if needed
                if isinstance(prs_result, str):
                    try:
                        prs_result = json.loads(prs_result)
                    except json.JSONDecodeError:
                        log(f"Could not parse kubevirt PRs page {page}", node="indexer", level="WARNING")
                        break

                if isinstance(prs_result, list):
                    if not prs_result:
                        break  # No more results
                    all_pr_items.extend(prs_result)
                    log(f"  Got {len(prs_result)} PRs on page {page} (total so far: {len(all_pr_items)})", node="indexer", level="DEBUG")
                    if len(prs_result) < 30:
                        break  # Last page
                    page += 1
                else:
                    break

            log(f"Fetched {len(all_pr_items)} total PRs from kubevirt/kubevirt across {page} page(s)", node="indexer")
            prs_result = all_pr_items

            # prs_result is now a list from pagination above
            if isinstance(prs_result, list):
                prs = []
                for pr in prs_result:
                    if isinstance(pr, dict):
                        body = pr.get("body") or ""
                        title = pr.get("title") or ""

                        # Extract VEP issue number from PR body
                        vep_issue_num = _extract_vep_issue_number(title, body)

                        # Detect merged status: check both "merged" boolean and "merged_at" datetime
                        # GitHub list PRs endpoint returns merged_at but not merged boolean
                        is_merged = pr.get("merged", False) or (pr.get("merged_at") is not None)

                        pr_data = {
                            "number": pr.get("number"),
                            "title": title,
                            "labels": [l.get("name") if isinstance(l, dict) else l for l in pr.get("labels", [])],
                            "state": "merged" if is_merged else pr.get("state"),
                            "merged": is_merged,
                            "url": pr.get("html_url") or pr.get("url"),
                            "created_at": pr.get("created_at"),
                            "updated_at": pr.get("updated_at"),
                            "body_preview": body[:500] if body else "",  # Truncated for VEP pattern matching
                            "vep_issue_number": vep_issue_num,  # Extracted VEP issue number
                            "review_count": None,  # Will be populated below if fetch_reviews=True
                        }
                        prs.append(pr_data)

                # Filter by date if requested
                if days_back is not None:
                    original_count = len(prs)
                    prs = _filter_by_date(prs, days_back)
                    log(f"Filtered PRs: {original_count} -> {len(prs)} (last {days_back} days)", node="indexer")

                # Fetch review counts for open PRs only (to limit API calls)
                if fetch_reviews and get_reviews_tool:
                    open_prs = [p for p in prs if p.get("state") == "open"]
                    log(f"Fetching review counts for {len(open_prs)} open PRs", node="indexer")
                    for pr_data in open_prs:
                        pr_number = pr_data.get("number")
                        if pr_number:
                            try:
                                time.sleep(API_DELAY)
                                reviews_result = _call_with_retry(
                                    get_reviews_tool.func,
                                    owner="kubevirt",
                                    repo="kubevirt",
                                    pull_number=pr_number
                                )
                                if isinstance(reviews_result, list):
                                    pr_data["review_count"] = len(reviews_result)
                                elif reviews_result:
                                    # Try to parse if string
                                    pr_data["review_count"] = 0
                            except Exception as e:
                                log(f"Error fetching reviews for PR #{pr_number}: {e}", node="indexer", level="DEBUG")

                # Log VEP-linked PRs
                vep_linked = [p for p in prs if p.get("vep_issue_number")]
                log(f"Indexed {len(prs)} PRs from kubevirt/kubevirt ({len(vep_linked)} linked to VEP issues)", node="indexer")
                if vep_linked:
                    for p in vep_linked[:20]:  # Log first 20
                        log(f"  kubevirt PR #{p['number']} -> VEP issue #{p['vep_issue_number']}", node="indexer", level="DEBUG")
                return prs
            else:
                return [{"raw_data": str(prs_result)[:15000]}]

        except Exception as e:
            log(f"Error listing PRs: {e}", node="indexer", level="WARNING")
            return []

    except Exception as e:
        log(f"Error in index_kubevirt_prs: {e}", node="indexer", level="WARNING")
        return []

    return []


def index_enhancements_readme() -> Optional[Dict[str, Any]]:
    """Index the README.md from kubevirt/enhancements repository.
    
    This contains crucial VEP process documentation, labels, structure, and requirements.
    
    Returns:
        Dict with README content, or None if not found
    """
    log("Indexing README.md from kubevirt/enhancements", node="indexer")
    
    try:
        tools = get_mcp_tools_by_name("github")
        
        # Find file reading tool - try exact matches first
        get_file_tool = None
        tool_names_to_try = [
            "mcp_GitHub_get_file_contents",  # Try full name first
            "get_file_contents",
            "read_file",
            "get_file",
            "read_file_contents",
        ]
        
        # First try exact match
        for tool in tools:
            if tool.name in tool_names_to_try:
                get_file_tool = tool
                log(f"Found file tool (exact match): {tool.name}", node="indexer", level="DEBUG")
                break
        
        # If no exact match, try partial
        if not get_file_tool:
            for tool in tools:
                if any(name.lower() in tool.name.lower() for name in tool_names_to_try):
                    get_file_tool = tool
                    log(f"Found file tool (partial match): {tool.name}", node="indexer", level="DEBUG")
                    break
        
        if not get_file_tool:
            log(f"Could not find file reading tool. Available tools: {[t.name for t in tools]}", node="indexer", level="WARNING")
            return None
        
        try:
            # Try different parameter formats
            try:
                readme_content = get_file_tool.func(
                    owner="kubevirt",
                    repo="enhancements",
                    path="README.md"
                )
            except TypeError:
                try:
                    readme_content = get_file_tool.func(
                        path="kubevirt/enhancements/README.md"
                    )
                except TypeError:
                    readme_content = get_file_tool.func(
                        owner="kubevirt",
                        repo="enhancements",
                        path="README.md",
                        branch="main"
                    )
            
            readme_str = str(readme_content)
            # Check if it's an error message
            if len(readme_str) < 500 or readme_str.lower().startswith(("error", "failed", "cannot", "unable")):
                log(f"Received error or suspiciously short README (length: {len(readme_str)}): {readme_str[:500]}", node="indexer", level="WARNING")
                log(f"Available tools: {[t.name for t in tools]}", node="indexer", level="DEBUG")
                return None
            
            if readme_content and len(readme_str) > 100:
                log(f"Retrieved README.md (length: {len(readme_str)})", node="indexer")
                
                # Truncate if too long, but keep more than other files since it's critical
                return {
                    "content": readme_str[:20000] if len(readme_str) > 20000 else readme_str,
                    "full_length": len(readme_str),
                    "note": "This contains VEP process documentation, labels, structure, and requirements. Use this to understand how VEPs are organized and what to look for."
                }
            else:
                log("README.md content is too short or empty", node="indexer", level="WARNING")
                return None
                
        except Exception as e:
            log(f"Error reading README.md: {e}", node="indexer", level="WARNING")
            return None
            
    except Exception as e:
        log(f"Error in index_enhancements_readme: {e}", node="indexer", level="WARNING")
        return None
    
    return None


def index_vep_files() -> List[Dict[str, Any]]:
    """Index all VEP files in kubevirt/enhancements/veps/ directory.
    
    Parses the directory listing to extract VEP file names, then reads each VEP file
    to include its content in the indexed context. This prevents the LLM from needing
    to make many tool calls to read individual files.
    
    Returns:
        List of VEP file info with names and content
    """
    log("Indexing VEP files from kubevirt/enhancements/veps/", node="indexer")
    
    try:
        tools = get_mcp_tools_by_name("github")
        
        # Find file reading tool - try exact matches first
        get_file_tool = None
        tool_names_to_try = [
            "mcp_GitHub_get_file_contents",  # Try full name first
            "get_file_contents",
            "read_file",
            "get_file",
            "read_file_contents",
        ]
        
        # First try exact match
        for tool in tools:
            if tool.name in tool_names_to_try:
                get_file_tool = tool
                break
        
        # If no exact match, try partial
        if not get_file_tool:
            for tool in tools:
                if any(name.lower() in tool.name.lower() for name in tool_names_to_try):
                    get_file_tool = tool
                    break
        
        if not get_file_tool:
            log(f"Could not find file reading tool. Available tools: {[t.name for t in tools]}", node="indexer", level="WARNING")
            return []
        
        try:
            # Get directory listing with retry logic
            veps_content = _call_with_retry(
                get_file_tool.func,
                owner="kubevirt",
                repo="enhancements",
                path="veps"
            )
            
            # If that fails, try with path format
            if veps_content is None:
                try:
                    veps_content = _call_with_retry(
                        get_file_tool.func,
                        path="kubevirt/enhancements/veps"
                    )
                except TypeError:
                    # Function doesn't accept path parameter
                    pass
            
            if veps_content is None:
                log("Failed to read VEPs directory after retries", node="indexer", level="WARNING")
                return []
            
            content_str = str(veps_content)
            # Check if it's an error message
            if len(content_str) < 500 or content_str.lower().startswith(("error", "failed", "cannot", "unable")):
                log(f"Received error or suspiciously short VEPs directory content (length: {len(content_str)}): {content_str[:500]}", node="indexer", level="WARNING")
                log(f"Available tools: {[t.name for t in tools]}", node="indexer", level="DEBUG")
                return []
            
            log(f"Retrieved VEPs directory content (length: {len(content_str)})", node="indexer")
            log(f"Directory content preview (first 1000 chars): {content_str[:1000]}", node="indexer", level="DEBUG")
            
            # Parse directory listing to find subdirectories and VEP files
            vep_files = []  # List of (subdirectory, filename) tuples or just filenames
            subdirectories = []  # List of subdirectory paths to search
            
            # Try to parse as JSON first
            try:
                if isinstance(veps_content, str):
                    listing_data = json.loads(veps_content)
                elif isinstance(veps_content, (list, dict)):
                    listing_data = veps_content
                else:
                    listing_data = None
                
                if isinstance(listing_data, list):
                    for item in listing_data:
                        if isinstance(item, dict):
                            name = item.get("name") or item.get("path") or item.get("filename") or ""
                            file_type = item.get("type", "")
                            path = item.get("path", name)
                            
                            if file_type == "dir":
                                # It's a subdirectory - we'll search it for VEP files
                                if name and name not in ["NNNN-vep-template"]:  # Skip template directory
                                    subdirectories.append(path if "/" in path else f"veps/{name}")
                            elif file_type == "file":
                                # It's a file - check if it's a VEP file
                                basename = name.split("/")[-1] if "/" in name else name
                                # VEP files are .md files (not necessarily vep-\d+\.md pattern)
                                if basename.endswith('.md') and not basename.startswith('README') and 'template' not in basename.lower():
                                    # Store full path for reading later
                                    full_path = path if "/" in path else f"veps/{name}"
                                    vep_files.append(full_path)
                        elif isinstance(item, str):
                            basename = item.split("/")[-1] if "/" in item else item
                            # VEP files are .md files (not necessarily vep-\d+\.md pattern)
                            if basename.endswith('.md') and not basename.startswith('README') and 'template' not in basename.lower():
                                vep_files.append(item if "/" in item else f"veps/{item}")
                elif isinstance(listing_data, dict):
                    # Try common keys that might contain file list
                    for key in ["tree", "items", "contents", "files"]:
                        if key in listing_data and isinstance(listing_data[key], list):
                            for item in listing_data[key]:
                                if isinstance(item, dict):
                                    name = item.get("name") or item.get("path") or item.get("filename") or ""
                                    file_type = item.get("type", "")
                                    path = item.get("path", name)
                                    
                                    if file_type == "dir":
                                        # It's a subdirectory
                                        if name and name not in ["NNNN-vep-template"]:
                                            subdirectories.append(path if "/" in path else f"veps/{name}")
                                    elif file_type == "file":
                                        basename = name.split("/")[-1] if "/" in name else name
                                        # VEP files are .md files (not necessarily vep-\d+\.md pattern)
                                        if basename.endswith('.md') and not basename.startswith('README') and 'template' not in basename.lower():
                                            full_path = path if "/" in path else f"veps/{name}"
                                            vep_files.append(full_path)
                                elif isinstance(item, str):
                                    basename = item.split("/")[-1] if "/" in item else item
                                    # VEP files are .md files (not necessarily vep-\d+\.md pattern)
                                    if basename.endswith('.md') and not basename.startswith('README') and 'template' not in basename.lower():
                                        vep_files.append(item if "/" in item else f"veps/{item}")
            except (json.JSONDecodeError, TypeError, AttributeError) as e:
                log(f"Could not parse directory listing as JSON, trying regex extraction: {e}", node="indexer", level="DEBUG")
            
            # Now search each subdirectory for VEP files
            log(f"Found {len(subdirectories)} subdirectories to search: {subdirectories[:5]}{'...' if len(subdirectories) > 5 else ''}", node="indexer", level="DEBUG")
            
            for i, subdir in enumerate(subdirectories):
                # Add delay between requests to avoid rate limits
                if i > 0:
                    time.sleep(API_DELAY)  # Dynamic delay based on auth status

                try:
                    log(f"Reading subdirectory {subdir} ({i+1}/{len(subdirectories)})", node="indexer", level="DEBUG")
                    # Try with owner/repo/path format first
                    subdir_content = _call_with_retry(
                        get_file_tool.func,
                        owner="kubevirt",
                        repo="enhancements",
                        path=subdir
                    )

                    # If that fails, try with path format
                    if subdir_content is None:
                        try:
                            subdir_content = _call_with_retry(
                                get_file_tool.func,
                                path=f"kubevirt/enhancements/{subdir}"
                            )
                        except TypeError:
                            # Function doesn't accept path parameter, skip
                            log(f"Tool doesn't accept path parameter for {subdir}", node="indexer", level="DEBUG")
                            continue
                    
                    if subdir_content is None:
                        log(f"Failed to read subdirectory {subdir} after retries", node="indexer", level="WARNING")
                        continue
                    
                    subdir_str = str(subdir_content)
                    log(f"Subdirectory {subdir} content length: {len(subdir_str)}", node="indexer", level="DEBUG")
                    if len(subdir_str) < 100:
                        log(f"Skipping {subdir} - content too short", node="indexer", level="DEBUG")
                        continue
                    
                    # Parse subdirectory listing
                    files_found_in_subdir = 0
                    try:
                        if isinstance(subdir_content, str):
                            subdir_data = json.loads(subdir_content)
                        elif isinstance(subdir_content, (list, dict)):
                            subdir_data = subdir_content
                        else:
                            subdir_data = None
                        
                        if isinstance(subdir_data, list):
                            log(f"Subdirectory {subdir} contains {len(subdir_data)} items", node="indexer", level="DEBUG")
                            file_names_in_subdir = []
                            for item in subdir_data:
                                if isinstance(item, dict):
                                    name = item.get("name") or item.get("path") or ""
                                    file_type = item.get("type", "")
                                    path = item.get("path", name)
                                    
                                    if file_type == "dir":
                                        # It's a nested subdirectory - add it to the queue for recursive search
                                        full_subdir_path = path if "/" in path else f"{subdir}/{name}"
                                        if full_subdir_path not in subdirectories:
                                            subdirectories.append(full_subdir_path)
                                            log(f"Found nested subdirectory: {full_subdir_path}", node="indexer", level="DEBUG")
                                    elif file_type == "file":
                                        basename = name.split("/")[-1] if "/" in name else name
                                        file_names_in_subdir.append(basename)
                                        # VEP files are .md files in subdirectories (not vep-\d+\.md pattern)
                                        # Skip non-markdown files and template files
                                        if basename.endswith('.md') and not basename.startswith('README') and 'template' not in basename.lower():
                                            full_path = path if "/" in path else f"{subdir}/{name}"
                                            if full_path not in vep_files:
                                                vep_files.append(full_path)
                                                files_found_in_subdir += 1
                                                log(f"Found VEP file: {full_path}", node="indexer", level="DEBUG")
                            
                            # Log all file names for debugging
                            if file_names_in_subdir:
                                log(f"Files in {subdir}: {file_names_in_subdir[:10]}{'...' if len(file_names_in_subdir) > 10 else ''}", node="indexer", level="DEBUG")
                    except (json.JSONDecodeError, TypeError, AttributeError) as e:
                        log(f"Could not parse subdirectory {subdir} as JSON: {e}, trying regex", node="indexer", level="DEBUG")
                        # Fallback: extract using regex - look for .md files
                        # Match any .md filename (not just vep-\d+\.md)
                        md_pattern = r'([a-zA-Z0-9_-]+\.md)'
                        matches = re.findall(md_pattern, subdir_str)
                        for match in matches:
                            # Skip README and template files
                            if not match.startswith('README') and 'template' not in match.lower():
                                full_path = f"{subdir}/{match}"
                                if full_path not in vep_files:
                                    vep_files.append(full_path)
                                    files_found_in_subdir += 1
                                    log(f"Found VEP file via regex: {full_path}", node="indexer", level="DEBUG")
                    
                    log(f"Found {files_found_in_subdir} VEP file(s) in {subdir}", node="indexer", level="DEBUG")
                except Exception as e:
                    log(f"Error reading subdirectory {subdir}: {e}", node="indexer", level="DEBUG")
                    continue
            
            # Fallback: extract VEP file names using regex from string (handles paths too)
            if not vep_files:
                # Match any .md filename (not just vep-\d+\.md)
                md_pattern = r'([a-zA-Z0-9_-]+\.md)'
                matches = re.findall(md_pattern, content_str)
                # Filter out README and template files
                vep_files = [m for m in matches if not m.startswith('README') and 'template' not in m.lower()]
                vep_files = list(set(vep_files))  # Remove duplicates
                log(f"Extracted {len(vep_files)} VEP files using regex fallback", node="indexer", level="DEBUG")
            
            # Remove duplicates and sort VEP files numerically (vep-0176 > vep-0174)
            vep_files = list(set(vep_files))  # Remove duplicates first
            
            def vep_sort_key(path: str) -> int:
                # Extract VEP number from path (e.g., "veps/sig-compute/vep-0176.md" -> 176)
                # Also try to extract from filename patterns
                match = re.search(r'vep-(\d+)', path, re.IGNORECASE)
                if match:
                    return int(match.group(1))
                # If no number in path, return 0 (will sort to end)
                return 0
            
            vep_files = sorted(vep_files, key=vep_sort_key, reverse=True)
            
            log(f"Found {len(vep_files)} VEP files: {[f.split('/')[-1] for f in vep_files[:10]]}{'...' if len(vep_files) > 10 else ''}", node="indexer")
            log(f"Reading content of {len(vep_files)} VEP files (this may take a moment due to rate limiting delays)...", node="indexer")
            
            # Read each VEP file and include its content
            vep_data = []
            for i, vep_file_path in enumerate(vep_files):
                # Add delay between requests to avoid rate limits
                if i > 0:
                    time.sleep(API_DELAY)  # Dynamic delay based on auth status
                
                try:
                    # vep_file_path is already a full path like "veps/sig-compute/vep-0176.md"
                    vep_content = _call_with_retry(
                        get_file_tool.func,
                        owner="kubevirt",
                        repo="enhancements",
                        path=vep_file_path
                    )
                    
                    # If that fails, try with path format
                    if vep_content is None:
                        try:
                            vep_content = _call_with_retry(
                                get_file_tool.func,
                                path=f"kubevirt/enhancements/{vep_file_path}"
                            )
                        except TypeError:
                            # Function doesn't accept path parameter, skip
                            continue
                    
                    if vep_content is None:
                        log(f"Failed to read VEP file {vep_file_path} after retries", node="indexer", level="DEBUG")
                        # Still include the filename even if we can't read it
                        filename = vep_file_path.split("/")[-1]
                        # Try to extract VEP number from path
                        vep_number_match = re.search(r'vep-(\d+)', vep_file_path, re.IGNORECASE)
                        if vep_number_match:
                            vep_num = vep_number_match.group(1)
                            vep_number = f"vep-{int(vep_num):04d}" if vep_num.isdigit() else f"vep-{vep_num}"
                        else:
                            vep_number = filename.replace('.md', '')
                        vep_data.append({
                            "filename": filename,
                            "path": vep_file_path,
                            "vep_number": vep_number,
                            "content": None,
                            "error": "Rate limit or read failure",
                        })
                        continue
                    
                    content_str = str(vep_content)
                    if len(content_str) > 100 and not content_str.lower().startswith(("error", "failed", "cannot", "unable")):
                        # Extract just the filename for display
                        filename = vep_file_path.split("/")[-1]
                        
                        # Extract VEP number from multiple sources:
                        # 1. Try filename first (vep-0176.md)
                        vep_number_match = re.search(r'vep-(\d+)', vep_file_path, re.IGNORECASE)
                        vep_number = None
                        
                        if vep_number_match:
                            vep_number = vep_number_match.group(0)  # e.g., "vep-0176"
                        else:
                            # 2. Try to extract from file content (look for "VEP 176", "VEP-176", "VEP #176", etc.)
                            # Check first 2000 chars for VEP number references
                            content_preview = content_str[:2000]
                            vep_patterns = [
                                r'VEP\s*#?\s*(\d+)',  # "VEP #176", "VEP 176"
                                r'VEP-(\d+)',  # "VEP-176"
                                r'vep\s*#?\s*(\d+)',  # "vep #176", "vep 176"
                                r'vep-(\d+)',  # "vep-176"
                            ]
                            for pattern in vep_patterns:
                                match = re.search(pattern, content_preview, re.IGNORECASE)
                                if match:
                                    vep_num = match.group(1)
                                    # Format as vep-0176 (with leading zeros if needed)
                                    vep_number = f"vep-{int(vep_num):04d}" if vep_num.isdigit() else f"vep-{vep_num}"
                                    break
                        
                        # If still no VEP number found, use filename as fallback
                        if not vep_number:
                            vep_number = filename.replace('.md', '')

                        vep_data.append({
                            "filename": filename,
                            "path": vep_file_path,  # Full path for reference
                            "vep_number": vep_number,
                            "content": content_str[:50000] if len(content_str) > 50000 else content_str,  # Limit to 50k chars per file
                            "content_length": len(content_str),
                        })
                    else:
                        log(f"Skipping {vep_file_path} - suspicious content (length: {len(content_str)})", node="indexer", level="DEBUG")
                except Exception as e:
                    log(f"Error reading VEP file {vep_file_path}: {e}", node="indexer", level="DEBUG")
                    # Still include the filename even if we can't read it
                    filename = vep_file_path.split("/")[-1]
                    # Try to extract VEP number from path
                    vep_number_match = re.search(r'vep-(\d+)', vep_file_path, re.IGNORECASE)
                    if vep_number_match:
                        vep_num = vep_number_match.group(1)
                        vep_number = f"vep-{int(vep_num):04d}" if vep_num.isdigit() else f"vep-{vep_num}"
                    else:
                        vep_number = filename.replace('.md', '')
                    
                    vep_data.append({
                        "filename": filename,
                        "path": vep_file_path,
                        "vep_number": vep_number,
                        "content": None,
                        "error": str(e),
                    })
            
            log(f"Indexed {len(vep_data)} VEP files with content", node="indexer")
            return vep_data
            
        except Exception as e:
            log(f"Error reading veps directory: {e}", node="indexer", level="WARNING")
            return []
            
    except Exception as e:
        log(f"Error in index_vep_files: {e}", node="indexer", level="WARNING")
        return []

    return []


def _find_active_release_project(current_release: str) -> Optional[int]:
    """Discover active release project board ID by title matching.

    Searches for a project board with title matching the current release
    (e.g., "KubeVirt v1.8 Release Tracking").

    Args:
        current_release: Release version string (e.g., "v1.8")

    Returns:
        Project board number if found, None otherwise
    """
    from services.graphql_client import find_project_by_title

    if not current_release:
        return None

    # Normalize version string (ensure it has 'v' prefix)
    version = current_release if current_release.startswith('v') else f'v{current_release}'

    # Search pattern: version + "release" + "tracking"
    # This should match boards like "KubeVirt v1.8 Release Tracking"
    search_pattern = f"{version} release tracking"

    try:
        project_num = find_project_by_title(org_name="kubevirt", title_pattern=search_pattern)
        if project_num:
            log(f"Auto-discovered project board for {version}: #{project_num}", node="indexer")
        return project_num
    except Exception as e:
        log(f"Error auto-discovering project board: {e}", node="indexer", level="WARNING")
        return None


def _parse_impl_prs_from_text(text: str) -> List[Dict[str, Any]]:
    """Parse implementation PR references from text content.

    Extracts PR numbers from patterns like:
    - https://github.com/kubevirt/kubevirt/pull/12345
    - Implementation: #1234
    - Implements: #1234
    - impl: #1234
    - PR: #1234
    - pull #1234

    IMPORTANT: Excludes PRs from kubevirt/enhancements (those are proposal PRs, not impl PRs)

    Args:
        text: Text content to parse (issue body, notes, etc.)

    Returns:
        List of dicts with {number: int, url: str}
    """
    if not text:
        return []

    impl_prs = []
    seen_numbers = set()

    # First, find all enhancements PR numbers to EXCLUDE (those are proposal PRs, not impl PRs)
    enhancements_pr_numbers = set()
    enhancements_url_pattern = re.compile(r'github\.com/kubevirt/enhancements/pull/(\d+)')
    for match in enhancements_url_pattern.finditer(text):
        enhancements_pr_numbers.add(int(match.group(1)))

    # Pattern 1: Full PR URLs from kubevirt/kubevirt repo ONLY
    url_pattern = re.compile(r'https?://github\.com/kubevirt/kubevirt/pull/(\d+)')
    for match in url_pattern.finditer(text):
        pr_num = int(match.group(1))
        if pr_num not in seen_numbers and pr_num not in enhancements_pr_numbers:
            seen_numbers.add(pr_num)
            impl_prs.append({
                "number": pr_num,
                "url": match.group(0),
            })

    # Pattern 2: PR references like "Implementation: #1234", "implements #1234", "impl: #1234", etc.
    # ONLY if the PR number is not from enhancements repo
    ref_pattern = re.compile(
        r'(?:Implementation|Implements|impl)[\s:]+#(\d+)',
        re.IGNORECASE
    )
    for match in ref_pattern.finditer(text):
        pr_num = int(match.group(1))
        if pr_num not in seen_numbers and pr_num not in enhancements_pr_numbers:
            seen_numbers.add(pr_num)
            impl_prs.append({
                "number": pr_num,
                "url": f"https://github.com/kubevirt/kubevirt/pull/{pr_num}",
            })

    # Pattern 3: Standalone #numbers that might be PR references
    # (only if they appear after keywords like "implementation", "implements", "impl:")
    # EXCLUDE generic "PR" and "pull" keywords to avoid false positives with proposal PRs
    # NOTE: No re.DOTALL — .*? must NOT span newlines to avoid matching unrelated #numbers
    context_pattern = re.compile(
        r'(?:implementation|implements|impl).*?#(\d+)',
        re.IGNORECASE
    )
    for match in context_pattern.finditer(text):
        pr_num = int(match.group(1))
        if pr_num not in seen_numbers and pr_num not in enhancements_pr_numbers:
            seen_numbers.add(pr_num)
            impl_prs.append({
                "number": pr_num,
                "url": f"https://github.com/kubevirt/kubevirt/pull/{pr_num}",
            })

    return impl_prs


def index_project_board_items(version: Optional[str] = None) -> Dict[int, Dict[str, Any]]:
    """Index VEP items from the kubevirt GitHub Project V2 board.

    Fetches all VEPs from the project board for the given release version,
    including all custom field metadata (Status, Priority, dates, etc.).
    Also extracts implementation PR references from issue bodies and text fields.

    Args:
        version: Release version string (e.g., "v1.8" or "1.8").
                 If None, will try to auto-detect from release schedule.

    Returns:
        Dict mapping issue_number -> {
            title, url, state, fields: {...},
            impl_prs: [{number, url}, ...]
        }
        Empty dict if board cannot be fetched.
    """
    from config import get_project_board_for_version
    from services.graphql_client import get_veps_from_project_board

    log(f"Indexing project board items for version: {version}", node="indexer")

    # Try auto-discovery first
    board_number = _find_active_release_project(version) if version else None

    # Fallback to config mapping
    if board_number is None:
        board_number = get_project_board_for_version(version)

    if board_number is None:
        log(f"No project board found for version: {version}", node="indexer", level="WARNING")
        return {}

    try:
        veps = get_veps_from_project_board(project_number=board_number)

        # Enhance each VEP with implementation PR data
        for issue_num, vep_data in veps.items():
            impl_prs = []

            # Parse implementation PRs from issue body
            body = vep_data.get("body", "")
            if body:
                impl_prs.extend(_parse_impl_prs_from_text(body))

            # Also check text fields (Notes, Implementation PRs, etc.)
            fields = vep_data.get("fields", {})
            for field_name, field_value in fields.items():
                if isinstance(field_value, str):
                    impl_prs.extend(_parse_impl_prs_from_text(field_value))

            # Store unique implementation PRs
            vep_data["impl_prs"] = impl_prs

        pr_count = sum(len(v.get("impl_prs", [])) for v in veps.values())
        log(f"Indexed {len(veps)} VEPs from project board #{board_number} with {pr_count} impl PR references", node="indexer")
        return veps
    except Exception as e:
        log(f"Error indexing project board: {e}", node="indexer", level="WARNING")
        return {}


def index_vep_pr_mappings(prs_index: Optional[List[Dict[str, Any]]] = None) -> Dict[str, List[Dict[str, Any]]]:
    """Pre-compute VEP number to PR mappings from kubevirt/kubevirt PRs.

    Searches PR titles and bodies for VEP references using patterns:
    - vep-{number}, VEP-{number}
    - vep {number}, VEP {number}
    - vep#{number}, VEP#{number}

    Args:
        prs_index: Optional pre-fetched PRs list. If None, will be fetched.

    Returns:
        Dict mapping vep_number (e.g., "176") -> [list of matching PRs]
    """
    log("Computing VEP-to-PR mappings from kubevirt/kubevirt", node="indexer")

    if prs_index is None:
        prs_index = index_kubevirt_prs()

    vep_pattern = re.compile(r'vep[-\s#]?(\d+)', re.IGNORECASE)
    mappings: Dict[str, List[Dict[str, Any]]] = {}

    for pr in prs_index:
        if not isinstance(pr, dict):
            continue
        # Skip raw_data entries
        if "raw_data" in pr:
            continue

        # Search in title and body
        title = pr.get("title", "") or ""
        body = pr.get("body_preview", "") or pr.get("body", "") or ""
        content = f"{title} {body}"

        matches = vep_pattern.findall(content)
        for vep_num in set(matches):  # Dedupe matches in same PR
            if vep_num not in mappings:
                mappings[vep_num] = []
            mappings[vep_num].append({
                "number": pr.get("number"),
                "title": pr.get("title"),
                "state": pr.get("state"),
                "merged": pr.get("merged", False),
                "url": pr.get("html_url") or pr.get("url"),
                "labels": pr.get("labels", []),
                "updated_at": pr.get("updated_at"),
            })

    total_prs = sum(len(prs) for prs in mappings.values())
    log(f"Mapped {total_prs} PRs to {len(mappings)} VEPs", node="indexer")
    return mappings


def _extract_proposal_prs_from_issue(issue_body: str) -> List[int]:
    """Extract proposal PR numbers from tracking issue body.

    The tracking issue is the authoritative source for proposal PRs.
    Looks for enhancements PR URLs in the issue body.

    Returns:
        List of PR numbers
    """
    if not issue_body:
        return []

    pr_numbers = set()

    # Pattern for enhancements PR URLs (most reliable)
    pr_url_pattern = re.compile(r'github\.com/kubevirt/enhancements/pull/(\d+)')
    for match in pr_url_pattern.finditer(issue_body):
        pr_numbers.add(int(match.group(1)))

    return sorted(list(pr_numbers))


def index_approved_vep_prs(prs_index: Optional[List[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    """Index PRs with 'approved-vep' label from kubevirt/kubevirt.

    These are PRs that implement approved VEPs. Per vladikr's approach,
    monitoring these helps identify lingering implementation PRs.

    Also detects mismatched PRs: those with 'approved-vep' label but no
    VEP reference in title/body (indicates labeling error).

    Args:
        prs_index: Optional pre-fetched PRs list. If None, will be fetched.

    Returns:
        List of PRs with approved-vep label, enriched with staleness and mismatch info.
    """
    log("Indexing 'approved-vep' labeled PRs", node="indexer")

    if prs_index is None:
        prs_index = index_kubevirt_prs()

    approved_vep_prs = []
    now = datetime.now()

    # Pattern to detect VEP references (e.g., "VEP-123", "vep 123", "VEP#123")
    vep_pattern = re.compile(r'vep[-\s#]?(\d+)', re.IGNORECASE)

    for pr in prs_index:
        if not isinstance(pr, dict):
            continue
        # Skip raw_data entries
        if "raw_data" in pr:
            continue

        labels = pr.get("labels", [])
        # Handle both string labels and dict labels
        label_names = []
        for label in labels:
            if isinstance(label, str):
                label_names.append(label.lower())
            elif isinstance(label, dict):
                label_names.append(label.get("name", "").lower())

        if "approved-vep" in label_names:
            # Calculate staleness
            updated_at = pr.get("updated_at")
            days_since_update = None
            if updated_at:
                try:
                    if isinstance(updated_at, str):
                        updated_dt = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
                        # Make naive for comparison
                        updated_dt = updated_dt.replace(tzinfo=None)
                    else:
                        updated_dt = updated_at
                    days_since_update = (now - updated_dt).days
                except (ValueError, TypeError):
                    pass

            # Check for VEP reference in title and body
            title = pr.get("title", "")
            body = pr.get("body", "")
            text_to_search = f"{title} {body}"
            vep_matches = vep_pattern.findall(text_to_search)
            has_vep_reference = len(vep_matches) > 0

            approved_vep_prs.append({
                "number": pr.get("number"),
                "title": pr.get("title"),
                "state": pr.get("state"),
                "url": pr.get("html_url") or pr.get("url"),
                "labels": labels,
                "updated_at": updated_at,
                "days_since_update": days_since_update,
                "is_open": pr.get("state") == "open",
                "has_vep_reference": has_vep_reference,
                "is_mismatched": not has_vep_reference,  # Label exists but no VEP ref
                "matched_vep_numbers": vep_matches if has_vep_reference else [],
            })

    # Count mismatched for logging
    mismatched_count = sum(1 for pr in approved_vep_prs if pr.get("is_mismatched"))
    log(f"Found {len(approved_vep_prs)} PRs with 'approved-vep' label ({mismatched_count} mismatched)", node="indexer")
    return approved_vep_prs


def _load_cached_index(cache_file: Path, max_age_minutes: int = 60) -> Optional[Dict[str, Any]]:
    """Load cached indexed context if it exists and is fresh.
    
    Args:
        cache_file: Path to the cache file
        max_age_minutes: Maximum age of cache in minutes (default: 60)
    
    Returns:
        Cached indexed context if fresh, None otherwise
    """
    if not cache_file.exists():
        log(f"Cache file not found: {cache_file}", node="indexer", level="DEBUG")
        return None
    
    try:
        with open(cache_file, 'r', encoding='utf-8') as f:
            cache_data = json.load(f)
        
        # Check if cache has timestamp
        cached_at_str = cache_data.get("cached_at")
        if not cached_at_str:
            log("Cache file missing timestamp, will regenerate", node="indexer", level="DEBUG")
            return None
        
        # Parse timestamp
        cached_at = datetime.fromisoformat(cached_at_str)
        age = datetime.now() - cached_at
        age_minutes = age.total_seconds() / 60
        
        if age_minutes < max_age_minutes:
            log(f"Using cached index (age: {age_minutes:.1f} minutes, max: {max_age_minutes} minutes)", node="indexer")
            # Remove cached_at from returned data (it's metadata, not part of indexed context)
            cached_context = cache_data.copy()
            cached_context.pop("cached_at", None)
            cached_context.pop("cache_age_minutes", None)
            return cached_context
        else:
            log(f"Cache expired (age: {age_minutes:.1f} minutes, max: {max_age_minutes} minutes), will regenerate", node="indexer")
            return None
    
    except (json.JSONDecodeError, ValueError, KeyError) as e:
        log(f"Error reading cache file: {e}, will regenerate", node="indexer", level="WARNING")
        return None


def _save_cached_index(cache_file: Path, indexed_context: Dict[str, Any]) -> None:
    """Save indexed context to cache file.
    
    Args:
        cache_file: Path to the cache file
        indexed_context: The indexed context to cache
    """
    try:
        # Add timestamp to cache
        cache_data = indexed_context.copy()
        cache_data["cached_at"] = datetime.now().isoformat()
        
        # Write to cache file
        with open(cache_file, 'w', encoding='utf-8') as f:
            json.dump(cache_data, f, indent=2, default=str)
        
        log(f"Saved indexed context to cache: {cache_file}", node="indexer", level="DEBUG")
    
    except Exception as e:
        log(f"Error saving cache file: {e}", node="indexer", level="WARNING")
        # Don't fail if cache save fails - indexing still succeeded


def _parse_date_from_text(text: str) -> Optional[datetime]:
    """Parse a date from text, supporting various formats.

    Supports formats like:
    - "2025-01-15"
    - "Jan 15, 2025"
    - "January 15, 2025"
    - "15 Jan 2025"

    Returns:
        datetime if parsed successfully, None otherwise
    """
    import calendar

    # Try ISO format first
    try:
        return datetime.fromisoformat(text.strip())
    except ValueError:
        pass

    # Try common date formats
    date_patterns = [
        r'(\d{4})-(\d{1,2})-(\d{1,2})',  # 2025-01-15
        r'(\d{4})/(\d{1,2})/(\d{1,2})',  # 2025/01/15
        r'(\w+)\s+(\d{1,2}),?\s+(\d{4})',  # Jan 15, 2025 or January 15, 2025
        r'(\d{1,2})\s+(\w+)\s+(\d{4})',  # 15 Jan 2025
    ]

    month_names = {name.lower(): num for num, name in enumerate(calendar.month_name) if num}
    month_abbrs = {name.lower(): num for num, name in enumerate(calendar.month_abbr) if num}

    for pattern in date_patterns:
        match = re.search(pattern, text)
        if match:
            groups = match.groups()
            try:
                if pattern == date_patterns[0]:  # ISO-like with dashes
                    year, month, day = int(groups[0]), int(groups[1]), int(groups[2])
                elif pattern == date_patterns[1]:  # ISO-like with slashes
                    year, month, day = int(groups[0]), int(groups[1]), int(groups[2])
                elif pattern == date_patterns[2]:  # Month Day, Year
                    month_str, day, year = groups[0].lower(), int(groups[1]), int(groups[2])
                    month = month_names.get(month_str) or month_abbrs.get(month_str[:3])
                    if not month:
                        continue
                else:  # Day Month Year
                    day, month_str, year = int(groups[0]), groups[1].lower(), int(groups[2])
                    month = month_names.get(month_str) or month_abbrs.get(month_str[:3])
                    if not month:
                        continue
                return datetime(year, month, day)
            except (ValueError, TypeError):
                continue

    return None

def compute_release_phase(release_info: dict) -> tuple[str, dict[str, str | None]]:
    """
    Parses the schedule_content markdown to find key dates:
    - Enhancement Freeze (EF)
    - Code Freeze (CF)
    - Release date

    Returns:
        One of: "design", "development", "stabilization", "post_release", "unknown"
        - "design": Before Enhancement Freeze - focus on VEP tracking/approval
        - "development": Between EF and Code Freeze - focus on implementation
        - "stabilization": After Code Freeze - prioritize compliance
        - "post_release": Post-release
        - "unknown": Could not determine phase
    """
    schedule_content = release_info.get("schedule_content", "")
    if not schedule_content:
        return "unknown", {}

    dates = {
        "enhancement_freeze": None,
        "code_freeze": None,
        "ga": None,
    }

    for line in schedule_content.splitlines():
        line_clean = line.replace("**", "").strip()
        if not line_clean:
            continue

        # Try to parse date using flexible parser
        parsed_date = _parse_date_from_text(line_clean)
        if not parsed_date:
            continue
        date_obj = parsed_date.date()

        line_lower = line_clean.lower()

        # Match enhancement freeze with multiple patterns (fallback mechanism)
        enhancement_patterns = [
            "virtualization enhancement proposal (vep) freeze",
            "vep freeze",
            "enhancement freeze",
            "enhancement_freeze"
        ]
        if not dates["enhancement_freeze"] and any(pattern in line_lower for pattern in enhancement_patterns):
            dates["enhancement_freeze"] = date_obj
            log(f"Matched Enhancement Freeze: {date_obj} on line: {line_clean[:100]}", node="indexer", level="DEBUG")

        # Match code freeze with multiple patterns (fallback mechanism)
        code_freeze_patterns = [
            "kubevirt code freeze",
            "code freeze",
            "code_freeze"
        ]
        if not dates["code_freeze"] and any(pattern in line_lower for pattern in code_freeze_patterns):
            dates["code_freeze"] = date_obj
            log(f"Matched Code Freeze: {date_obj} on line: {line_clean[:100]}", node="indexer", level="DEBUG")

        # Match GA/release with multiple patterns (fallback mechanism)
        ga_patterns = [
            "kubevirt ga",
            "ga release",
            "general availability",
            "kubevirt release"
        ]
        if not dates["ga"] and any(pattern in line_lower for pattern in ga_patterns):
            dates["ga"] = date_obj
            log(f"Matched GA: {date_obj} on line: {line_clean[:100]}", node="indexer", level="DEBUG")

    today = datetime.now(timezone.utc).date()

    ef = dates["enhancement_freeze"]
    cf = dates["code_freeze"]
    ga = dates["ga"]

    if ef and today < ef:
        phase = "design"
    elif cf and today < cf:
        phase = "development"
    elif ga and today < ga:
        phase = "stabilization"
    else:
        phase = "post_release"

    deadlines = {
        key: date.isoformat() if date else None
        for key, date in dates.items()
    }

    log(f"Final parsed deadlines: {deadlines}, phase: {phase}", node="indexer")

    return phase, deadlines

def compute_veps_missing_prs(
    project_board_items: Dict[int, Dict[str, Any]],
    vep_to_pr_mappings: Dict[str, List[Dict[str, Any]]]
) -> List[Dict[str, Any]]:
    """Find tracked VEPs that have no implementation PRs.

    VEPs with status "Tracked" or "At risk" should have implementation PRs.
    This function identifies VEPs on the project board that are missing PRs.

    Prioritizes board data (impl_prs from issue body/fields) over label-based
    VEP-to-PR mappings for more accurate tracking.

    Args:
        project_board_items: Dict mapping issue number to board item data
            (includes impl_prs field if parsed from board)
        vep_to_pr_mappings: Dict mapping VEP number (string) to list of PRs
            (from label-based matching as fallback)

    Returns:
        List of VEPs that are tracked but have no implementation PRs
    """
    missing = []
    for issue_num, item in project_board_items.items():
        if not isinstance(item, dict):
            continue

        fields = item.get("fields", {})
        status = fields.get("Status", "")

        # Only check VEPs that are actively tracked
        if status in ["Tracked", "At risk"]:
            # Check board data first (impl_prs from issue body/fields)
            board_impl_prs = item.get("impl_prs", [])
            has_impl_prs = len(board_impl_prs) > 0

            # Fallback to label-based mappings if no board data
            if not has_impl_prs:
                vep_num = str(issue_num)
                label_prs = vep_to_pr_mappings.get(vep_num, [])
                has_impl_prs = len(label_prs) > 0

            # VEP is missing PRs if neither source has any
            if not has_impl_prs:
                missing.append({
                    "issue_number": issue_num,
                    "title": item.get("title"),
                    "status": status,
                    "url": item.get("url"),
                    "priority": fields.get("Priority"),
                })

    log(f"Found {len(missing)} tracked VEPs without implementation PRs", node="indexer")
    return missing


def create_indexed_context(days_back: Optional[int] = 365, cache_max_age_minutes: int = 60) -> Dict[str, Any]:
    """Create a comprehensive indexed context for VEP discovery.
    
    This pre-fetches key information so the LLM has a complete picture
    of what exists before starting discovery. Results are cached to avoid
    re-indexing on every run.
    
    Args:
        days_back: Only include issues/PRs from last N days (None = all items)
                   Default 365 days to avoid overwhelming context
        cache_max_age_minutes: Maximum age of cache in minutes before regenerating (default: 60)
    
    Returns:
        Dict with indexed information:
        - release_info: Current release and schedule
        - enhancements_readme: README.md content with VEP process documentation
        - issues_index: List of issues in enhancements repo
        - prs_index: List of PRs in kubevirt repo
        - vep_files_index: List of VEP files in veps/ directory
        - board_veps: VEPs from GitHub Project V2 board with all fields
        - vep_to_pr_mappings: Pre-computed VEP number to implementation PR mappings
        - approved_vep_prs: PRs with 'approved-vep' label
    """
    # Try to load from cache first
    cached_context = _load_cached_index(CACHE_FILE, cache_max_age_minutes)
    if cached_context is not None:
        # Verify cache has expected structure
        if all(key in cached_context for key in ["release_info", "enhancements_readme", "issues_index", "prs_index", "vep_files_index"]):
            # Cache is valid and fresh
            log(f"Using cached indexed context (days_back={cached_context.get('days_back', 'unknown')})", node="indexer")
            return cached_context
        else:
            log("Cache file missing required keys, will regenerate", node="indexer", level="WARNING")
    
    # Cache miss or expired - create new index
    log(f"Creating indexed context for VEP discovery (days_back={days_back}, cache_max_age_minutes={cache_max_age_minutes})", node="indexer")

    # First fetch release info to get version for project board lookup
    release_info = index_release_schedule()
    release_version = release_info.get("current_release") if release_info else None

    # Compute PR lookback: ~30 days before enhancement freeze (start of dev cycle)
    # This covers the full development window without fetching years of old PRs
    pr_days_back = 365  # Default: 1 year
    if release_info:
        deadlines = release_info.get("release_deadlines", {})
        ef_str = deadlines.get("enhancement_freeze")
        if ef_str:
            try:
                ef_date = datetime.fromisoformat(ef_str.replace('Z', '+00:00'))
                if ef_date.tzinfo is None:
                    ef_date = ef_date.replace(tzinfo=timezone.utc)
                days_since_ef = (datetime.now(timezone.utc) - ef_date).days
                # 30 days before EF + time since EF
                pr_days_back = max(days_since_ef + 30, 90)  # At least 90 days
                log(f"PR lookback: {pr_days_back} days (EF was {days_since_ef} days ago)", node="indexer")
            except (ValueError, AttributeError):
                pass

    prs_index = index_kubevirt_prs(days_back=pr_days_back)

    # Also search for kubevirt PRs referencing VEP issues via GitHub search API
    # list_pull_requests may not return full PR bodies, so search is more reliable
    # for finding PRs that reference enhancements tracking issues
    search_prs = _search_kubevirt_prs_referencing_veps()
    if search_prs:
        existing_pr_nums = {pr.get("number") for pr in prs_index if isinstance(pr, dict)}
        new_count = 0
        updated_count = 0
        for pr in search_prs:
            pr_num = pr.get("number")
            if pr_num not in existing_pr_nums:
                prs_index.append(pr)
                existing_pr_nums.add(pr_num)
                new_count += 1
            else:
                # Update existing entry with vep_issue_number/merged if missing
                for existing in prs_index:
                    if isinstance(existing, dict) and existing.get("number") == pr_num:
                        if not existing.get("vep_issue_number") and pr.get("vep_issue_number"):
                            existing["vep_issue_number"] = pr["vep_issue_number"]
                            updated_count += 1
                        if not existing.get("merged") and pr.get("merged"):
                            existing["merged"] = pr["merged"]
                            existing["state"] = "merged"
                        break
        log(f"Search merge: {new_count} new PRs added, {updated_count} existing PRs updated with VEP issue numbers", node="indexer")

    # Get ALL enhancements PRs (proposal PRs can be very old - VEPs exist for years)
    # Don't filter by date for proposal PRs
    enhancements_prs = index_enhancements_prs(days_back=None)

    # Fetch project board items and VEP-to-PR mappings (needed for compute_veps_missing_prs)
    project_board_items = index_project_board_items(version=release_version)
    vep_to_pr_mappings = index_vep_pr_mappings(prs_index=prs_index)

    indexed_context = {
        "release_info": release_info,
        "current_release": release_version,
        "enhancements_readme": index_enhancements_readme(),
        "issues_index": index_enhancements_issues(days_back=days_back),
        "prs_index": prs_index,
        "enhancements_prs": enhancements_prs,
        "vep_files_index": index_vep_files(),
        # Board data with impl_prs (source-of-truth for targeted VEPs)
        "board_veps": project_board_items,
        # Pre-computed VEP-to-PR mappings
        "vep_to_pr_mappings": vep_to_pr_mappings,
        # PRs with approved-vep label
        "approved_vep_prs": index_approved_vep_prs(prs_index=prs_index),
        # VEPs that are tracked but have no implementation PRs
        "veps_missing_prs": compute_veps_missing_prs(project_board_items, vep_to_pr_mappings),
        "indexed_at": datetime.now().isoformat(),
        "days_back": days_back,
    }
    indexed_context["release_phase"] = release_info.get("release_phase", "unknown") if release_info else "unknown"
    indexed_context["release_deadlines"] = release_info.get("release_deadlines", {}) if release_info else {}

    # Log summary
    release = release_version if release_version else "unknown"
    readme_available = "yes" if indexed_context["enhancements_readme"] else "no"
    issues_count = len(indexed_context["issues_index"])
    prs_count = len(indexed_context["prs_index"])
    enhancements_prs_count = len(indexed_context["enhancements_prs"])
    vep_files_count = len(indexed_context["vep_files_index"])
    board_items_count = len(indexed_context["board_veps"])
    vep_pr_mappings_count = len(indexed_context["vep_to_pr_mappings"])
    approved_vep_prs_count = len(indexed_context["approved_vep_prs"])
    veps_missing_prs_count = len(indexed_context["veps_missing_prs"])
    phase = indexed_context["release_phase"]
    deadlines = indexed_context["release_deadlines"]

    log(f"Indexed context created: release={release}, readme={readme_available}, issues={issues_count}, prs={prs_count}, enhancements_prs={enhancements_prs_count}, vep_files={vep_files_count}, phase={phase}, deadlines={deadlines}", node="indexer")
    log(f"  - Project board items: {board_items_count}, VEP-to-PR mappings: {vep_pr_mappings_count}, approved-vep PRs: {approved_vep_prs_count}, VEPs missing PRs: {veps_missing_prs_count}", node="indexer")

    # Diagnostic: log issues excluded as non-VEP related
    all_issues = indexed_context["issues_index"]
    vep_related_count = sum(1 for i in all_issues if i.get("is_vep_related", False))
    non_vep_count = issues_count - vep_related_count
    log(f"  - VEP-related issues: {vep_related_count}, excluded as non-VEP: {non_vep_count}", node="indexer")

    if non_vep_count > 0:
        excluded_issues = [i for i in all_issues if not i.get("is_vep_related", False)]
        excluded_titles = [f"#{i.get('number')}: {i.get('title', 'untitled')[:50]}" for i in excluded_issues[:10]]
        log(f"  - Excluded issues (first 10): {', '.join(excluded_titles)}", node="indexer", level="DEBUG")

    # Save to cache
    _save_cached_index(CACHE_FILE, indexed_context)
    
    # Extract VEP numbers from issues and match them to files
    # This helps identify VEPs that only exist as issues (no file yet)
    vep_related_issues = [i for i in indexed_context.get("issues_index", []) if i.get("is_vep_related", False)]
    vep_files_index = indexed_context.get("vep_files_index", [])
    
    # Extract VEP numbers from issues
    vep_numbers_from_issues = set()
    for issue in vep_related_issues:
        title = issue.get("title", "")
        body = issue.get("body_preview", "")
        # Try to extract VEP number from title/body
        vep_patterns = [
            r'VEP\s*#?\s*(\d+)',
            r'VEP-(\d+)',
            r'vep\s*#?\s*(\d+)',
            r'vep-(\d+)',
        ]
        for pattern in vep_patterns:
            match = re.search(pattern, title + " " + body, re.IGNORECASE)
            if match:
                vep_num = match.group(1)
                vep_numbers_from_issues.add(int(vep_num))
                break
    
    # Extract VEP numbers from files
    vep_numbers_from_files = set()
    vep_numbers_from_files_formatted = []
    for vep_file in vep_files_index:
        vep_number = vep_file.get("vep_number", "")
        # Extract numeric part from vep_number (e.g., "vep-0176" -> 176)
        match = re.search(r'vep-(\d+)', vep_number, re.IGNORECASE)
        if match:
            vep_num = int(match.group(1))
            vep_numbers_from_files.add(vep_num)
            vep_numbers_from_files_formatted.append(f"vep-{vep_num:04d}")
    
    # Find VEPs that exist only as issues (no file)
    vep_numbers_only_in_issues = vep_numbers_from_issues - vep_numbers_from_files
    
    log(f"  - VEP-related issues: {len(vep_related_issues)}", node="indexer")
    log(f"  - VEP files with content: {sum(1 for f in vep_files_index if f.get('content'))}", node="indexer")
    log(f"  - VEP files without content (errors): {sum(1 for f in vep_files_index if not f.get('content'))}", node="indexer")
    log(f"  - Unique VEP numbers from issues: {len(vep_numbers_from_issues)}", node="indexer")
    log(f"  - Unique VEP numbers from files: {len(vep_numbers_from_files)}", node="indexer")
    if vep_numbers_from_files_formatted:
        log(f"  - VEP numbers found in files: {', '.join(sorted(vep_numbers_from_files_formatted))}", node="indexer")
    if vep_numbers_only_in_issues:
        log(f"  - VEPs only in issues (no file): {sorted(vep_numbers_only_in_issues)}", node="indexer")
    
    # DEBUG: Print all indexed VEP files and issues, then exit (only if debug mode is enabled)
    import os
    debug_mode = os.environ.get("DEBUG_MODE")
    if debug_mode == "discover-veps":
        import sys
        vep_related_issues = [issue for issue in indexed_context["issues_index"] if issue.get("is_vep_related", False)]
        
        log("\n" + "="*80, node="indexer", level="INFO")
        log("DEBUG: Indexed Context Summary", node="indexer", level="INFO")
        log("="*80, node="indexer", level="INFO")
        log(f"Release: {release}", node="indexer", level="INFO")
        log(f"VEP Files: {vep_files_count}", node="indexer", level="INFO")
        log(f"Total Issues: {issues_count} ({len(vep_related_issues)} VEP-related)", node="indexer", level="INFO")
        log(f"PRs: {prs_count}", node="indexer", level="INFO")
        log("\n" + "-"*80, node="indexer", level="INFO")
        log(f"VEP Files ({vep_files_count}):", node="indexer", level="INFO")
        log("-"*80, node="indexer", level="INFO")
        for i, vep_file in enumerate(indexed_context["vep_files_index"], 1):
            filename = vep_file.get("filename", "N/A")
            vep_number = vep_file.get("vep_number", "N/A")
            has_content = "✓" if vep_file.get("content") else "✗"
            log(f"{i:2d}. {has_content} {filename:40s} | VEP: {vep_number}", node="indexer", level="INFO")
        
        log("\n" + "-"*80, node="indexer", level="INFO")
        log(f"VEP-Related Issues ({len(vep_related_issues)}):", node="indexer", level="INFO")
        log("-"*80, node="indexer", level="INFO")
        for i, issue in enumerate(vep_related_issues, 1):
            issue_num = issue.get("number", "N/A")
            issue_title = issue.get("title", "N/A")[:50]
            issue_state = issue.get("state", "N/A")
            # Convert issue_num to string if it's an integer
            issue_num_str = str(issue_num) if isinstance(issue_num, int) else issue_num
            log(f"{i:2d}. [{issue_state:6s}] #{issue_num_str:6s} | {issue_title}", node="indexer", level="INFO")
        
        log("="*80, node="indexer", level="INFO")
        log("\nExiting for debug purposes...", node="indexer", level="INFO")
        sys.exit(0)
    
    return indexed_context
