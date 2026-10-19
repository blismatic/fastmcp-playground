"""Audit logging: who called which tool, when, with what pipeline, and how it turned out.

Builds on FastMCP's StructuredLoggingMiddleware (JSON lines), adding the caller's Auth0 `sub` and a
UTC timestamp to every line. The audit logger has its own stderr handler that prints only the
message, so every line is pure JSON regardless of how FastMCP formats its own logs.
"""

import json
import logging
import sys
from datetime import UTC, datetime

from fastmcp.exceptions import ToolError
from fastmcp.server.middleware.logging import StructuredLoggingMiddleware

from identity import get_current_sub

AUDIT_LOG = logging.getLogger("audit")
AUDIT_LOG.setLevel(logging.INFO)
AUDIT_LOG.propagate = False  # don't also print through the root logger's text format
if not AUDIT_LOG.handlers:
    _handler = logging.StreamHandler(sys.stderr)  # stderr: stdout carries the MCP protocol on stdio
    _handler.setFormatter(logging.Formatter("%(message)s"))
    AUDIT_LOG.addHandler(_handler)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _caller_sub() -> str | None:
    """The caller's Auth0 sub, or None. Logging must never fail a request."""
    try:
        return get_current_sub()
    except ToolError:
        return None


def audit(event: str, **fields) -> None:
    """Write one JSON audit line with the time and the caller's sub."""
    AUDIT_LOG.info(json.dumps({"time": _now(), "event": event, "sub": _caller_sub(), **fields}))


class AuditLogging(StructuredLoggingMiddleware):
    """StructuredLoggingMiddleware plus the caller's sub and a UTC timestamp on every line."""

    def __init__(self) -> None:
        super().__init__(logger=AUDIT_LOG, include_payloads=True, methods=["tools/call", "resources/read"])
        self.max_payload_length = 4000  # this class has no parameter for it

    # Overrides a private FastMCP method; re-check after upgrading FastMCP.
    def _log_message(self, message: dict[str, str | int | float], log_level: int | None = None) -> None:
        entry: dict = {"time": _now(), "sub": _caller_sub(), **message}  # sub may be None -> JSON null
        super()._log_message(entry, log_level)
