import asyncio
from types import SimpleNamespace

import pytest
from fastmcp.exceptions import ToolError

import report_limits
from report_limits import ReportLimits


class Harness:
    """Drives ReportLimits directly: a fake clock, a switchable caller, and reports that can be held open."""

    def __init__(self, monkeypatch, **limits):
        self.now = 1000.0
        self.sub = "auth0|alice"
        monkeypatch.setattr(report_limits, "get_current_sub", lambda: self.sub)
        self.limits = ReportLimits(clock=lambda: self.now, **limits)

    async def call(self, tool="run_report", *, hold: asyncio.Event | None = None, fail=False):
        async def call_next(_context):
            if hold is not None:
                await hold.wait()
            if fail:
                raise RuntimeError("report failed")
            return "ok"

        context = SimpleNamespace(message=SimpleNamespace(name=tool))
        return await self.limits.on_call_tool(context, call_next)  # type: ignore[arg-type]

    async def start_held(self, sub: str) -> tuple[asyncio.Task, asyncio.Event]:
        """Start a report for `sub` that stays running until the returned event is set."""
        self.sub = sub
        hold = asyncio.Event()
        task = asyncio.create_task(self.call(hold=hold))
        await asyncio.sleep(0)  # let it acquire its slot
        return task, hold


def test_third_concurrent_report_is_rejected_until_one_finishes(monkeypatch):
    async def run():
        h = Harness(monkeypatch, max_concurrent_per_user=2)
        first, release_first = await h.start_held("auth0|alice")
        second, release_second = await h.start_held("auth0|alice")
        with pytest.raises(ToolError, match="too many reports at once"):
            await h.call()
        release_first.set()
        await first
        assert await h.call() == "ok"
        release_second.set()
        await second

    asyncio.run(run())


def test_per_minute_limit_rejects_then_recovers(monkeypatch):
    async def run():
        h = Harness(monkeypatch, max_per_minute=3)
        for _ in range(3):
            assert await h.call() == "ok"
        h.now += 15
        with pytest.raises(ToolError, match=r"3 per minute\). Try again in 45 seconds"):
            await h.call()
        h.now += 46
        assert await h.call() == "ok"

    asyncio.run(run())


def test_users_do_not_share_per_user_limits(monkeypatch):
    async def run():
        h = Harness(monkeypatch, max_concurrent_per_user=1)
        alice, release_alice = await h.start_held("auth0|alice")
        h.sub = "auth0|bob"
        assert await h.call() == "ok"
        release_alice.set()
        await alice

    asyncio.run(run())


def test_server_wide_limit_applies_across_users(monkeypatch):
    async def run():
        h = Harness(monkeypatch, max_concurrent_total=2)
        a, release_a = await h.start_held("auth0|alice")
        b, release_b = await h.start_held("auth0|bob")
        h.sub = "auth0|carol"
        with pytest.raises(ToolError, match="server is busy"):
            await h.call()
        release_a.set()
        release_b.set()
        await asyncio.gather(a, b)

    asyncio.run(run())


def test_other_tools_are_never_limited(monkeypatch):
    async def run():
        h = Harness(monkeypatch, max_per_minute=0)
        with pytest.raises(ToolError):
            await h.call("run_report")
        for tool in ("whoami", "read_resource", "ping_database_connection"):
            assert await h.call(tool) == "ok"

    asyncio.run(run())


def test_failing_report_still_frees_its_slot(monkeypatch):
    async def run():
        h = Harness(monkeypatch, max_concurrent_per_user=1)
        with pytest.raises(RuntimeError):
            await h.call(fail=True)
        assert await h.call() == "ok"
        assert h.limits._running == {} and h.limits._total_running == 0

    asyncio.run(run())


def test_old_start_times_are_forgotten(monkeypatch):
    async def run():
        h = Harness(monkeypatch)
        for sub in ("auth0|alice", "auth0|bob"):
            h.sub = sub
            await h.call()
        h.now += 61
        h.sub = "auth0|carol"
        await h.call()
        assert set(h.limits._starts) == {"auth0|carol"}

    asyncio.run(run())
