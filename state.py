from typing import TypedDict, Literal, Optional, List, Dict, Any, Annotated
from datetime import datetime

from langchain_core.messages import BaseMessage
from pydantic import BaseModel, Field
from langgraph.graph.message import add_messages


def merge_dict_reducer(x: Dict[str, Any], y: Dict[str, Any]) -> Dict[str, Any]:
    """Reducer function to merge two dictionaries."""
    if not x:
        return y or {}
    if not y:
        return x or {}
    return {**x, **y}


def concat_list_reducer(x: List[Any], y: List[Any]) -> List[Any]:
    """Reducer function to concatenate two lists."""
    if not x:
        return y or []
    if not y:
        return x or []
    return x + y

class ReleaseScheduleDelay(BaseModel):
    """Represents a delay in the release schedule for a specific milestone."""
    topic: Literal['enhancement_freeze', 'code_freeze', 'kubevirt_release']
    original_date: datetime
    delay_date: datetime
    is_current_release: bool
    reasons: List[str]
    notes: str

class ReleaseSchedule(BaseModel):
    """Parsed release schedule from kubevirt/sig-release.
    
    Contains all key dates for a release cycle including Enhancement Freeze (EF),
    Code Freeze (CF), and General Availability (GA) dates.
    """
    version: str  # e.g., "v1.8"
    enhancement_freeze: datetime  # EF - deadline for VEP acceptance
    code_freeze: datetime  # CF - deadline for code implementation
    kubevirt_release: datetime  # GA - general availability date
    freeze_delays: List[ReleaseScheduleDelay]  # Any delays to the original schedule

class VEPMilestone(BaseModel):
    """VEP milestone tracking information for a specific release.
    
    Tracks the VEP's status in the release cycle, promotion phase, exception status,
    and whether all code PRs have been merged.
    """
    version: str  # Release version this milestone is for (e.g., "v1.8")
    status: Literal['Proposed for consideration', 'Tracked', 'Unchanged', 'Removed from Milestone', 'Exception Required', 'At risk', 'Complete']
    promotion_phase: Literal['Net New','Remaining','Graduating','Deprecation']
    exception_phase: Literal['Accepted', 'Pending', 'Rejected', 'Completed', 'None']
    target_stage: Literal['Alpha', 'Beta', 'Stable', 'Deprecation/Removal']
    all_code_prs_merged: bool  # Whether all implementation PRs are merged

class VEPCompliance(BaseModel):
    """Compliance check results for a VEP.
    
    All checks must pass for a VEP to be considered compliant with the process.
    These flags are checked by the compliance monitoring node.
    """
    template_complete: bool  # VEP document follows template structure
    all_sigs_signed_off: bool  # All 3 SIGs (compute, network, storage) have LGTM
    vep_merged: bool  # VEP PR is merged in kubevirt/enhancements
    prs_linked: bool  # Related PRs are linked in tracking issue
    docs_pr_created: bool  # Documentation PR exists
    labels_valid: bool  # Has required SIG and target release labels

class VEPActivity(BaseModel):
    """Activity metrics for monitoring VEP progress.
    
    Used to detect inactive VEPs and review lag times.
    """
    last_activity: datetime  # Last update time (from issue/PR updates)
    days_since_update: int  # Days since last activity
    review_lag_days: Optional[int] = None  # Days since last review (None if no reviews)

class PRInfo(BaseModel):
    """Structured PR information with core fields we monitor.
    
    Contains essential PR data for compliance checking and activity monitoring.
    The full GitHub API response is preserved in github_data for reference.
    """
    number: int
    title: str
    url: str
    state: str  # "open", "closed", "merged"
    created_at: datetime
    updated_at: datetime
    author: str
    merged: bool = False
    merged_at: Optional[datetime] = None

    # Review status - these are what we actually check for compliance
    has_lgtm: bool = False  # Has "LGTM" comment from approver
    has_approve: bool = False  # Has GitHub approval
    on_hold: bool = False  # Marked as on hold
    review_count: int = 0  # Number of reviews

    # Full GitHub API response stored here for reference
    # Allows access to any field not explicitly defined above
    github_data: Dict[str, Any] = Field(default_factory=dict)

    class Config:
        extra = "allow"  # Allow extra fields from GitHub API that aren't defined above

class IssueInfo(BaseModel):
    """Structured issue information with core fields we monitor.
    
    Used for tracking issues, especially the main VEP tracking issue.
    Full GitHub API response preserved in github_data for reference.
    """
    number: int
    title: str
    url: str
    state: str  # "open", "closed"
    created_at: datetime
    updated_at: datetime
    author: str
    labels: List[str] = []  # GitHub labels (SIG labels, target release, etc.)

    # Full GitHub API response stored here for reference
    github_data: Dict[str, Any] = Field(default_factory=dict)

    class Config:
        extra = "allow"  # Allow extra fields from GitHub API

class VEPInfo(BaseModel):
    """Core VEP data structure with all tracking information.
    
    This represents a single Virtualization Enhancement Proposal (VEP) with all
    its metadata, compliance status, activity metrics, and related PRs/issues.
    Used throughout the agent for monitoring, compliance checking, and notifications.
    """
    # Required core fields
    tracking_issue_id: int  # GitHub issue number that tracks this VEP
    name: str  # VEP identifier (e.g., "vep-1234")
    title: str  # VEP title
    owner: str  # GitHub username of VEP owner
    owning_sig: str  # Primary SIG: "compute", "network", or "storage"
    status: str  # Current status from tracking issue
    last_updated: datetime  # Last update timestamp
    created_at: datetime  # Creation timestamp

    # Structured nested models - validated and type-safe
    current_milestone: VEPMilestone  # Current release milestone status
    compliance: VEPCompliance  # Compliance check results
    activity: VEPActivity  # Activity metrics for monitoring

    # Related GitHub resources
    tracking_issue: IssueInfo | None  # Full tracking issue data from GitHub
    enhancement_prs: List[PRInfo] = []  # PRs in kubevirt/enhancements repo (VEP creation/updates)
    implementation_prs: List[PRInfo] = []  # PRs in kubevirt/kubevirt repo (actual code implementation)

    # Flexible extensions - structure varies, so Dict allows flexibility
    target_release: Optional[str] = None  # Target release version (e.g., "v1.8")
    exceptions: Dict[str, Any] = {}  # Exception requests and status - structure varies by VEP
    analysis: Dict[str, Any] = {}  # LLM-generated insights and analysis - flexible structure
    notes: Optional[str] = None  # Free-form notes for human review

class VEPState(TypedDict):
    """LangGraph state schema - the entire state of the VEP governance agent.
    
    This is the top-level state that flows through the LangGraph state machine.
    All nodes read from and write to this state. TypedDict is used (not Pydantic)
    for performance - state updates are frequent and TypedDict is lightweight.
    """
    # Required by LangGraph for LLM interactions
    messages: List[BaseMessage]

    # Release tracking
    current_release: Optional[str]  # Active release version (e.g., "v1.8")
    release_schedule: Optional[ReleaseSchedule]  # Parsed schedule with EF/CF dates

    # VEP data
    veps: List[VEPInfo]  # All VEPs being tracked

    # Task scheduling and tracking
    last_check_times: Annotated[Dict[str, datetime], merge_dict_reducer]  # Last execution time per node/task (merged from parallel nodes)
    next_tasks: List[str]  # Tasks the scheduler should run next

    # State management
    alerts: Annotated[List[Dict[str, Any]], concat_list_reducer]  # Alerts queued for notification (deadline warnings, compliance issues, etc.) - concatenated from parallel nodes
    alert_summary_text: Optional[str]  # Human-readable summary text for email alerts
    sheets_need_update: bool  # Flag indicating Google Sheets needs syncing
    errors: List[Dict[str, Any]]  # Errors encountered during processing
    config_cache: Dict[str, Any]  # Cached configuration (VEP template, process docs, etc.)
    vep_updates_by_check: Annotated[Dict[str, List[VEPInfo]], merge_dict_reducer]  # Temporary storage for VEP updates from parallel checks (merged from parallel nodes)
    
    # Google Sheets configuration
    sheet_config: Dict[str, Any]  # Sheet configuration: sheet_id, create_new, sheet_name, etc.
    
    # Configuration flags
    index_cache_minutes: int  # Maximum age of index cache in minutes
    one_cycle: bool  # Flag to indicate if the agent should run only one cycle
    _exit_after_sheets: Optional[bool]  # Internal flag to signal exit after sheets update
    skip_monitoring: bool  # Flag to skip monitoring checks for faster debugging
    skip_sheets: bool  # Flag to skip sheet updates for faster debugging
    skip_send_email: bool  # Flag to skip sending email alerts
    mock_veps: bool  # Flag to use mock VEPs instead of fetching from GitHub
    mock_analyzed_combined: bool  # Flag to skip LLM in analyze_combined node
    mock_alert_summary: bool  # Flag to skip LLM in alert_summary node