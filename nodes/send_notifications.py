"""Send notifications coordination node - triggers email and Slack in parallel."""

from datetime import datetime
from typing import Any
from state import VEPState
from services.utils import log


def send_notifications_node(state: VEPState) -> Any:
    """Trigger all notification channels to run in parallel.

    This is a coordination node that doesn't do work itself, but allows
    the graph to route to all notification channels (email, Slack) simultaneously.
    The actual work is done by the individual send nodes (send_email, send_slack).
    """
    alerts = state.get("alerts", [])
    log(f"Triggering parallel notification channels for {len(alerts)} alert(s)", node="send_notifications")

    # Update last check time for this coordination node
    last_check_times = state.get("last_check_times", {})
    last_check_times["send_notifications"] = datetime.now()

    return {
        "last_check_times": last_check_times,
    }
