#!/usr/bin/env python3
"""Main entry point for VEP governance agent."""

import argparse
import os
import signal
import sys
from datetime import datetime
from typing import Optional
from langchain_core.messages import HumanMessage
from graph import create_graph
from services.utils import log, invoke_agent

# Global flag for graceful shutdown
_shutdown_requested = False


def get_initial_state(sheet_id: Optional[str] = None, index_cache_minutes: int = 60, one_cycle: bool = False, skip_monitoring: bool = False, skip_sheets: bool = False, mock_veps: bool = False):
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
        "sheets_need_update": False,
        "errors": [],
        "config_cache": {},
        "vep_updates_by_check": {},
        "sheet_config": sheet_config,
        "index_cache_minutes": index_cache_minutes,  # Store cache timeout in state
        "one_cycle": one_cycle,  # Flag to exit after one cycle
        "skip_monitoring": skip_monitoring,  # Flag to skip monitoring checks
        "skip_sheets": skip_sheets,  # Flag to skip sheet updates
        "mock_veps": mock_veps,  # Flag to use mock VEPs instead of fetching from GitHub
    }


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
        help="Force all nodes to use GEMINI_3_FLASH_PREVIEW (fastest model) regardless of node configuration"
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
        "--mock-veps",
        action="store_true",
        help="Use mock VEPs instead of fetching from GitHub. Skips VEP discovery entirely and creates sample VEPs for testing. Useful for testing sheets and alerts without API calls."
    )
    return parser.parse_args()


def signal_handler(signum, frame):
    """Handle SIGINT (Ctrl+C) gracefully."""
    global _shutdown_requested
    if _shutdown_requested:
        # Second Ctrl+C - force exit
        log("\nForce exit requested. Terminating...", node="main", level="WARNING")
        sys.exit(130)  # Standard exit code for SIGINT
    else:
        _shutdown_requested = True
        log("\nShutdown requested (Ctrl+C). Finishing current operation and exiting gracefully...", node="main", level="INFO")


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
    
    if args.debug:
        os.environ["DEBUG_MODE"] = args.debug
        log(f"Debug mode enabled: {args.debug}", node="main")
    
    if args.fastest_model:
        import config
        config.set_fastest_model(True)
        log("Fastest model mode enabled: all nodes will use GEMINI_3_FLASH_PREVIEW", node="main")


def main():
    """Run the VEP governance agent."""
    global _shutdown_requested
    
    # Register signal handler for graceful shutdown
    signal.signal(signal.SIGINT, signal_handler)
    
    # Parse command line arguments
    args = parse_args()
    
    # Set up credentials from CLI args
    setup_credentials(args)
    
    # Handle index cache flags
    index_cache_minutes = 0 if args.no_index_cache else args.index_cache_minutes
    if args.no_index_cache:
        log("Index caching disabled (--no-index-cache)", node="main")
    elif index_cache_minutes != 60:  # Only log if different from default
        log(f"Index cache timeout set to {index_cache_minutes} minutes", node="main")

    # Log startup configuration (excluding sensitive credentials)
    log("Starting VEP governance agent", node="main")
    log("Configuration flags:", node="main")
    flags = []
    if args.api_key:
        # Show file path if it's a file, otherwise just indicate it was set
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
    if args.mock_veps:
        flags.append("  --mock-veps: enabled")
    
    if flags:
        for flag in flags:
            log(flag, node="main")
    else:
        log("  (using defaults)", node="main")
    log("Press Ctrl+C to exit gracefully", node="main")
    
    # Create the graph
    agent = create_graph()
    log("Graph created successfully", node="main")
    
    # Initialize state
    initial_state = get_initial_state(sheet_id=args.sheet_id, index_cache_minutes=index_cache_minutes, one_cycle=args.one_cycle, skip_monitoring=args.skip_monitoring, skip_sheets=args.skip_sheets, mock_veps=args.mock_veps)
    log("Initial state prepared", node="main")
    log(f"Sheet config: {initial_state['sheet_config']}", node="main")
    if args.one_cycle:
        log("One-cycle mode: will exit after sheet update completes", node="main")
    if args.skip_monitoring:
        log("Skip-monitoring mode: monitoring checks (deadlines, activity, compliance, exceptions) will be skipped", node="main")
    if args.skip_sheets:
        log("Skip-sheets mode: Google Sheets updates will be skipped", node="main")
    if args.mock_veps:
        log("Mock VEPs mode: will use mock VEPs instead of fetching from GitHub", node="main")
    
    # Run the agent
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
                
                if _shutdown_requested:
                    log("Agent interrupted by user. Exiting...", node="main", level="INFO")
                    return
                
                # Check if we should exit after sheet update
                if response.get("_exit_after_sheets", False):
                    log("One-cycle mode: Sheet update completed, exiting", node="main")
                    return  # Exit immediately
                
                # Update state for next iteration
                current_state = response
                
                # Safety check: if no tasks are scheduled and sheets don't need update, exit
                if not response.get("next_tasks") and not response.get("sheets_need_update", False):
                    log("No more tasks scheduled and sheets are up to date, exiting", node="main")
                    return
            log(f"One-cycle mode: Reached max iterations ({max_iterations}), exiting", node="main", level="WARNING")
        else:
            # Normal mode - run continuously
            log("Invoking agent...", node="main")
            response = agent.invoke(initial_state)
        
        if _shutdown_requested:
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
        if _shutdown_requested:
            log(f"Error occurred during shutdown: {e}", node="main", level="WARNING")
            sys.exit(130)
        log(f"Error running agent: {e}", node="main", level="ERROR")
        import traceback
        log(f"Traceback: {traceback.format_exc()}", node="main", level="ERROR")
        raise


if __name__ == "__main__":
    main()
