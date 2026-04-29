"""Shared shutdown coordination via threading.Event."""

import threading

_shutdown_event = threading.Event()


def is_shutdown_requested() -> bool:
    return _shutdown_event.is_set()


def request_shutdown() -> None:
    _shutdown_event.set()


def wait_for_shutdown(timeout: float) -> bool:
    """Block until shutdown is requested or timeout expires.

    Returns True if shutdown was requested, False on timeout.
    """
    return _shutdown_event.wait(timeout=timeout)
