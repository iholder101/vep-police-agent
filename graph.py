"""LangGraph definition for VEP governance agent."""

from typing import Literal, Any
from langgraph.graph import StateGraph
from langgraph.graph.state import CompiledStateGraph

from state import VEPState
from nodes.scheduler import scheduler_node
from nodes.fetch_veps import fetch_veps_node
from nodes.run_monitoring import run_monitoring_node
from nodes.check_deadlines import check_deadlines_node
from nodes.check_activity import check_activity_node
from nodes.check_compliance import check_compliance_node
from nodes.check_exceptions import check_exceptions_node
from nodes.analyze_combined import analyze_combined_node
from nodes.merge_vep_updates import merge_vep_updates_node
from nodes.update_sheets import update_sheets_node
from nodes.alert_summary import alert_summary_node
from nodes.send_email import send_email_node
from nodes.wait import wait_node

def create_graph() -> CompiledStateGraph[Any, Any, Any, Any]:
    """Create and configure the VEP governance agent graph.
    
    Graph flow:
    1. Scheduler determines which tasks to run (central coordinator)
    2. Routes to monitoring checks (which run in parallel)
    3. Each check fetches its own data from GitHub MCP and stores updates in vep_updates_by_check
    4. All checks complete → merge_vep_updates node merges all parallel updates
    5. Merged state → analyze_combined node reasons about combinations
    6. Analysis completes → returns to scheduler
    7. Scheduler decides next action (update_sheets, notify, wait)
    8. Loop continues with scheduler as the central hub
    
    Architecture:
    - Parallel monitoring checks for performance
    - Explicit merge step to combine parallel updates (LangGraph replaces lists by default)
    - Holistic analysis for cross-check reasoning
    - Scheduler-based coordination for flexibility
    """
    workflow = StateGraph(VEPState)
    
    # Add nodes
    workflow.add_node("scheduler", scheduler_node)
    workflow.add_node("fetch_veps", fetch_veps_node)
    workflow.add_node("run_monitoring", run_monitoring_node)
    workflow.add_node("check_deadlines", check_deadlines_node)
    workflow.add_node("check_activity", check_activity_node)
    workflow.add_node("check_compliance", check_compliance_node)
    workflow.add_node("check_exceptions", check_exceptions_node)
    workflow.add_node("merge_vep_updates", merge_vep_updates_node)
    workflow.add_node("analyze_combined", analyze_combined_node)
    workflow.add_node("update_sheets", update_sheets_node)
    workflow.add_node("alert_summary", alert_summary_node)
    workflow.add_node("send_email", send_email_node)
    workflow.add_node("wait", wait_node)
    
    # Set entry point
    workflow.set_entry_point("scheduler")
    
    # Define edges from scheduler
    # No path_map needed - route_scheduler returns node names directly
    workflow.add_conditional_edges(
        "scheduler",
        route_scheduler,
    )
    
    # run_monitoring triggers all checks in parallel
    # Each check fetches its own data from GitHub MCP
    workflow.add_edge("run_monitoring", "check_deadlines")
    workflow.add_edge("run_monitoring", "check_activity")
    workflow.add_edge("run_monitoring", "check_compliance")
    workflow.add_edge("run_monitoring", "check_exceptions")
    
    # All monitoring checks complete → merge_vep_updates (merges parallel updates)
    workflow.add_edge("check_deadlines", "merge_vep_updates")
    workflow.add_edge("check_activity", "merge_vep_updates")
    workflow.add_edge("check_compliance", "merge_vep_updates")
    workflow.add_edge("check_exceptions", "merge_vep_updates")
    
    # After merging, analyze combined results
    workflow.add_edge("merge_vep_updates", "analyze_combined")
    
    # Analysis completes, run sheet update and alert summary in parallel
    workflow.add_edge("analyze_combined", "update_sheets")
    workflow.add_edge("analyze_combined", "alert_summary")
    
    # Alert summary triggers email sending
    workflow.add_edge("alert_summary", "send_email")
    
    # Both update_sheets and send_email go back to scheduler
    workflow.add_edge("update_sheets", "scheduler")
    workflow.add_edge("send_email", "scheduler")
    
    # fetch_veps goes back to scheduler
    workflow.add_edge("fetch_veps", "scheduler")
    
    # wait node goes back to scheduler (creates continuous loop)
    workflow.add_edge("wait", "scheduler")
    
    return workflow.compile()


def route_scheduler(state: VEPState) -> Literal["fetch_veps", "run_monitoring", "update_sheets", "wait"]:
    """Route based on scheduler's next_tasks.
    
    Routes to the first task in the queue. The scheduler queues:
    - "fetch_veps" (discovers VEPs from GitHub)
    - "run_monitoring" (triggers all checks in parallel)
    - "update_sheets" (when sheets need updating)
    """
    import os
    debug_mode = os.environ.get("DEBUG_MODE")
    # In one-cycle mode or test-sheets debug mode, if sheet update completed, exit (don't route to wait)
    if (state.get("one_cycle", False) or debug_mode == "test-sheets") and state.get("_exit_after_sheets", False):
        # Don't route anywhere - main loop will detect this and exit
        return "wait"  # Return wait but wait node will exit immediately
    
    next_tasks = state.get("next_tasks", [])
    
    if not next_tasks:
        return "wait"
    
    # Return first task (scheduler should prioritize)
    task = next_tasks[0]
    
    # If skip_monitoring is enabled, don't route to run_monitoring (shouldn't be in queue, but be safe)
    skip_monitoring = state.get("skip_monitoring", False)
    if skip_monitoring and task == "run_monitoring":
        # Skip run_monitoring, try next task or wait
        if len(next_tasks) > 1:
            task = next_tasks[1]
        else:
            return "wait"
    
    # Validate it's a known task, otherwise wait
    valid_tasks = {"fetch_veps", "run_monitoring", "update_sheets"}
    return task if task in valid_tasks else "wait"
