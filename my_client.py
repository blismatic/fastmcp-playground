import asyncio
from pathlib import Path

from fastmcp import Client

# client = Client("http://localhost:8000")
client = Client(Path("my_server.py"))


async def call_tool(name: str):
    async with client:
        result = await client.call_tool("greet", {"name": name})
        print(result)


asyncio.run(call_tool("Alice"))
