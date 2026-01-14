#!/usr/bin/env python3
"""Main entry point for VEP governance agent."""

import argparse
import os
from datetime import datetime
from typing import Optional
from langchain_core.messages import HumanMessage
from graph import create_graph
from services.utils import log, invoke_agent


def get_initial_state(sheet_id: Optional[str] = None):
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
    return parser.parse_args()


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


def main():
    """Run the VEP governance agent."""
    # Parse command line arguments
    args = parse_args()
    
    # Set up credentials from CLI args
    setup_credentials(args)
    
    log("Starting VEP governance agent", node="main")
    
    # Create the graph
    agent = create_graph()
    log("Graph created successfully", node="main")
    
    # Initialize state
    initial_state = get_initial_state(sheet_id=args.sheet_id)
    log("Initial state prepared", node="main")
    log(f"Sheet config: {initial_state['sheet_config']}", node="main")
    
    # Run the agent
    try:
        log("Invoking agent...", node="main")
        response = agent.invoke(initial_state)
        
        log("Agent execution completed", node="main")
        log(f"Final state keys: {list(response.keys())}", node="main")
        
        # Check if sheet was created
        sheet_config = response.get("sheet_config", {})
        if sheet_config.get("sheet_id"):
            log(f"✓ Sheet created/updated! Sheet ID: {sheet_config['sheet_id']}", node="main")
            log(f"  View at: https://docs.google.com/spreadsheets/d/{sheet_config['sheet_id']}/edit", node="main")
        else:
            log("Sheet ID not yet set - will be created on first update_sheets run", node="main")
            
    except Exception as e:
        log(f"Error running agent: {e}", node="main", level="ERROR")
        import traceback
        log(f"Traceback: {traceback.format_exc()}", node="main", level="ERROR")
        raise


if __name__ == "__main__":
    main()
