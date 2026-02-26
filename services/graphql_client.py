"""GitHub GraphQL client for Project V2 board queries.

Provides functions to query kubevirt GitHub Project V2 boards and extract
VEP metadata including status, priority, dates, and other custom fields.

Inspired by vladikr/vepMonitoring project.
"""

import os
import requests
from typing import Dict, List, Any, Optional
from services.utils import log

# GitHub GraphQL API endpoint
GRAPHQL_API_URL = "https://api.github.com/graphql"

# Hardcoded repository names
ENHANCEMENTS_REPO = "kubevirt/enhancements"

# GraphQL query to list all projects for an organization
_LIST_PROJECTS_QUERY = """
query($orgName: String!, $cursor: String) {
  organization(login: $orgName) {
    projectsV2(first: 20, after: $cursor) {
      nodes {
        id
        number
        title
      }
      pageInfo {
        hasNextPage
        endCursor
      }
    }
  }
}
"""

# GraphQL query to fetch all items from a Project V2 board with ALL field types
_PROJECT_BOARD_QUERY = """
query($orgName: String!, $projectNumber: Int!, $cursor: String) {
  organization(login: $orgName) {
    projectV2(number: $projectNumber) {
      title
      items(first: 100, after: $cursor) {
        nodes {
          id
          content {
            __typename
            ... on Issue {
              number
              title
              url
              state
              body
              repository {
                nameWithOwner
              }
            }
            ... on PullRequest {
              number
              title
              url
              state
              body
              repository {
                nameWithOwner
              }
            }
          }
          fieldValues(first: 20) {
            nodes {
              __typename
              ... on ProjectV2ItemFieldSingleSelectValue {
                name
                field {
                  ... on ProjectV2SingleSelectField {
                    name
                  }
                }
              }
              ... on ProjectV2ItemFieldTextValue {
                text
                field {
                  ... on ProjectV2Field {
                    name
                  }
                }
              }
              ... on ProjectV2ItemFieldDateValue {
                date
                field {
                  ... on ProjectV2Field {
                    name
                  }
                }
              }
              ... on ProjectV2ItemFieldNumberValue {
                number
                field {
                  ... on ProjectV2Field {
                    name
                  }
                }
              }
              ... on ProjectV2ItemFieldIterationValue {
                title
                startDate
                duration
                field {
                  ... on ProjectV2IterationField {
                    name
                  }
                }
              }
              ... on ProjectV2ItemFieldLabelValue {
                labels(first: 10) {
                  nodes {
                    name
                  }
                }
                field {
                  ... on ProjectV2Field {
                    name
                  }
                }
              }
              ... on ProjectV2ItemFieldMilestoneValue {
                milestone {
                  title
                  dueOn
                }
                field {
                  ... on ProjectV2Field {
                    name
                  }
                }
              }
              ... on ProjectV2ItemFieldRepositoryValue {
                repository {
                  nameWithOwner
                }
                field {
                  ... on ProjectV2Field {
                    name
                  }
                }
              }
            }
          }
        }
        pageInfo {
          hasNextPage
          endCursor
        }
      }
    }
  }
}
"""


def find_project_by_title(
    org_name: str = "kubevirt",
    title_pattern: str = "",
) -> Optional[int]:
    """Find a GitHub Project V2 by title pattern.

    Searches for projects in the organization that match the given title pattern.
    Pattern matching is case-insensitive and checks if all pattern words appear in title.

    Args:
        org_name: GitHub organization name (default: "kubevirt")
        title_pattern: Pattern to match in project title (e.g., "v1.8 release tracking")

    Returns:
        Project number if found, None otherwise
    """
    if not title_pattern:
        return None

    log(f"Searching for project matching pattern: '{title_pattern}'", node="graphql")

    pattern_lower = title_pattern.lower()
    pattern_words = pattern_lower.split()

    variables = {
        "orgName": org_name,
        "cursor": None,
    }
    has_next_page = True

    while has_next_page:
        try:
            result = execute_graphql_query(_LIST_PROJECTS_QUERY, variables)
        except Exception as e:
            log(f"GraphQL query failed while searching projects: {e}", node="graphql", level="ERROR")
            return None

        if "errors" in result:
            log(f"GraphQL errors while searching projects: {result['errors']}", node="graphql", level="ERROR")
            return None

        data = (
            result.get("data", {})
            .get("organization", {})
            .get("projectsV2", {})
        )

        for project in data.get("nodes", []):
            title = project.get("title", "")
            title_lower = title.lower()

            # Check if all pattern words appear in the title
            if all(word in title_lower for word in pattern_words):
                project_num = project.get("number")
                log(f"Found matching project: '{title}' (number: {project_num})", node="graphql")
                return project_num

        page_info = data.get("pageInfo", {})
        has_next_page = page_info.get("hasNextPage", False)
        variables["cursor"] = page_info.get("endCursor")

    log(f"No project found matching pattern: '{title_pattern}'", node="graphql", level="WARNING")
    return None


def execute_graphql_query(query: str, variables: Dict[str, Any]) -> Dict[str, Any]:
    """Execute a GraphQL query against GitHub API.

    Args:
        query: GraphQL query string
        variables: Variables to pass to the query

    Returns:
        Parsed JSON response

    Raises:
        Exception: If request fails or returns errors
    """
    github_token = os.environ.get("GITHUB_TOKEN")
    if not github_token:
        raise Exception("GITHUB_TOKEN environment variable not set")

    headers = {
        "Authorization": f"Bearer {github_token}",
        "Content-Type": "application/json",
    }

    response = requests.post(
        GRAPHQL_API_URL,
        json={"query": query, "variables": variables},
        headers=headers,
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


def _extract_field_value(field_node: Dict[str, Any]) -> tuple[Optional[str], Any]:
    """Extract field name and value from a fieldValues node.

    Args:
        field_node: A node from fieldValues.nodes

    Returns:
        Tuple of (field_name, field_value) or (None, None) if cannot extract
    """
    typename = field_node.get("__typename", "")

    if typename == "ProjectV2ItemFieldSingleSelectValue":
        field_name = field_node.get("field", {}).get("name")
        value = field_node.get("name")
        return (field_name, value)

    elif typename == "ProjectV2ItemFieldTextValue":
        field_name = field_node.get("field", {}).get("name")
        value = field_node.get("text")
        return (field_name, value)

    elif typename == "ProjectV2ItemFieldDateValue":
        field_name = field_node.get("field", {}).get("name")
        value = field_node.get("date")
        return (field_name, value)

    elif typename == "ProjectV2ItemFieldNumberValue":
        field_name = field_node.get("field", {}).get("name")
        value = field_node.get("number")
        return (field_name, value)

    elif typename == "ProjectV2ItemFieldIterationValue":
        field_name = field_node.get("field", {}).get("name")
        value = {
            "title": field_node.get("title"),
            "startDate": field_node.get("startDate"),
            "duration": field_node.get("duration"),
        }
        return (field_name, value)

    elif typename == "ProjectV2ItemFieldLabelValue":
        field_name = field_node.get("field", {}).get("name")
        labels_nodes = field_node.get("labels", {}).get("nodes", [])
        value = [l.get("name") for l in labels_nodes if l.get("name")]
        return (field_name, value)

    elif typename == "ProjectV2ItemFieldMilestoneValue":
        field_name = field_node.get("field", {}).get("name")
        milestone = field_node.get("milestone", {})
        value = {
            "title": milestone.get("title"),
            "dueOn": milestone.get("dueOn"),
        }
        return (field_name, value)

    elif typename == "ProjectV2ItemFieldRepositoryValue":
        field_name = field_node.get("field", {}).get("name")
        repo = field_node.get("repository", {})
        value = repo.get("nameWithOwner")
        return (field_name, value)

    return (None, None)


def get_project_board_items(
    project_number: int,
    org_name: str = "kubevirt",
    filter_repo: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Fetch all items from a GitHub Project V2 board with all field metadata.

    Args:
        project_number: GitHub Project V2 number (e.g., 19)
        org_name: GitHub organization name (default: "kubevirt")
        filter_repo: Optional repository to filter by (e.g., "kubevirt/enhancements")

    Returns:
        List of items, each with:
        - number: Issue/PR number
        - title: Issue/PR title
        - url: GitHub URL
        - state: Issue/PR state
        - content_type: "Issue" or "PullRequest"
        - repository: Repository name (e.g., "kubevirt/enhancements")
        - fields: Dict of field_name -> field_value for all project fields
    """
    log(f"Fetching project board #{project_number} from {org_name}", node="graphql")

    items = []
    variables = {
        "orgName": org_name,
        "projectNumber": project_number,
        "cursor": None,
    }
    has_next_page = True

    while has_next_page:
        try:
            result = execute_graphql_query(_PROJECT_BOARD_QUERY, variables)
        except Exception as e:
            log(f"GraphQL query failed: {e}", node="graphql", level="ERROR")
            return items

        if "errors" in result:
            log(f"GraphQL errors: {result['errors']}", node="graphql", level="ERROR")
            return items

        data = (
            result.get("data", {})
            .get("organization", {})
            .get("projectV2", {})
            .get("items", {})
        )

        for item in data.get("nodes", []):
            content = item.get("content")
            if not content:
                continue

            content_type = content.get("__typename")
            if content_type not in ("Issue", "PullRequest"):
                continue

            repo = content.get("repository", {}).get("nameWithOwner", "")

            # Filter by repository if specified
            if filter_repo and repo != filter_repo:
                continue

            # Extract all field values
            fields = {}
            for field_node in item.get("fieldValues", {}).get("nodes", []):
                field_name, field_value = _extract_field_value(field_node)
                if field_name and field_value is not None:
                    fields[field_name] = field_value

            items.append({
                "number": content.get("number"),
                "title": content.get("title"),
                "url": content.get("url"),
                "state": content.get("state"),
                "body": content.get("body"),
                "content_type": content_type,
                "repository": repo,
                "fields": fields,
            })

        page_info = data.get("pageInfo", {})
        has_next_page = page_info.get("hasNextPage", False)
        variables["cursor"] = page_info.get("endCursor")

    log(f"Fetched {len(items)} items from project board #{project_number}", node="graphql")
    return items


def get_veps_from_project_board(
    project_number: int,
    org_name: str = "kubevirt",
) -> Dict[int, Dict[str, Any]]:
    """Fetch VEPs from project board, returning a mapping by issue number.

    Filters to only include issues from kubevirt/enhancements repository.

    Args:
        project_number: GitHub Project V2 number
        org_name: GitHub organization name (default: "kubevirt")

    Returns:
        Dict mapping issue_number -> {title, url, state, fields: {...}}
    """
    items = get_project_board_items(
        project_number=project_number,
        org_name=org_name,
        filter_repo=ENHANCEMENTS_REPO,
    )

    veps = {}
    for item in items:
        if item.get("content_type") == "Issue":
            issue_number = item.get("number")
            if issue_number:
                veps[issue_number] = {
                    "title": item.get("title"),
                    "url": item.get("url"),
                    "state": item.get("state"),
                    "body": item.get("body"),
                    "fields": item.get("fields", {}),
                }

    log(f"Found {len(veps)} VEPs on project board #{project_number}", node="graphql")
    return veps
