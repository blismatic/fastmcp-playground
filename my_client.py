"""End-to-end smoke test against the server running over HTTP with Auth0 enabled.

Every run opens a browser for the FastMCP consent page and the Auth0 login. Tokens are kept in
memory only, so nothing is saved between runs.
"""

import asyncio
import os

from fastmcp import Client
from fastmcp.exceptions import ToolError

SERVER_URL = "http://localhost:8000/mcp"

GROUP_BY_CATALOG = [{"$group": {"_id": "$catalog", "n": {"$sum": 1}}}]
LOOKUP_EXPLOIT = [{"$lookup": {"from": os.getenv("MONGO_COLLECTION", "records"), "pipeline": [], "as": "leak"}}]


async def main():
    async with Client(SERVER_URL, auth="oauth") as client:
        identity = (await client.call_tool("whoami")).data
        print("whoami:", identity)
        assert identity["authenticated"], "whoami reports the caller as unauthenticated"
        print(f"Token expires in {identity['token_expires_in_seconds']}s")

        report = (await client.call_tool("run_report", {"pipeline": GROUP_BY_CATALOG})).data
        seen = {row["_id"] for row in report["results"]}
        print("Catalogs in report:", sorted(seen, key=str))
        if not identity["is_staff"]:
            leaked = seen - set(identity["catalogs"])
            assert not leaked, f"LEAK: report returned catalogs outside the user's scope: {leaked}"

        # Nobody may $lookup, staff included. The guard must reject it before MongoDB sees it,
        # so the error has to be the guard's, not a MongoDB failure.
        try:
            await client.call_tool("run_report", {"pipeline": LOOKUP_EXPLOIT})
        except ToolError as exc:
            assert "Stage '$lookup' is not allowed" in str(exc), f"$lookup failed, but not in the guard: {exc}"
            print("$lookup rejected by the guard as expected")
        else:
            raise AssertionError("LEAK: $lookup was accepted")

        print("All checks passed.")


if __name__ == "__main__":
    asyncio.run(main())
