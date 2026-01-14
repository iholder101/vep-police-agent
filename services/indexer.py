"""Indexing service to pre-fetch key information for VEP discovery.

This module provides functions to index critical information before LLM processing,
ensuring the LLM has a complete picture of what exists rather than having to discover it.
"""

import re
import json
from typing import Dict, List, Any, Optional
from datetime import datetime, timedelta
from services.utils import log
from services.mcp_factory import get_mcp_tools_by_name


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
                        log(f"Found release schedule for {version} (newest available)", node="indexer")
                        content_str = str(schedule_content)
                        return {
                            "current_release": version,
                            "schedule_path": schedule_path,
                            "schedule_content": content_str[:10000] if len(content_str) > 10000 else content_str,
                            "all_versions_found": sorted_versions,
                        }
                except Exception as e:
                    log(f"Error fetching schedule for {version}: {e}", node="indexer", level="DEBUG")
                    continue
        else:
            log("Could not extract version numbers from releases directory", node="indexer", level="WARNING")
        
        # Fallback: try common recent versions if directory listing failed
        log("Falling back to trying common recent versions", node="indexer")
        fallback_versions = _sort_versions_numerically(["v1.11", "v1.10", "v1.9", "v1.8", "v1.7"])
        
        for version in fallback_versions:
            try:
                schedule_path = f"releases/{version}/schedule.md"
                schedule_content = get_file_tool.func(
                    owner="kubevirt",
                    repo="sig-release",
                    path=schedule_path
                )
                
                if schedule_content and len(str(schedule_content)) > 100:
                    log(f"Found release schedule for {version} (fallback)", node="indexer")
                    content_str = str(schedule_content)
                    return {
                        "current_release": version,
                        "schedule_path": schedule_path,
                        "schedule_content": content_str[:10000] if len(content_str) > 10000 else content_str,
                    }
            except Exception:
                continue
                    
    except Exception as e:
        log(f"Error in index_release_schedule: {e}", node="indexer", level="WARNING")
    
    log("Could not determine current release schedule", node="indexer", level="WARNING")
    return None


def _filter_by_date(items: List[Dict[str, Any]], days: int = 365) -> List[Dict[str, Any]]:
    """Filter items to only include those from the last N days.
    
    Args:
        items: List of items with 'created_at' or 'updated_at' fields
        days: Number of days to look back (default 365)
    
    Returns:
        Filtered list of items
    """
    cutoff_date = datetime.now() - timedelta(days=days)
    filtered = []
    
    for item in items:
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
        
        # Find list_issues tool - try exact matches first, then partial
        list_issues_tool = None
        tool_names_to_try = [
            "mcp_GitHub_list_issues",  # Try full name first
            "list_issues",
            "search_issues",
            "get_issues",
            "mcp_GitHub_search_issues",
        ]
        
        # First try exact match
        for tool in tools:
            if tool.name in tool_names_to_try:
                list_issues_tool = tool
                log(f"Found issues tool (exact match): {tool.name}", node="indexer", level="DEBUG")
                break
        
        # If no exact match, try partial
        if not list_issues_tool:
            for tool in tools:
                tool_name_lower = tool.name.lower()
                if any(name.lower() in tool_name_lower for name in tool_names_to_try):
                    list_issues_tool = tool
                    log(f"Found issues tool (partial match): {tool.name}", node="indexer", level="DEBUG")
                    break
        
        if not list_issues_tool:
            log(f"Could not find issues listing tool. Available tools: {[t.name for t in tools]}", node="indexer", level="WARNING")
            return []
        
        try:
            # Try different parameter formats
            try:
                issues_result = list_issues_tool.func(
                    owner="kubevirt",
                    repo="enhancements",
                    state="all"
                )
            except TypeError:
                issues_result = list_issues_tool.func(
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
                                
                                # Check if this is a VEP-related issue
                                is_vep_related = False
                                # Check labels
                                vep_label_patterns = ["kind/vep", "vep", "area/enhancement", "enhancement"]
                                if any(pattern.lower() in str(label).lower() for label in labels for pattern in vep_label_patterns):
                                    is_vep_related = True
                                # Check title/body for VEP references
                                if re.search(r'vep-?\d+', title, re.IGNORECASE) or re.search(r'vep-?\d+', body[:500], re.IGNORECASE):
                                    is_vep_related = True
                                
                                issues.append({
                                    "number": issue.get("number"),
                                    "title": issue.get("title"),
                                    "labels": labels,
                                    "state": issue.get("state"),
                                    "url": issue.get("url") or issue.get("html_url"),
                                    "created_at": issue.get("created_at"),
                                    "updated_at": issue.get("updated_at"),
                                    "is_vep_related": is_vep_related,
                                    "body_preview": body[:500] if body else "",
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
                    log(f"Could not parse issues string as JSON, returning raw data", node="indexer", level="DEBUG")
                return [{"raw_data": issues_result[:15000]}]
            elif isinstance(issues_result, list):
                issues = []
                for issue in issues_result:
                    if isinstance(issue, dict):
                        labels = [l.get("name") if isinstance(l, dict) else l for l in issue.get("labels", [])]
                        title = issue.get("title", "")
                        body = issue.get("body", "") or ""
                        
                        # Check if this is a VEP-related issue
                        is_vep_related = False
                        # Check labels
                        vep_label_patterns = ["kind/vep", "vep", "area/enhancement", "enhancement"]
                        if any(pattern.lower() in str(label).lower() for label in labels for pattern in vep_label_patterns):
                            is_vep_related = True
                        # Check title/body for VEP references
                        if re.search(r'vep-?\d+', title, re.IGNORECASE) or re.search(r'vep-?\d+', body[:500], re.IGNORECASE):
                            is_vep_related = True
                        
                        # Include all issues for now, but mark VEP-related ones
                        issues.append({
                            "number": issue.get("number"),
                            "title": issue.get("title"),
                            "labels": labels,
                            "state": issue.get("state"),
                            "url": issue.get("url") or issue.get("html_url"),
                            "created_at": issue.get("created_at"),
                            "updated_at": issue.get("updated_at"),
                            "is_vep_related": is_vep_related,
                            "body_preview": body[:500] if body else "",  # First 500 chars for VEP number detection
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


def index_kubevirt_prs(days_back: Optional[int] = 365) -> List[Dict[str, Any]]:
    """Index PRs in kubevirt/kubevirt repository.
    
    Args:
        days_back: Only include PRs from last N days (None = all PRs)
    
    Returns:
        List of PR summaries (number, title, state, labels) for context
    """
    log(f"Indexing PRs from kubevirt/kubevirt (days_back={days_back})", node="indexer")
    
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
        
        try:
            # Try different parameter formats
            try:
                prs_result = list_prs_tool.func(
                    owner="kubevirt",
                    repo="kubevirt",
                    state="all"
                )
            except TypeError:
                try:
                    prs_result = list_prs_tool.func(
                        repo="kubevirt/kubevirt",
                        state="all"
                    )
                except TypeError:
                    # Try with different parameter name
                    prs_result = list_prs_tool.func(
                        owner="kubevirt",
                        repo="kubevirt"
                    )
            
            # Parse result
            if isinstance(prs_result, str):
                # Check if it's an error message
                if len(prs_result) < 500 or prs_result.lower().startswith(("error", "failed", "cannot", "unable")):
                    log(f"Received error or suspiciously short response (length: {len(prs_result)}): {prs_result[:500]}", node="indexer", level="WARNING")
                    log(f"Available tools: {[t.name for t in tools]}", node="indexer", level="DEBUG")
                    return []
                log(f"Retrieved PRs data as string (length: {len(prs_result)})", node="indexer")
                return [{"raw_data": prs_result[:15000]}]
            elif isinstance(prs_result, list):
                prs = []
                for pr in prs_result:
                    if isinstance(pr, dict):
                        prs.append({
                            "number": pr.get("number"),
                            "title": pr.get("title"),
                            "labels": [l.get("name") if isinstance(l, dict) else l for l in pr.get("labels", [])],
                            "state": pr.get("state"),
                            "merged": pr.get("merged", False),
                            "url": pr.get("url") or pr.get("html_url"),
                            "created_at": pr.get("created_at"),
                            "updated_at": pr.get("updated_at"),
                            "body": (pr.get("body") or "")[:500],  # First 500 chars of body for VEP references
                        })
                
                # Filter by date if requested
                if days_back is not None:
                    original_count = len(prs)
                    prs = _filter_by_date(prs, days_back)
                    log(f"Filtered PRs: {original_count} -> {len(prs)} (last {days_back} days)", node="indexer")
                
                log(f"Indexed {len(prs)} PRs", node="indexer")
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
            # Get directory listing
            try:
                veps_content = get_file_tool.func(
                    owner="kubevirt",
                    repo="enhancements",
                    path="veps"
                )
            except TypeError:
                veps_content = get_file_tool.func(
                    path="kubevirt/enhancements/veps"
                )
            
            content_str = str(veps_content)
            # Check if it's an error message
            if len(content_str) < 500 or content_str.lower().startswith(("error", "failed", "cannot", "unable")):
                log(f"Received error or suspiciously short VEPs directory content (length: {len(content_str)}): {content_str[:500]}", node="indexer", level="WARNING")
                log(f"Available tools: {[t.name for t in tools]}", node="indexer", level="DEBUG")
                return []
            
            log(f"Retrieved VEPs directory content (length: {len(content_str)})", node="indexer")
            log(f"Directory content preview (first 1000 chars): {content_str[:1000]}", node="indexer", level="DEBUG")
            
            # Parse directory listing to extract VEP file names
            vep_files = []
            
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
                            # Handle both "vep-0176.md" and "veps/vep-0176.md" formats
                            if name:
                                # Extract just the filename if it's a path
                                basename = name.split("/")[-1] if "/" in name else name
                                # Only include .md files that match vep-*.md pattern
                                if file_type == "file" and re.match(r'vep-\d+\.md$', basename):
                                    vep_files.append(basename)
                        elif isinstance(item, str):
                            basename = item.split("/")[-1] if "/" in item else item
                            if re.match(r'vep-\d+\.md$', basename):
                                vep_files.append(basename)
                elif isinstance(listing_data, dict):
                    # Try common keys that might contain file list
                    for key in ["tree", "items", "contents", "files"]:
                        if key in listing_data and isinstance(listing_data[key], list):
                            for item in listing_data[key]:
                                if isinstance(item, dict):
                                    name = item.get("name") or item.get("path") or item.get("filename") or ""
                                    file_type = item.get("type", "")
                                    if name:
                                        basename = name.split("/")[-1] if "/" in name else name
                                        if file_type == "file" and re.match(r'vep-\d+\.md$', basename):
                                            vep_files.append(basename)
                                elif isinstance(item, str):
                                    basename = item.split("/")[-1] if "/" in item else item
                                    if re.match(r'vep-\d+\.md$', basename):
                                        vep_files.append(basename)
            except (json.JSONDecodeError, TypeError, AttributeError) as e:
                log(f"Could not parse directory listing as JSON, trying regex extraction: {e}", node="indexer", level="DEBUG")
            
            # Fallback: extract VEP file names using regex from string (handles paths too)
            if not vep_files:
                vep_pattern = r'vep-\d+\.md'
                matches = re.findall(vep_pattern, content_str)
                vep_files = list(set(matches))  # Remove duplicates
                log(f"Extracted {len(vep_files)} VEP files using regex fallback", node="indexer", level="DEBUG")
            
            # Sort VEP files numerically (vep-0176 > vep-0174)
            def vep_sort_key(name: str) -> int:
                match = re.search(r'vep-(\d+)', name)
                return int(match.group(1)) if match else 0
            
            vep_files = sorted(set(vep_files), key=vep_sort_key, reverse=True)
            
            log(f"Found {len(vep_files)} VEP files: {vep_files[:10]}{'...' if len(vep_files) > 10 else ''}", node="indexer")
            
            # Read each VEP file and include its content
            vep_data = []
            for vep_file in vep_files:
                try:
                    try:
                        vep_content = get_file_tool.func(
                            owner="kubevirt",
                            repo="enhancements",
                            path=f"veps/{vep_file}"
                        )
                    except TypeError:
                        vep_content = get_file_tool.func(
                            path=f"kubevirt/enhancements/veps/{vep_file}"
                        )
                    
                    content_str = str(vep_content)
                    if len(content_str) > 100 and not content_str.lower().startswith(("error", "failed", "cannot", "unable")):
                        vep_data.append({
                            "filename": vep_file,
                            "vep_number": re.search(r'vep-(\d+)', vep_file).group(0) if re.search(r'vep-(\d+)', vep_file) else vep_file,
                            "content": content_str[:50000] if len(content_str) > 50000 else content_str,  # Limit to 50k chars per file
                            "content_length": len(content_str),
                        })
                    else:
                        log(f"Skipping {vep_file} - suspicious content (length: {len(content_str)})", node="indexer", level="DEBUG")
                except Exception as e:
                    log(f"Error reading VEP file {vep_file}: {e}", node="indexer", level="DEBUG")
                    # Still include the filename even if we can't read it
                    vep_data.append({
                        "filename": vep_file,
                        "vep_number": re.search(r'vep-(\d+)', vep_file).group(0) if re.search(r'vep-(\d+)', vep_file) else vep_file,
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


def create_indexed_context(days_back: Optional[int] = 365) -> Dict[str, Any]:
    """Create a comprehensive indexed context for VEP discovery.
    
    This pre-fetches key information so the LLM has a complete picture
    of what exists before starting discovery.
    
    Args:
        days_back: Only include issues/PRs from last N days (None = all items)
                   Default 365 days to avoid overwhelming context
    
    Returns:
        Dict with indexed information:
        - release_info: Current release and schedule
        - enhancements_readme: README.md content with VEP process documentation
        - issues_index: List of issues in enhancements repo
        - prs_index: List of PRs in kubevirt repo
        - vep_files_index: List of VEP files in veps/ directory
    """
    log(f"Creating indexed context for VEP discovery (days_back={days_back})", node="indexer")
    
    indexed_context = {
        "release_info": index_release_schedule(),
        "enhancements_readme": index_enhancements_readme(),
        "issues_index": index_enhancements_issues(days_back=days_back),
        "prs_index": index_kubevirt_prs(days_back=days_back),
        "vep_files_index": index_vep_files(),
        "indexed_at": datetime.now().isoformat(),
        "days_back": days_back,
    }
    
    # Log summary
    release = indexed_context["release_info"]["current_release"] if indexed_context["release_info"] else "unknown"
    readme_available = "yes" if indexed_context["enhancements_readme"] else "no"
    issues_count = len(indexed_context["issues_index"])
    prs_count = len(indexed_context["prs_index"])
    vep_files_count = len(indexed_context["vep_files_index"])
    
    log(f"Indexed context created: release={release}, readme={readme_available}, issues={issues_count}, prs={prs_count}, vep_files={vep_files_count}", node="indexer")
    
    return indexed_context
