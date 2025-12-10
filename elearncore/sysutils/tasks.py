import logging
import threading
from typing import Callable, Any

logger = logging.getLogger(__name__)


def _run_safely(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
    """Execute a function and log any exception instead of raising it.

    This is intended for background "fire-and-forget" style tasks where
    failures should not impact the HTTP request lifecycle.
    """
    try:
        fn(*args, **kwargs)
    except Exception:
        logger.exception("Background task failed")


def fire_and_forget(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
    """Run ``fn(*args, **kwargs)`` in a daemon thread and return immediately.

    This is a lightweight helper for non-critical background work such as
    sending notifications. It is *best-effort* only: tasks may be lost if the
    process exits, and there are no retries.
    """
    thread = threading.Thread(
        target=_run_safely,
        args=(fn, *args),
        kwargs=kwargs,
        daemon=True,
    )
    thread.start()
