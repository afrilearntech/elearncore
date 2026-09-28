import logging
from typing import Callable, Any

logger = logging.getLogger(__name__)


def fire_and_forget(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
    """Dispatch a function which enqueues durable background work."""
    try:
        fn(*args, **kwargs)
    except Exception:
        logger.exception("Unable to enqueue background task")
