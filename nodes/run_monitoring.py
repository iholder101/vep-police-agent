"""Run monitoring node - triggers all monitoring checks in parallel."""

from datetime import datetime
from typing import Any
from state import VEPState
from services.utils import log


def run_monitoring_node(state: VEPState) -> Any:
    """Trigger all monitoring checks to run in parallel.
    
    This is a coordination node that doesn't do work itself, but allows
    the graph to route to all monitoring checks simultaneously via multiple edges.
    The actual work is done by the individual check nodes.
    """
    log("Triggering parallel monitoring checks", node="run_monitoring")
    
    # Update last check time for this coordination node
    last_check_times = state.get("last_check_times", {})
    last_check_times["run_monitoring"] = datetime.now()
    
    # Remove this task from queue
    next_tasks = state.get("next_tasks", [])
    if next_tasks and next_tasks[0] == "run_monitoring":
        next_tasks = next_tasks[1:]
    
    return {
        "last_check_times": last_check_times,
        "next_tasks": next_tasks,
    }
