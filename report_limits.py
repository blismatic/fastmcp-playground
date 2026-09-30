"""Per-user limits on `run_report`: how many can run at once, and how many can start per minute.

FastMCP's built-in rate limiters don't fit here: by default they share one limit across all users,
they count every MCP message (not just reports), they limit rate rather than concurrency, and they
reject with a protocol error instead of a message the model can act on. This middleware limits only
`run_report`, keys on the caller's Auth0 `sub`, and rejects with a ToolError that says when to retry.

State is in memory, per server process: with several server instances, each enforces its own limits.
"""

import math
import time
from collections import deque
from collections.abc import Callable

import anyio
import mcp_types as mt
from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools.base import ToolResult

from identity import get_current_sub

MAX_CONCURRENT_PER_USER = 2
# Generous on purpose: a rejected pipeline also counts, and the model's fix-and-retry must not be blocked.
MAX_REPORTS_PER_MINUTE = 30
# Kept below anyio's 40 worker threads so `whoami` and the other tools always have threads left.
MAX_CONCURRENT_TOTAL = 30
WINDOW_SECONDS = 60


class ReportLimits(Middleware):
    """Reject `run_report` calls that exceed the per-user or server-wide limits. Other tools pass through."""

    def __init__(
        self,
        *,
        tool_name: str = "run_report",
        max_concurrent_per_user: int = MAX_CONCURRENT_PER_USER,
        max_per_minute: int = MAX_REPORTS_PER_MINUTE,
        max_concurrent_total: int = MAX_CONCURRENT_TOTAL,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._tool_name = tool_name
        self._max_concurrent_per_user = max_concurrent_per_user
        self._max_per_minute = max_per_minute
        self._max_concurrent_total = max_concurrent_total
        self._clock = clock
        self._running: dict[str, int] = {}  # sub -> reports running now
        self._starts: dict[str, deque[float]] = {}  # sub -> start times within the window
        self._total_running = 0
        self._lock = anyio.Lock()

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        if context.message.name != self._tool_name:
            return await call_next(context)

        sub = get_current_sub()
        await self._acquire(sub)
        try:
            return await call_next(context)
        finally:
            await self._release(sub)

    async def _acquire(self, sub: str) -> None:
        async with self._lock:
            now = self._clock()
            self._forget_expired(now)
            starts = self._starts.get(sub, deque())

            if self._running.get(sub, 0) >= self._max_concurrent_per_user:
                raise ToolError(
                    f"You're running too many reports at once (limit {self._max_concurrent_per_user}). "
                    "Wait for one to finish, then retry."
                )
            if len(starts) >= self._max_per_minute:
                retry_in = max(1, math.ceil(starts[0] + WINDOW_SECONDS - now)) if starts else WINDOW_SECONDS
                raise ToolError(f"Report limit reached ({self._max_per_minute} per minute). Try again in {retry_in} seconds.")
            if self._total_running >= self._max_concurrent_total:
                raise ToolError("The server is busy running other reports. Try again in a few seconds.")

            self._running[sub] = self._running.get(sub, 0) + 1
            self._total_running += 1
            starts.append(now)
            self._starts[sub] = starts

    async def _release(self, sub: str) -> None:
        async with self._lock:
            self._total_running -= 1
            self._running[sub] -= 1
            if not self._running[sub]:
                del self._running[sub]

    def _forget_expired(self, now: float) -> None:
        """Drop start times older than the window, and users with none left, so memory stays bounded."""
        cutoff = now - WINDOW_SECONDS
        for sub in list(self._starts):
            starts = self._starts[sub]
            while starts and starts[0] <= cutoff:
                starts.popleft()
            if not starts:
                del self._starts[sub]
