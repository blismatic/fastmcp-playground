import json
import random
from pathlib import Path

from fastmcp import FastMCP
from fastmcp.apps.generative import GenerativeUI
from prefab_ui.app import PrefabApp
from prefab_ui.components import Badge, Column, Heading, Row, Text

mcp = FastMCP("My MCP Server")
mcp.add_provider(GenerativeUI())

DATA_MODEL_SCHEMA_PATH = Path("schemas/data_model_schema.json")
PARENT_SCHEMA_PATH = Path("schemas/parent_schema.json")


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


@mcp.resource("data://schema/main", name="Data Model Schema", description="A schema for the data model.", mime_type="application/json")
def get_data_model_schema() -> dict:
    return json.loads(DATA_MODEL_SCHEMA_PATH.read_text())


@mcp.resource(
    "data://schema/parent",
    name="Parent Schema",
    description="A wrapped schema for the parent document which includes metadata but also contains the main data model.",
    mime_type="application/json",
)
def get_parent_schema() -> dict:
    parent = json.loads(PARENT_SCHEMA_PATH.read_text())

    parent["properties"]["record"]["properties"]["data"] = get_data_model_schema()
    return parent


if __name__ == "__main__":
    mcp.run()
