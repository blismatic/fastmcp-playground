import json
import os
from pathlib import Path

from fastmcp import Context, FastMCP
from fastmcp.apps.generative import GenerativeUI
from fastmcp.server.lifespan import lifespan
from fastmcp.server.transforms import ResourcesAsTools
from fastmcp.utilities.types import Image
from mcp.types import Icon, ToolAnnotations
from pymongo import MongoClient


@lifespan
async def mongodb_lifespan(server):
    # client = MongoClient("mongodb://localhost:27017/")
    client = MongoClient(
        f"mongodb+srv://{os.environ['MONGO_USER']}:{os.environ['MONGO_PASSWORD']}@{os.environ['MONGO_CLUSTER']}/?retryWrites=true&w=majority"
    )
    try:
        yield {"db": client[os.environ["MONGO_DB"]]}
    finally:
        client.close()


# Generate a data URI from a local image file
img_light = Image(path=Path(__file__).parent / "assets" / "brand" / "Bolt-Black.svg")
icon_light = Icon(src=img_light.to_data_uri(), theme="light")
img_dark = Image(path=Path(__file__).parent / "assets" / "brand" / "Bolt-White.svg")
icon_dark = Icon(src=img_dark.to_data_uri(), theme="dark")

mcp = FastMCP("My MCP Server", lifespan=mongodb_lifespan, icons=[icon_light, icon_dark])
mcp.add_provider(GenerativeUI())
mcp.add_transform(ResourcesAsTools(mcp))

DATA_MODEL_SCHEMA_PATH = Path(__file__).parent / "schemas" / "data_model_schema.json"
PARENT_SCHEMA_PATH = Path(__file__).parent / "schemas" / "parent.json"


@mcp.resource("data://schema/main", name="Data Model Schema", description="A schema for the data model.", mime_type="application/json")
def get_data_model_schema() -> dict:
    return json.loads(DATA_MODEL_SCHEMA_PATH.read_text(encoding="utf-8"))


@mcp.resource(
    "data://schema/parent",
    name="Parent Schema",
    description="A wrapped schema for the parent document which includes metadata but also contains the main data model.",
    mime_type="application/json",
)
def get_parent_schema() -> dict:
    parent = json.loads(PARENT_SCHEMA_PATH.read_text(encoding="utf-8"))

    parent["properties"]["record"]["properties"]["data"] = get_data_model_schema()
    return parent


def validate_pipeline(pipeline: list[dict]) -> None:
    # Only allow read-only, non-mutating aggregation stages
    ALLOWED_AGGREGATION_STAGES = {"$match", "$project", "$group", "$sort", "$limit", "$skip", "$count", "$unwind", "$lookup", "$facet", "$addFields"}

    for stage in pipeline:
        stage_name = next(iter(stage))
        if stage_name not in ALLOWED_AGGREGATION_STAGES:
            raise ValueError(f"MongoDB stage '{stage_name}' is not allowed in reports.")


@mcp.tool(name="Run Report", annotations=ToolAnnotations(read_only_hint=True))
def run_report(pipeline: list[dict], ctx: Context) -> list[dict]:
    """Run a read-only aggregation pipeline against MongoDB to build a report.

    IMPORTANT: The pipeline's field names and structure MUST conform to the
    schema returned by the `data://schema/parent` resource. Read that resource
    first if you haven't already. Only reference fields that match the layout
    of the `data://schema/parent` schema. The pipeline must also only use
    read-only aggregation stages, and cannot modify or delete any data.
    """
    MAX_PIPELINE_TIME_MS = 30 * 1000  # 30 seconds
    validate_pipeline(pipeline)

    db = ctx.lifespan_context["db"]
    coll = db[os.getenv("MONGO_COLLECTION")]
    results = coll.aggregate(pipeline, maxTimeMS=MAX_PIPELINE_TIME_MS)
    return list(results)


@mcp.tool(name="Ping database connection")
def ping(ctx: Context) -> str:
    """Ping the database server to check if it's reachable."""
    db = ctx.lifespan_context["db"]
    try:
        db.command("ping")
        return "Database server is reachable."
    except Exception as e:
        return f"Failed to reach database server: {e}"


if __name__ == "__main__":
    mcp.run()
