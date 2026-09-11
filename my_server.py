import random

from fastmcp import FastMCP
from prefab_ui.app import PrefabApp
from prefab_ui.components import Badge, Column, Heading, Row, Text

mcp = FastMCP("My MCP Server")


@mcp.tool(app=True)
def greet(name: str) -> PrefabApp:
    """Greet someone with a visual card."""
    with Column(gap=4, css_class="p-6") as view:
        Heading(f"Hello, {name}!")
        with Row(gap=2, align="center"):
            Text("Status")
            Badge("Greeted", variant="success")

    return PrefabApp(view=view)


@mcp.tool
def roll_dice(n_dice: int) -> list[int]:
    """Roll `n_dice` 6-sided dice and return the results."""
    return [random.randint(1, 6) for _ in range(n_dice)]


@mcp.tool
def generate_number(lower_bound: int, upper_bound: int) -> int:
    """Generate a random number between `lower_bound` and `upper_bound`. Result is inclusive of both bounds."""
    return random.randint(lower_bound, upper_bound)


if __name__ == "__main__":
    mcp.run()
