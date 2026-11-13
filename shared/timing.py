"""Lightweight timing instrumentation for data collection modules."""

from __future__ import annotations

import functools
import logging
import threading
import time
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from shared.logging_config import get_logger

logger = get_logger(__name__)

_active_capture: ContextVar[logging.Handler | None] = ContextVar(
    "_active_capture", default=None
)


class _ThreadErrorCapture(logging.Handler):
    """Temporary handler that captures ERROR+ records within the active context.

    Uses a ContextVar so that child threads created via ContextAwareExecutor
    inherit the parent's capture handler, while threads from a plain
    ThreadPoolExecutor (separate modules) remain isolated.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.ERROR)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        if _active_capture.get(None) is self:
            self.records.append(record)


@dataclass
class ModuleResult:
    """Result of a single module's execution."""

    name: str
    status: str  # "success", "warning", or "error"
    duration_seconds: float
    error: str | None = None


@dataclass
class RunTiming:
    """Collects timing data for all modules in a run."""

    modules: dict[str, ModuleResult] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _abandoned: set[str] = field(default_factory=set, repr=False)

    def record(self, result: ModuleResult) -> None:
        with self._lock:
            # An abandoned module's thread may still finish later; keep
            # the timeout error rather than letting a late success hide it
            if result.name in self._abandoned:
                return
            self.modules[result.name] = result

    def abandon(self, name: str, duration_seconds: float, error: str) -> None:
        """Record a module as timed out and ignore any later result for it."""
        with self._lock:
            self._abandoned.add(name)
            self.modules[name] = ModuleResult(
                name=name,
                status="error",
                duration_seconds=duration_seconds,
                error=error,
            )

    def summary(self) -> dict[str, dict]:
        with self._lock:
            return {
                name: {
                    "status": r.status,
                    "duration_seconds": round(r.duration_seconds, 2),
                    "error": r.error,
                }
                for name, r in self.modules.items()
            }


_timing: RunTiming | None = None


def get_timing() -> RunTiming:
    """Get the current RunTiming instance, creating one if needed."""
    global _timing
    if _timing is None:
        _timing = RunTiming()
    return _timing


def reset_timing() -> None:
    """Clear the timing data. Used in test teardown."""
    global _timing
    _timing = None


def timed(name: str) -> Callable:
    """Decorator that times a callable and records the result.

    Records "warning" status if the function returns normally but logged
    ERROR-level messages during execution (e.g. caught exceptions).
    """

    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            capture = _ThreadErrorCapture()
            root = logging.getLogger()
            root.addHandler(capture)
            token = _active_capture.set(capture)
            start = time.monotonic()
            try:
                result = func(*args, **kwargs)
                elapsed = time.monotonic() - start
                if capture.records:
                    error_msgs = "; ".join(r.getMessage() for r in capture.records)
                    logger.info("%s completed in %.2fs (with warnings)", name, elapsed)
                    get_timing().record(
                        ModuleResult(
                            name=name,
                            status="warning",
                            duration_seconds=elapsed,
                            error=error_msgs,
                        )
                    )
                else:
                    logger.info("%s completed in %.2fs", name, elapsed)
                    get_timing().record(
                        ModuleResult(
                            name=name,
                            status="success",
                            duration_seconds=elapsed,
                        )
                    )
                return result
            except Exception as e:
                elapsed = time.monotonic() - start
                logger.error("%s failed after %.2fs: %s", name, elapsed, e)
                get_timing().record(
                    ModuleResult(
                        name=name,
                        status="error",
                        duration_seconds=elapsed,
                        error=str(e),
                    )
                )
                raise
            finally:
                _active_capture.reset(token)
                root.removeHandler(capture)

        return wrapper

    return decorator
