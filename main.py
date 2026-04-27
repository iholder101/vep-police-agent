#!/usr/bin/env python3
"""Main entry point for VEP governance agent."""

import argparse
import json
import os
import signal
import sys
from pathlib import Path
from typing import Optional, Dict, Any
from langchain_core.messages import HumanMessage
from graph import create_graph
from services.utils import log
from state import VEPInfo, ReleaseSchedule
from nodes.state_history import clear_all_history
from nodes.escalation import clear_persistence

# State cache file location
STATE_CACHE_FILE = Path(__file__).parent / "cache" / "state_cache.json"

from services.shutdown import is_shutdown_requested, request_shutdown


def load_state_cache() -> Optional[Dict[str, Any]]:
    """Load cached state from previous run.

    Returns:
        Dict with cached state fields if cache exists and is valid, None otherwise
    """
    if not STATE_CACHE_FILE.exists():
        log("State cache file not found", node="main")
        return None

    try:
        with open(STATE_CACHE_FILE, "r") as f:
            cached = json.load(f)

        # Validate cache version
        if cached.get("version") != "1.0":
            log(f"State cache version mismatch: {cached.get('version')}", node="main", level="WARNING")
            return None

        # Deserialize VEPs
        veps = []
        for vep_data in cached.get("veps", []):
            try:
                veps.append(VEPInfo(**vep_data))
            except Exception as e:
                log(f"Failed to deserialize VEP: {e}", node="main", level="WARNING")

        # Deserialize release schedule
        release_schedule = None
        if cached.get("release_schedule"):
            try:
                release_schedule = ReleaseSchedule(**cached["release_schedule"])
            except Exception as e:
                log(f"Failed to deserialize release schedule: {e}", node="main", level="WARNING")

        log(f"Loaded state cache: {len(veps)} VEPs from {cached.get('timestamp', 'unknown')}", node="main")

        return {
            "veps": veps,
            "release_schedule": release_schedule,
            "current_release": cached.get("current_release"),
            "general_insights": cached.get("general_insights", []),
            "alerts": cached.get("alerts", []),
        }

    except Exception as e:
        log(f"Failed to load state cache: {e}", node="main", level="ERROR")
        return None


def get_initial_state(sheet_id: Optional[str] = None, index_cache_minutes: int = 60, one_cycle: bool = False, skip_monitoring: bool = False, skip_sheets: bool = False, skip_update_board: bool = False, skip_send_email: bool = False, skip_send_slack: bool = False, mock_veps: bool = False, mock_analyzed_combined: bool = False, mock_alert_summary: bool = False, immediate_start: bool = False, use_state_cache: bool = False):
    """Create initial state for the agent."""
    sheet_config = {
        "sheet_name": "VEP Status",  # Optional: name for the sheet/tab
    }
    
    if sheet_id:
        sheet_config["sheet_id"] = sheet_id
        sheet_config["create_new"] = False  # Use existing sheet
        log(f"Using existing Google Sheet: {sheet_id}", node="main")
    else:
        sheet_config["create_new"] = True  # Will create a new sheet on first run
        # sheet_id will be set by update_sheets node after creation
    
    return {
        "messages": [HumanMessage(content="Initialize VEP governance agent")],
        "current_release": None,
        "release_schedule": None,
        "veps": [],
        "last_check_times": {},
        "next_tasks": [],
        "alerts": [],
        "alert_summary_text": None,
        "vep_summary_table": [],
        "general_insights": [],
        "sheets_need_update": False,
        "errors": [],
        "config_cache": {},
        "vep_updates_by_check": {},
        "sheet_config": sheet_config,
        "index_cache_minutes": index_cache_minutes,  # Store cache timeout in state
        "one_cycle": one_cycle,  # Flag to exit after one cycle
        "skip_monitoring": skip_monitoring,  # Flag to skip monitoring checks
        "skip_sheets": skip_sheets,  # Flag to skip sheet updates
        "skip_update_board": skip_update_board,  # Flag to skip writing to GitHub project board
        "skip_send_email": skip_send_email,  # Flag to skip sending email alerts
        "skip_send_slack": skip_send_slack,  # Flag to skip sending Slack alerts
        "mock_veps": mock_veps,  # Flag to use mock VEPs instead of fetching from GitHub
        "mock_analyzed_combined": mock_analyzed_combined,  # Flag to skip LLM in analyze_combined
        "mock_alert_summary": mock_alert_summary,  # Flag to skip LLM in alert_summary
        "immediate_start": immediate_start,  # Flag to start immediately without waiting for round hour
        "use_state_cache": use_state_cache,  # Flag to use cached state on first cycle
        "_state_cache_used": False,  # Internal flag tracking if cache was used
        "_force_sheets_update": False,  # Internal flag to force sheets update on first cache cycle
    }


def log_startup_flags(args, index_cache_minutes: int) -> None:
    """Log all startup configuration flags (excluding sensitive credentials).
    
    Args:
        args: Parsed command line arguments
        index_cache_minutes: Calculated index cache timeout in minutes
    """
    import os
    
    log("Starting VEP governance agent", node="main")
    log("Configuration flags:", node="main")
    
    flags = []
    
    # Credential flags (show file paths, not content)
    if args.api_key:
        if os.path.exists(args.api_key):
            flags.append(f"  --api-key: {args.api_key} (file)")
        else:
            flags.append("  --api-key: <provided>")
    if args.google_token:
        if os.path.exists(args.google_token):
            flags.append(f"  --google-token: {args.google_token} (file)")
        else:
            flags.append("  --google-token: <provided>")
    if args.github_token:
        if os.path.exists(args.github_token):
            flags.append(f"  --github-token: {args.github_token} (file)")
        else:
            flags.append("  --github-token: <provided>")
    if args.resend_api_key:
        if os.path.exists(args.resend_api_key):
            flags.append(f"  --resend-api-key: {args.resend_api_key} (file)")
        else:
            flags.append("  --resend-api-key: <provided>")
    if args.slack_webhook_url:
        if os.path.exists(args.slack_webhook_url):
            flags.append(f"  --slack-webhook-url: {args.slack_webhook_url} (file)")
        else:
            flags.append("  --slack-webhook-url: <provided>")
    
    # Configuration flags
    if args.sheet_id:
        flags.append(f"  --sheet-id: {args.sheet_id}")
    if args.debug:
        flags.append(f"  --debug: {args.debug}")
    if args.one_cycle:
        flags.append("  --one-cycle: enabled")
    if args.fastest_model:
        flags.append("  --fastest-model: enabled")
    if args.no_index_cache:
        flags.append("  --no-index-cache: enabled")
    elif index_cache_minutes != 60:
        flags.append(f"  --index-cache-minutes: {index_cache_minutes}")
    if args.skip_monitoring:
        flags.append("  --skip-monitoring: enabled")
    if args.skip_sheets:
        flags.append("  --skip-sheets: enabled")
    if args.skip_update_board:
        flags.append("  --skip-update-board: enabled")
    if args.skip_send_email:
        flags.append("  --skip-send-email: enabled")
    if args.skip_send_slack:
        flags.append("  --skip-send-slack: enabled")
    if args.mock_veps:
        flags.append("  --mock-veps: enabled")
    if args.mock_analyzed_combined:
        flags.append("  --mock-analyzed-combined: enabled")
    if args.mock_alert_summary:
        flags.append("  --mock-alert-summary: enabled")
    if args.immediate_start:
        flags.append("  --immediate-start: enabled")
    if args.use_state_cache:
        flags.append("  --use-state-cache: enabled")

    # Log all flags
    if flags:
        for flag in flags:
            log(flag, node="main")
    else:
        log("  (using defaults)", node="main")
    
    log("Press Ctrl+C to exit gracefully", node="main")
    
    # Log mode descriptions
    if args.one_cycle:
        log("One-cycle mode: will exit after sheet update completes", node="main")
    if args.skip_monitoring:
        log("Skip-monitoring mode: monitoring checks (deadlines, activity, compliance, exceptions) will be skipped", node="main")
    if args.skip_sheets:
        log("Skip-sheets mode: Google Sheets updates will be skipped", node="main")
    if args.skip_update_board:
        log("Skip-update-board mode: GitHub project board updates will be skipped", node="main")
    if args.mock_veps:
        log("Mock VEPs mode: will use mock VEPs instead of fetching from GitHub", node="main")
    if args.mock_analyzed_combined:
        log("Mock analyzed-combined mode: will skip LLM call and use naive analysis", node="main")
    if args.mock_alert_summary:
        log("Mock alert-summary mode: will skip LLM call and create mocked alerts", node="main")
    if args.immediate_start:
        log("Immediate-start mode: will run first cycle immediately and use current time + interval instead of round hours", node="main")
    if args.use_state_cache:
        log("Use-state-cache mode: first cycle will use cached state (skipping fetch/analyze), subsequent cycles run normally", node="main")


def parse_args():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="VEP governance agent - monitors and manages VEP status"
    )
    parser.add_argument(
        "--api-key",
        type=str,
        help="API key for Gemini LLM (or set API_KEY environment variable)"
    )
    parser.add_argument(
        "--google-token",
        type=str,
        help="Google service account JSON token (file path or JSON string). Can also set GOOGLE_TOKEN environment variable."
    )
    parser.add_argument(
        "--github-token",
        type=str,
        help="GitHub token for API access (or set GITHUB_TOKEN environment variable)"
    )
    parser.add_argument(
        "--resend-api-key",
        type=str,
        help="Resend API key for email sending (or set RESEND_API_KEY environment variable)"
    )
    parser.add_argument(
        "--slack-webhook-url",
        type=str,
        default="SLACK_WEBHOOK_URL",
        help="Path to file containing Slack Incoming Webhook URL (default: SLACK_WEBHOOK_URL)"
    )
    parser.add_argument(
        "--debug",
        type=str,
        choices=["discover-veps", "test-sheets"],
        help="Enable debug mode. Options: 'discover-veps' - print indexed VEP data and exit; 'test-sheets' - test Google Sheets with limited LLM iterations"
    )
    parser.add_argument(
        "--sheet-id",
        type=str,
        help="Google Sheets document ID to use (from URL: https://docs.google.com/spreadsheets/d/SHEET_ID/edit). If not provided, will try to create a new sheet."
    )
    parser.add_argument(
        "--index-cache-minutes",
        type=int,
        default=60,
        help="Maximum age of index cache in minutes before regenerating (default: 60). Set to 0 to disable caching."
    )
    parser.add_argument(
        "--no-index-cache",
        action="store_true",
        help="Disable index caching (equivalent to --index-cache-minutes=0)"
    )
    parser.add_argument(
        "--one-cycle",
        action="store_true",
        help="Run one cycle and exit after sheet update completes"
    )
    parser.add_argument(
        "--fastest-model",
        action="store_true",
        help="Force all nodes to use FAST_MODEL (fastest model) regardless of node configuration"
    )
    parser.add_argument(
        "--skip-monitoring",
        action="store_true",
        help="Skip all monitoring checks (deadlines, activity, compliance, exceptions). Useful for debugging VEP discovery and sheet updates. Goes straight from fetch_veps to update_sheets."
    )
    parser.add_argument(
        "--skip-sheets",
        action="store_true",
        help="Skip Google Sheets updates. Useful for debugging email alerts. When combined with --skip-monitoring, focuses on email notification only."
    )
    parser.add_argument(
        "--skip-update-board",
        action="store_true",
        help="Skip writing summary data back to the GitHub project board. Useful when testing without project write scope."
    )
    parser.add_argument(
        "--skip-send-email",
        action="store_true",
        help="Skip sending email alerts. Useful for debugging without sending emails."
    )
    parser.add_argument(
        "--skip-send-slack",
        action="store_true",
        help="Skip sending Slack alerts. Useful for debugging without sending Slack messages."
    )
    parser.add_argument(
        "--mock-veps",
        action="store_true",
        help="Use mock VEPs instead of fetching from GitHub. Skips VEP discovery entirely and creates sample VEPs for testing. Useful for testing sheets and alerts without API calls."
    )
    parser.add_argument(
        "--mock-analyzed-combined",
        action="store_true",
        help="Skip LLM call in analyze_combined node and use naive analysis instead. Useful for faster testing without LLM costs."
    )
    parser.add_argument(
        "--mock-alert-summary",
        action="store_true",
        help="Skip LLM call in alert_summary node and create mocked alerts instead. Useful for faster testing without LLM costs."
    )
    parser.add_argument(
        "--immediate-start",
        action="store_true",
        help="Run the first cycle immediately without waiting for round hour. Subsequent cycles will use current time + interval instead of round hours."
    )
    parser.add_argument(
        "--use-state-cache",
        action="store_true",
        help="Use cached state from previous run on first cycle (skips fetch/analyze). Cache is created after each full analysis run. Useful for fast debug/test cycles."
    )
    parser.add_argument(
        "--clear-history",
        action="store_true",
        help="Clear all history and caches (state_cache.json, index_cache.json, history snapshots, alert persistence) and exit. Useful for fresh starts."
    )
    return parser.parse_args()


def signal_handler(signum, frame):
    """Handle SIGINT/SIGTERM gracefully."""
    if is_shutdown_requested():
        log("\nForce exit requested. Terminating...", node="main", level="WARNING")
        sys.exit(130)
    else:
        request_shutdown()
        sig_name = "SIGTERM" if signum == signal.SIGTERM else "SIGINT"
        log(f"\nShutdown requested ({sig_name}). Finishing current operation...", node="main", level="INFO")


def setup_credentials(args):
    """Set up credentials from CLI arguments as environment variables."""
    if args.api_key:
        # If it looks like a file path, read it; otherwise treat as API key string
        if os.path.exists(args.api_key):
            with open(args.api_key, "r") as f:
                os.environ["API_KEY"] = f.read().strip()
            log(f"API key loaded from file: {args.api_key}", node="main")
        else:
            os.environ["API_KEY"] = args.api_key
            log("API key set from CLI argument", node="main")
    
    if args.google_token:
        # If it looks like a file path, read it; otherwise treat as JSON string
        if os.path.exists(args.google_token):
            with open(args.google_token, "r") as f:
                os.environ["GOOGLE_TOKEN"] = f.read().strip()
            log(f"Google token loaded from file: {args.google_token}", node="main")
        else:
            os.environ["GOOGLE_TOKEN"] = args.google_token
            log("Google token set from CLI argument", node="main")
    
    if args.github_token:
        # If it looks like a file path, read it; otherwise treat as token string
        if os.path.exists(args.github_token):
            with open(args.github_token, "r") as f:
                os.environ["GITHUB_TOKEN"] = f.read().strip()
            log(f"GitHub token loaded from file: {args.github_token}", node="main")
        else:
            os.environ["GITHUB_TOKEN"] = args.github_token
            log("GitHub token set from CLI argument", node="main")
    
    if args.resend_api_key:
        # If it looks like a file path, read it; otherwise treat as API key string
        if os.path.exists(args.resend_api_key):
            with open(args.resend_api_key, "r") as f:
                os.environ["RESEND_API_KEY"] = f.read().strip()
            log(f"Resend API key loaded from file: {args.resend_api_key}", node="main")
        else:
            os.environ["RESEND_API_KEY"] = args.resend_api_key
            log("Resend API key set from CLI argument", node="main")

    if args.slack_webhook_url:
        # If it looks like a file path, read it; otherwise treat as webhook URL string
        if os.path.exists(args.slack_webhook_url):
            with open(args.slack_webhook_url, "r") as f:
                os.environ["SLACK_WEBHOOK_URL"] = f.read().strip()
            log(f"Slack webhook URL loaded from file: {args.slack_webhook_url}", node="main")
        elif args.slack_webhook_url.startswith("https://"):
            os.environ["SLACK_WEBHOOK_URL"] = args.slack_webhook_url
            log("Slack webhook URL set from CLI argument", node="main")
        # If file doesn't exist and it's not a URL, silently skip (default file may not exist)

    if args.debug:
        os.environ["DEBUG_MODE"] = args.debug
        log(f"Debug mode enabled: {args.debug}", node="main")
    
    if args.fastest_model:
        import config
        config.set_fastest_model(True)
        log("Fastest model mode enabled: all nodes will use FAST_MODEL", node="main")


def main():
    """Run the VEP governance agent."""
    # Register signal handlers for graceful shutdown
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # Parse command line arguments
    args = parse_args()

    # Handle --clear-history flag (clears caches and exits)
    if args.clear_history:
        log("Clearing all history and caches...", node="main")
        cleared = 0

        # Clear state cache
        if STATE_CACHE_FILE.exists():
            STATE_CACHE_FILE.unlink()
            log(f"  Removed: {STATE_CACHE_FILE}", node="main")
            cleared += 1

        # Clear index cache
        index_cache_file = Path(__file__).parent / "cache" / "index_cache.json"
        if index_cache_file.exists():
            index_cache_file.unlink()
            log(f"  Removed: {index_cache_file}", node="main")
            cleared += 1

        # Clear history snapshots
        history_cleared = clear_all_history()
        if history_cleared > 0:
            log(f"  Removed: {history_cleared} history snapshot(s)", node="main")
            cleared += history_cleared

        # Clear alert persistence
        persistence_cleared = clear_persistence()
        if persistence_cleared > 0:
            log(f"  Removed: {persistence_cleared} alert persistence entries", node="main")
            cleared += 1

        log(f"Cleared {cleared} item(s). Ready for fresh start.", node="main")
        return

    # Set up credentials from CLI args
    setup_credentials(args)
    
    # Handle index cache flags
    index_cache_minutes = 0 if args.no_index_cache else args.index_cache_minutes
    if args.no_index_cache:
        log("Index caching disabled (--no-index-cache)", node="main")
    elif index_cache_minutes != 60:  # Only log if different from default
        log(f"Index cache timeout set to {index_cache_minutes} minutes", node="main")

    # Log startup configuration flags
    log_startup_flags(args, index_cache_minutes)
    
    # Create the graph
    agent = create_graph()
    log("Graph created successfully", node="main")
    
    # Initialize state
    initial_state = get_initial_state(sheet_id=args.sheet_id, index_cache_minutes=index_cache_minutes, one_cycle=args.one_cycle, skip_monitoring=args.skip_monitoring, skip_sheets=args.skip_sheets, skip_update_board=args.skip_update_board, skip_send_email=args.skip_send_email, skip_send_slack=args.skip_send_slack, mock_veps=args.mock_veps, mock_analyzed_combined=args.mock_analyzed_combined, mock_alert_summary=args.mock_alert_summary, immediate_start=args.immediate_start, use_state_cache=args.use_state_cache)

    # Load state cache if requested
    if args.use_state_cache:
        cached_state = load_state_cache()
        if cached_state:
            # Merge cached fields into initial state
            initial_state["veps"] = cached_state.get("veps", [])
            initial_state["release_schedule"] = cached_state.get("release_schedule")
            initial_state["current_release"] = cached_state.get("current_release")
            initial_state["general_insights"] = cached_state.get("general_insights", [])
            initial_state["alerts"] = cached_state.get("alerts", [])
            log(f"State cache merged: {len(initial_state['veps'])} VEPs loaded", node="main")
        else:
            log("No valid state cache found, will run full pipeline", node="main", level="WARNING")

    log("Initial state prepared", node="main")
    log(f"Sheet config: {initial_state['sheet_config']}", node="main")
    
    # Run the agent
    response = initial_state
    try:
        if args.one_cycle:
            # In one-cycle mode, run until update_sheets completes
            log("Invoking agent (one-cycle mode)...", node="main")
            current_state = initial_state
            max_iterations = 50  # Safety limit
            iteration = 0

            while iteration < max_iterations:
                iteration += 1
                response = agent.invoke(current_state)

                if is_shutdown_requested():
                    log("Agent interrupted by user. Exiting...", node="main", level="INFO")
                    return

                # Check if we should exit after sheet update
                if response.get("_exit_after_sheets", False):
                    log("One-cycle mode: Sheet update completed, exiting", node="main")
                    return

                # Update state for next iteration
                current_state = response

                # Safety check: if no tasks are scheduled and sheets don't need update, exit
                if not response.get("next_tasks") and not response.get("sheets_need_update", False):
                    log("No more tasks scheduled and sheets are up to date, exiting", node="main")
                    return
            log(f"One-cycle mode: Reached max iterations ({max_iterations}), exiting", node="main", level="WARNING")
            return
        else:
            # Normal mode - run continuously
            log("Invoking agent...", node="main")
            response = agent.invoke(initial_state)

        if is_shutdown_requested():
            log("Agent interrupted by user. Exiting...", node="main", level="INFO")
            return

        log("Agent execution completed", node="main")
        log(f"Final state keys: {list(response.keys())}", node="main")

        # Check if sheet was created
        sheet_config = response.get("sheet_config", {})
        if sheet_config.get("sheet_id"):
            log(f"✓ Sheet created/updated! Sheet ID: {sheet_config['sheet_id']}", node="main")
            log(f"  View at: https://docs.google.com/spreadsheets/d/{sheet_config['sheet_id']}/edit", node="main")
        else:
            log("Sheet ID not yet set - will be created on first update_sheets run", node="main")

    except KeyboardInterrupt:
        log("\nInterrupted by user. Exiting gracefully...", node="main", level="INFO")
        sys.exit(130)
    except Exception as e:
        if is_shutdown_requested():
            log(f"Error occurred during shutdown: {e}", node="main", level="WARNING")
            sys.exit(130)
        log(f"Error running agent: {e}", node="main", level="ERROR")
        import traceback
        log(f"Traceback: {traceback.format_exc()}", node="main", level="ERROR")
        raise


if __name__ == "__main__":
    main()
