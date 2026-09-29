"""Who is calling, and which catalogs they may read.

The Auth0 `sub` comes from the verified access token. Staff status and catalog access
come from a live DynamoDB read (cached for a short TTL), so permission changes take
effect without the user logging in again.

Everything here fails closed: the only way to get `is_staff=True` is an explicit
"staff"/"superadmin" globalRole in UsersTable, and any lookup failure raises rather than
returning a permissive default.
"""

import functools
import logging
import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime

import cachetools.func
from boto3.dynamodb.types import TypeDeserializer
from boto3.session import Session
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_access_token, get_context

logger = logging.getLogger(__name__)

AUTH_ENABLED = os.getenv("MCP_AUTH_ENABLED", "true").lower() not in ("0", "false", "no")
DEV_SUB = os.getenv("MCP_DEV_SUB") or None
PERMS_TTL_SECONDS = 60

STAFF_ROLES = frozenset({"staff", "superadmin"})
BATCH_GET_MAX_KEYS = 100
BATCH_GET_MAX_RETRIES = 5

_deserializer = TypeDeserializer()  # stateless, safe to share across threads


class PermissionsUnavailableError(ToolError):
    """Permissions could not be determined. Never cached; the caller is denied."""


@dataclass(frozen=True, slots=True)
class UserPermissions:
    """Represents the permissions of a user, including their Auth0 `sub`, staff status, and accessible catalogs."""

    sub: str
    is_staff: bool = False
    catalogs: tuple[str, ...] = ()  # tuple, not list -- it's cached and shared across threads

    @property
    def has_any_access(self) -> bool:
        return self.is_staff or bool(self.catalogs)


# --- Reading the caller ---------------------------------------------------------------------


def get_current_sub() -> str:
    """Return the caller's Auth0 `sub`, or raise ToolError if the caller is not authenticated.
    This may seem unnecessary when FastMCP already ships a `get_access_token` function, but
    it provides a convenient way to get the `sub` claim directly, handling both authenticated and dev bypass scenarios.
    """
    token = get_access_token()
    if token is None:
        # Dev bypass: only with auth explicitly disabled AND on stdio, so an open HTTP server can't ship.
        if not AUTH_ENABLED and DEV_SUB and get_context().transport == "stdio":
            return DEV_SUB
        raise ToolError("This tool requires authentication. Reconnect and sign in with your account.")

    sub = token.subject or token.claims.get("sub")
    if not sub:
        raise ToolError("Your access token has no subject claim. Reconnect to re-authenticate.")
    return sub


# --- DynamoDB -------------------------------------------------------------------------------

@functools.cache
def _dynamo():
    """
    Return a cached DynamoDB client, creating it on first use.
    The client is thread-safe once built. Each call builds from its own Session (boto3's shared
    default session isn't thread-safe), so if two threads race on the first call the worst case is
    an extra client that gets discarded. In practice the server's startup check builds it first.
    """
    return Session().client(
        "dynamodb",
        config=Config(
            retries={"max_attempts": 2, "mode": "standard"},
            connect_timeout=2,
            read_timeout=3,
            max_pool_connections=25,
        ),
    )


@dataclass(frozen=True)
class _Tables:
    """Container for the names of the DynamoDB tables and indexes."""

    users: str
    users_auth0_index: str
    user_orgs: str
    user_orgs_account_index: str
    orgs: str


_TABLE_ENV_VARS = {
    "users": "DYNAMO_USERS_TABLE",
    "users_auth0_index": "DYNAMO_USERS_AUTH0_INDEX",
    "user_orgs": "DYNAMO_USER_ORGS_TABLE",
    "user_orgs_account_index": "DYNAMO_USER_ORGS_ACCOUNT_INDEX",
    "orgs": "DYNAMO_ORGS_TABLE",
}


def _tables() -> _Tables:
    """
    Return a _Tables instance containing the names of the DynamoDB tables and indexes.
    Raises PermissionsUnavailableError if any required table or index is not configured.
    """
    names = {field: os.getenv(env_var) or "" for field, env_var in _TABLE_ENV_VARS.items()}
    missing = [_TABLE_ENV_VARS[field] for field, value in names.items() if not value]
    if missing:
        logger.error("DynamoDB table configuration missing: %s", missing)
        raise PermissionsUnavailableError("The permissions service is not configured. Contact an administrator.")
    return _Tables(**names)


def _lookup_permissions_uncached(sub: str) -> UserPermissions:
    """
    Look up the permissions for a user based on their Auth0 `sub` through the DynamoDB tables.
    Returns a UserPermissions instance. If the user is not found or is ambiguous, returns a default UserPermissions instance.
    """
    tables = _tables()
    client = _dynamo()

    # 1. UsersTable GSI on auth0UserId (the exact, case-preserved sub) -> globalRole, isArchived.
    # A GSI doesn't enforce uniqueness, so fetch up to two rows and deny if the sub is ambiguous.
    rows = client.query(
        TableName=tables.users,
        IndexName=tables.users_auth0_index,
        KeyConditionExpression="#s = :s",
        ProjectionExpression="#r, #x",
        ExpressionAttributeNames={"#s": "auth0UserId", "#r": "globalRole", "#x": "isArchived"},
        ExpressionAttributeValues={":s": {"S": sub}},
        Limit=2,
    ).get("Items", [])
    if not rows:
        return UserPermissions(sub)
    if len(rows) > 1:
        logger.warning("Multiple UsersTable rows share auth0UserId %r; denying", sub)
        return UserPermissions(sub)
    item = rows[0]
    if "isArchived" in item and _deserializer.deserialize(item["isArchived"]) is True:
        return UserPermissions(sub)
    role = _deserializer.deserialize(item["globalRole"]) if "globalRole" in item else None
    if role in STAFF_ROLES:
        return UserPermissions(sub, is_staff=True)  # catalogs irrelevant -- full access

    # 2. UserOrganizationsTable GSI on accountId (the exact, case-preserved sub) -> organizationId.
    # A user can be in many orgs.
    org_keys: dict[object, dict] = {}
    for page in client.get_paginator("query").paginate(
        TableName=tables.user_orgs,
        IndexName=tables.user_orgs_account_index,
        KeyConditionExpression="#a = :a",
        ProjectionExpression="#o",
        ExpressionAttributeNames={"#a": "accountId", "#o": "organizationId"},
        ExpressionAttributeValues={":a": {"S": sub}},
    ):
        for row in page.get("Items", []):
            if "organizationId" in row:
                org_keys[_deserializer.deserialize(row["organizationId"])] = row["organizationId"]

    # 3. OrganizationsTable: id (PK) in org ids -> catalogName
    catalogs: set[str] = set()
    raw_ids = list(org_keys.values())
    for start in range(0, len(raw_ids), BATCH_GET_MAX_KEYS):
        pending = {
            tables.orgs: {
                "Keys": [{"id": raw_id} for raw_id in raw_ids[start : start + BATCH_GET_MAX_KEYS]],
                "ProjectionExpression": "#c",
                "ExpressionAttributeNames": {"#c": "catalogName"},
            }
        }
        for attempt in range(BATCH_GET_MAX_RETRIES + 1):
            response = client.batch_get_item(RequestItems=pending)
            for org in response.get("Responses", {}).get(tables.orgs, []):
                if "catalogName" in org:
                    name = _deserializer.deserialize(org["catalogName"])
                    if isinstance(name, str) and name:
                        catalogs.add(name)
            pending = response.get("UnprocessedKeys") or {}
            if not pending:
                break
            time.sleep(0.05 * 2**attempt)
        else:
            logger.error("BatchGetItem left unprocessed keys after %d retries", BATCH_GET_MAX_RETRIES)
            raise PermissionsUnavailableError("The permissions service is busy. Try again shortly.")

    return UserPermissions(sub, is_staff=False, catalogs=tuple(sorted(catalogs)))


# ttl_cache is lock-protected (unlike the bare TTLCache class) and caches return values only,
# so a transient DynamoDB failure is never pinned in the cache.
@cachetools.func.ttl_cache(maxsize=2048, ttl=PERMS_TTL_SECONDS)
def get_user_permissions(sub: str) -> UserPermissions:
    """
    Retrieve the permissions for a user based on their Auth0 `sub`.
    This function uses a TTL cache to store the results of the lookup.
    Raises PermissionsUnavailableError if the permissions service is unavailable.
    """
    try:
        return _lookup_permissions_uncached(sub)
    except (BotoCoreError, ClientError):
        logger.exception("DynamoDB permissions lookup failed")
        raise PermissionsUnavailableError("The permissions service is unavailable. Try again shortly.") from None


def require_catalog_scope() -> UserPermissions:
    """The single enforcement entry point: authenticated caller with access to at least one catalog."""
    perms = get_user_permissions(get_current_sub())
    if not perms.has_any_access:
        raise ToolError("Your account is not authorized for any catalogs. Contact an administrator if this is wrong.")
    return perms


def warm_up_permissions_backend() -> None:
    """
    Ensure the DynamoDB permissions backend is reachable and properly configured.
    This function is intended to be called at application startup to fail early if there are misconfigurations.
    Raises RuntimeError if the backend is unreachable or misconfigured.
    """
    tables = _tables()
    client = _dynamo()
    required_indexes = {
        tables.users: tables.users_auth0_index,
        tables.user_orgs: tables.user_orgs_account_index,
        tables.orgs: None,
    }
    try:
        for name, index_name in required_indexes.items():
            description = client.describe_table(TableName=name)["Table"]
            if index_name is None:
                continue
            statuses = {index["IndexName"]: index.get("IndexStatus") for index in description.get("GlobalSecondaryIndexes", [])}
            if statuses.get(index_name) != "ACTIVE":
                raise RuntimeError(f"Index {index_name!r} on table {name!r} is missing or not ACTIVE (status: {statuses.get(index_name)})")
    except (BotoCoreError, ClientError) as exc:
        raise RuntimeError(f"DynamoDB permissions backend is unreachable or misconfigured: {exc}") from exc


def describe_current_identity() -> dict:
    """Diagnostic snapshot of the caller. Never raises; failures are reported in the result."""
    info: dict = {
        "authenticated": False,
        "auth_mode": "auth0" if AUTH_ENABLED else "dev-bypass",
        "sub": None,
        "token_expires_at_iso": None,
        "token_expires_in_seconds": None,
        "is_staff": False,
        "all_catalogs": False,
        "catalogs": None,
        "access": "No report access.",
        "permissions_error": None,
    }
    token = get_access_token()
    if token is not None:  # never include token.token itself
        info["authenticated"] = True
        if token.expires_at is not None:
            info["token_expires_at_iso"] = datetime.fromtimestamp(token.expires_at, UTC).isoformat()
            info["token_expires_in_seconds"] = int(token.expires_at - time.time())

    try:
        info["sub"] = get_current_sub()
        perms = get_user_permissions(info["sub"])
    except ToolError as exc:
        info["permissions_error"] = str(exc)
        info["access"] = f"No report access: {exc}"
        return info

    # Staff get `catalogs: None`, never `[]`: agents read an empty list as "no access".
    # `access` states the scope in plain language so it can't be misread.
    if perms.is_staff:
        info.update(
            is_staff=True,
            all_catalogs=True,
            access="Staff: reports cover every catalog; results are not filtered by catalog.",
        )
    elif perms.catalogs:
        info.update(catalogs=list(perms.catalogs), access=f"Reports only include these catalogs: {', '.join(perms.catalogs)}.")
    else:
        info.update(catalogs=[], access="No report access: this account is not authorized for any catalogs.")
    return info
