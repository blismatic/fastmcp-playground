import asyncio
import json
import logging

import pytest
from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import MiddlewareContext

import audit_logging
from audit_logging import AUDIT_LOG, AuditLogging, audit

SUB = "waad|MixedCaseSub123"


class _Collect(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record):
        self.records.append(record)


@pytest.fixture
def audit_lines():
    """Capture audit lines as parsed JSON, from a handler attached directly to the audit logger."""
    handler = _Collect()
    AUDIT_LOG.addHandler(handler)
    previous_level = AUDIT_LOG.level
    AUDIT_LOG.setLevel(logging.INFO)
    yield lambda: [json.loads(record.getMessage()) for record in handler.records]
    AUDIT_LOG.removeHandler(handler)
    AUDIT_LOG.setLevel(previous_level)


@pytest.fixture
def caller(monkeypatch):
    """Set who is calling: a sub, or None for an unauthenticated caller."""

    def set_caller(sub):
        def current_sub():
            if sub is None:
                raise ToolError("This tool requires authentication.")
            return sub

        monkeypatch.setattr(audit_logging, "get_current_sub", current_sub)

    return set_caller


def run_tool_call(arguments, *, fail=False):
    async def call_next(context):
        if fail:
            raise ToolError("Stage '$lookup' is not allowed in reports.")
        return "ok"

    context = MiddlewareContext(message={"name": "run_report", "arguments": arguments}, method="tools/call")

    async def go():
        try:
            await AuditLogging().on_message(context, call_next)
        except ToolError:
            pass

    asyncio.run(go())


def test_every_line_is_json_with_time_and_sub(audit_lines, caller):
    caller(SUB)
    run_tool_call({"pipeline": [{"$match": {}}]})
    start, success = audit_lines()
    assert start["event"] == "request_start" and success["event"] == "request_success"
    for line in (start, success):
        assert line["sub"] == SUB and line["time"].endswith("+00:00")
    assert "$match" in start["payload"]
    assert "duration_ms" in success


def test_unauthenticated_caller_is_logged_as_null(audit_lines, caller):
    caller(None)
    run_tool_call({"pipeline": []})
    assert all(line["sub"] is None for line in audit_lines())


def test_failure_is_logged_with_the_models_error_message(audit_lines, caller):
    caller(SUB)
    run_tool_call({"pipeline": [{"$lookup": {}}]}, fail=True)
    error = audit_lines()[-1]
    assert error["event"] == "request_error" and "$lookup" in error["error"]


def test_payload_is_cut_off_at_4_kb(audit_lines, caller):
    caller(SUB)
    run_tool_call({"pipeline": [{"$match": {"x": "a" * 20_000}}]})
    payload = audit_lines()[0]["payload"]
    assert len(payload) <= 4000 + len("...") and payload.endswith("...")


def test_audit_writes_its_fields(audit_lines, caller):
    caller(SUB)
    audit("report_result", returned_count=3, truncated=False)
    (line,) = audit_lines()
    assert line == {"time": line["time"], "event": "report_result", "sub": SUB, "returned_count": 3, "truncated": False}
