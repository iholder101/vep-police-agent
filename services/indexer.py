"""Indexing service to pre-fetch key information for VEP discovery.

This module provides functions to index critical information before LLM processing,
ensuring the LLM has a complete picture of what exists rather than having to discover it.
"""

import re
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
        
        # Find file reading tool
        get_file_tool = None
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
        
        # First, try to get the releases directory listing
        # GitHub API might return directory contents as JSON or we might need to parse HTML
        try:
            releases_dir_content = get_file_tool.func(
                owner="kubevirt",
                repo="sig-release",
                path="releases"
            )
            
            # Try to extract version directories from the content
            # This could be JSON, HTML, or markdown listing
            content_str = str(releases_dir_content)
            
            # Extract version patterns (v1.8, v1.9, v1.10, v1.11, etc.)
            version_pattern = r'v\d+\.\d+'
            found_versions = list(set(re.findall(version_pattern, content_str)))
            
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
                
        except Exception as e:
            log(f"Error reading releases directory: {e}", node="indexer", level="WARNING")
        
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
        
        # Find list_issues tool
        list_issues_tool = None
        tool_names_to_try = [
            "list_issues",
            "search_issues",
            "get_issues",
            "mcp_GitHub_list_issues",
            "mcp_GitHub_search_issues",
        ]
        
        for tool in tools:
            if any(name.lower() in tool.name.lower() for name in tool_names_to_try):
                list_issues_tool = tool
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
                log(f"Retrieved issues data as string (length: {len(issues_result)})", node="indexer")
                return [{"raw_data": issues_result[:15000]}]
            elif isinstance(issues_result, list):
                issues = []
                for issue in issues_result:
                    if isinstance(issue, dict):
                        issues.append({
                            "number": issue.get("number"),
                            "title": issue.get("title"),
                            "labels": [l.get("name") if isinstance(l, dict) else l for l in issue.get("labels", [])],
                            "state": issue.get("state"),
                            "url": issue.get("url") or issue.get("html_url"),
                            "created_at": issue.get("created_at"),
                            "updated_at": issue.get("updated_at"),
                        })
                
                # Filter by date if requested
                if days_back is not None:
                    original_count = len(issues)
                    issues = _filter_by_date(issues, days_back)
                    log(f"Filtered issues: {original_count} -> {len(issues)} (last {days_back} days)", node="indexer")
                
                log(f"Indexed {len(issues)} issues", node="indexer")
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
        
        # Find list_pull_requests tool
        list_prs_tool = None
        tool_names_to_try = [
            "list_pull_requests",
            "list_pulls",
            "search_pull_requests",
            "mcp_GitHub_list_pull_requests",
        ]
        
        for tool in tools:
            if any(name.lower() in tool.name.lower() and ("pull" in tool.name.lower() or "pr" in tool.name.lower()) for name in tool_names_to_try):
                list_prs_tool = tool
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
        
        # Find file reading tool
        get_file_tool = None
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
            
            if readme_content and len(str(readme_content)) > 100:
                content_str = str(readme_content)
                log(f"Retrieved README.md (length: {len(content_str)})", node="indexer")
                
                # Truncate if too long, but keep more than other files since it's critical
                return {
                    "content": content_str[:20000] if len(content_str) > 20000 else content_str,
                    "full_length": len(content_str),
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
    
    Returns:
        List of VEP file names and basic info
    """
    log("Indexing VEP files from kubevirt/enhancements/veps/", node="indexer")
    
    try:
        tools = get_mcp_tools_by_name("github")
        
        # Find file reading tool
        get_file_tool = None
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
            return []
        
        try:
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
            log(f"Retrieved VEPs directory content (length: {len(content_str)})", node="indexer")
            
            return [{
                "directory_content": content_str[:10000] if len(content_str) > 10000 else content_str,
                "note": "This contains the directory listing. Extract all vep-*.md file names from it."
            }]
            
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
