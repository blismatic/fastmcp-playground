import asyncio
import json
import logging
import os
from pathlib import Path

from fastmcp import Context, FastMCP
from fastmcp.apps.generative import GenerativeUI
from fastmcp.exceptions import ToolError
from fastmcp.server.auth.providers.auth0 import Auth0Provider
from fastmcp.server.lifespan import lifespan
from fastmcp.tools.base import ToolResult
from fastmcp.utilities.types import Image
from mcp.types import Icon, ToolAnnotations
from pymongo import MongoClient
from pymongo.errors import OperationFailure
from starlette.requests import Request
from starlette.responses import HTMLResponse

from audit_logging import AUDIT_LOG, AuditLogging, audit
from identity import AUTH_ENABLED, describe_current_identity, get_current_sub, require_catalog_scope, warm_up_permissions_backend
from pipeline_guard import MAX_RESULT_BYTES, PipelineRejected, build_safe_pipeline, jsonify, read_capped
from report_limits import ReportLimits

# Logs go to stderr -- never stdout, which carries the MCP protocol on stdio. Third-party libraries
# stay at WARNING; LOG_LEVEL applies only to our own modules, so DEBUG doesn't flood the output.
# FastMCP's own messages are separate, controlled by FASTMCP_LOG_LEVEL.
logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
for _name in ("__main__", "my_server", "identity", "pipeline_guard", "report_limits", "audit_logging"):
    logging.getLogger(_name).setLevel(os.getenv("LOG_LEVEL", "INFO"))

# Optional, for local dev: also write logs to files. LOG_DIR unset means no files. No rotation, because
# Claude Desktop runs several copies of a stdio server and Windows can't rename a file another one has open.
if log_dir := os.getenv("LOG_DIR"):
    log_path = Path(__file__).parent / log_dir  # relative paths are relative to the project folder
    log_path.mkdir(parents=True, exist_ok=True)

    text_file = logging.FileHandler(log_path / "server.log", encoding="utf-8")
    text_file.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.getLogger().addHandler(text_file)  # our modules + third-party warnings
    logging.getLogger("fastmcp").addHandler(text_file)  # FastMCP's own messages (it doesn't propagate)

    audit_file = logging.FileHandler(log_path / "audit.jsonl", encoding="utf-8")
    audit_file.setFormatter(logging.Formatter("%(message)s"))
    AUDIT_LOG.addHandler(audit_file)

logger = logging.getLogger(__name__)

MAX_PIPELINE_TIME_SECONDS = 20


@lifespan
async def mongodb_lifespan(server):
    client = MongoClient(
        f"mongodb+srv://{os.environ['MONGO_USER']}:{os.environ['MONGO_PASSWORD']}@{os.environ['MONGO_CLUSTER']}/?retryWrites=true&w=majority"
    )
    try:
        db = client[os.environ["MONGO_DB"]]
        collection = db[os.environ["MONGO_COLLECTION"]]
        await asyncio.to_thread(warm_up_permissions_backend)
        yield {"db": db, "collection": collection}
    finally:
        client.close()


def build_auth() -> Auth0Provider | None:
    if not AUTH_ENABLED:
        logger.warning("MCP_AUTH_ENABLED is false -- server is UNAUTHENTICATED. Local dev only.")
        return None

    return Auth0Provider(
        config_url=os.environ["AUTH0_CONFIG_URL"],
        client_id=os.environ["AUTH0_CLIENT_ID"],
        client_secret=os.environ["AUTH0_CLIENT_SECRET"],
        audience=os.environ["AUTH0_AUDIENCE"],
        base_url=os.environ["AUTH0_BASE_URL"],
        required_scopes=["openid"],
        forward_resource=False,
    )


# Generate a data URI from a local image file
img_light = Image(path=Path(__file__).parent / "assets" / "brand" / "Bolt-Black.svg")
icon_light = Icon(src=img_light.to_data_uri(), theme="light")
img_dark = Image(path=Path(__file__).parent / "assets" / "brand" / "Bolt-White.svg")
icon_dark = Icon(src=img_dark.to_data_uri(), theme="dark")

mcp = FastMCP(
    "Lightning Docs MCP",
    website_url="https://lightningdocs.ai",
    lifespan=mongodb_lifespan,
    icons=[icon_light, icon_dark],
    auth=build_auth(),
)
mcp.add_provider(GenerativeUI())
# Middleware runs in the order added: audit logging first, so rate-limited calls are logged too.
mcp.add_middleware(AuditLogging())
mcp.add_middleware(ReportLimits())


@mcp.custom_route("/", methods=["GET"])
async def setup_page(request: Request) -> HTMLResponse:
    """Serve a static HTML page for setup instructions."""
    html_file = (Path(__file__).parent / "setup.html").read_text(encoding="utf-8")
    return HTMLResponse(html_file)


@mcp.tool(name="get_report_schema", annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True))
def get_report_schema() -> ToolResult:
    """Return the JSON schema of the documents that `run_report` queries.

    Call this once per conversation before writing a pipeline: every field name and the nesting in a
    pipeline must match this schema. The record's own data is under `record.data`. Fields marked
    DEPRECATED in their `usage` notes still hold data in older records; the note says where newer data
    lives, so check both places.
    """
    schemas = Path(__file__).parent / "schemas"
    parent = json.loads((schemas / "parent.json").read_text(encoding="utf-8"))
    parent["properties"]["record"]["properties"]["data"] = json.loads((schemas / "data_model_schema.json").read_text(encoding="utf-8"))
    # Text only (no structured copy), so clients don't receive this ~44k-token schema twice.
    return ToolResult(content=json.dumps(parent, separators=(",", ":")))


def _explain_mongo_failure(exc: OperationFailure) -> str:
    if exc.code == 50:
        return f"The report took longer than {MAX_PIPELINE_TIME_SECONDS} seconds and was stopped. Narrow the $match or add a $limit earlier in the pipeline."
    if exc.code == 292:
        return "The report ran out of memory. Narrow the $match, or $project away unneeded fields before $sort/$group."
    if exc.code == 10334:
        return "A result document exceeded 16 MB. Don't $push whole documents; $project only the fields you need."
    if exc.code == 13:
        return "The database rejected this report as unauthorized."
    message = (exc.details or {}).get("errmsg") or str(exc)
    return f"MongoDB rejected the pipeline: {message}"


@mcp.tool(name="run_report", annotations=ToolAnnotations(read_only_hint=True))
def run_report(pipeline: list[dict], ctx: Context) -> dict:
    """Run a read-only aggregation pipeline against MongoDB to build a report.

    IMPORTANT: The pipeline's field names and structure MUST conform to the
    schema returned by the `get_report_schema` tool. Call it first if you
    haven't already in this conversation, and only reference fields from it.

    Allowed stages: $match, $project, $addFields, $set, $unset, $group, $sort,
    $sortByCount, $limit, $skip, $count, $unwind, $replaceRoot, $replaceWith,
    $bucket, $bucketAuto, $facet. Joins ($lookup, $unionWith, $graphLookup) and
    server-side JavaScript ($where, $function, $accumulator) are not available.

    Results are automatically limited to the catalogs the user can access, so
    don't add your own access filters; `whoami` shows the scope. For staff
    (`all_catalogs: true`), results are not filtered by catalog at all.
    At most 512 KB of results are returned. If `truncated` is true, `note`
    explains why: aggregate further or $project fewer fields rather than
    reporting partial results as complete.
    """
    perms = require_catalog_scope()
    try:
        safe = build_safe_pipeline(pipeline, perms)
    except PipelineRejected as exc:
        raise ToolError(str(exc)) from None

    coll = ctx.lifespan_context["collection"]
    try:
        with coll.aggregate(
            safe,
            maxTimeMS=MAX_PIPELINE_TIME_SECONDS * 1000,
            allowDiskUse=False,
            batchSize=200,
            comment=f"mcp:run_report sub={perms.sub}",  # tags the query in MongoDB logs / profiler / currentOp
        ) as cursor:
            docs, truncation = read_capped(cursor, MAX_RESULT_BYTES)
    except OperationFailure as exc:
        raise ToolError(_explain_mongo_failure(exc)) from None
    audit("report_result", returned_count=len(docs), truncated=truncation is not None)
    return {"results": jsonify(docs), "returned_count": len(docs), "truncated": truncation is not None, "note": truncation}


@mcp.tool(name="whoami", annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True))
def whoami() -> dict:
    """Report the caller's identity, token status, and data access scope.

    `access` states the scope in plain language. If `all_catalogs` is true (staff), `run_report`
    covers every catalog and `catalogs` is null. Otherwise `catalogs` lists the only catalogs
    reports can include.
    """
    return describe_current_identity()


@mcp.tool(name="ping_database_connection")
def ping(ctx: Context) -> str:
    """Ping the database server to check if it's reachable."""
    get_current_sub()
    db = ctx.lifespan_context["db"]
    try:
        db.command("ping")
        return "Database server is reachable."
    except Exception as e:
        return f"Failed to reach database server: {e}"


if __name__ == "__main__":
    mcp.run()
