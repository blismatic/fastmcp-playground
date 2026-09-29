"""Validation and tenant scoping for LLM-authored MongoDB aggregation pipelines.

Everything here is I/O-free, so it can be unit tested without a database.

The isolation argument this module exists to uphold:

    Let P be the caller's pipeline and G = [{"$match": {"catalog": {"$in": C}}}] + P.
    Stage 0 emits exactly the documents whose catalog is in C. Every allowed stage is a
    pure function of its input stream -- none can read the collection -- and $facet
    branches receive the same already-filtered stream as the $facet stage itself.
    Therefore every byte G produces derives from documents whose catalog is in C.

That only holds if the injected $match is at index 0 and every stage, at every depth, is
on the ALLOWED_STAGES allowlist. Anything not listed -- joins, $unionWith, $documents,
$out, $merge, stages added in future MongoDB versions -- is rejected.
"""

import copy
import datetime
import json
from collections.abc import Iterable, Mapping
from typing import Any

import bson
from bson import Decimal128, ObjectId

from identity import UserPermissions

# The only stages anyone may use. Each is a pure function of its input stream: none can read
# other documents, other collections, or write anything. Only add a stage that meets that bar.
ALLOWED_STAGES = frozenset(
    {
        "$match",
        "$project",
        "$addFields",
        "$set",
        "$unset",
        "$group",
        "$sort",
        "$sortByCount",
        "$limit",
        "$skip",
        "$count",
        "$unwind",
        "$replaceRoot",
        "$replaceWith",
        "$bucket",
        "$bucketAuto",
        "$facet",
    }
)

# Server-side JavaScript and geo/text operators: blocked as DoS / CVE surface, not for isolation.
BANNED_OPERATORS = frozenset(
    {
        "$where",
        "$function",
        "$accumulator",
        "$js",
        "$near",
        "$nearSphere",
        "$text",
    }
)
BANNED_VARIABLES = ("$$USER_ROLES", "$$SEARCH_META")

MAX_PIPELINE_BYTES = 64 * 1024
MAX_STAGES = 40  # counted across all nesting levels
MAX_DEPTH = 4  # top-level pipeline is depth 1
MAX_FACET_BRANCHES = 16
MAX_VALUE_DEPTH = 64  # nesting inside a single stage body

MAX_RESULT_BYTES = 512 * 1024


class PipelineRejected(ValueError):
    """The pipeline failed validation. The message is safe to show the model."""


def build_safe_pipeline(
    pipeline: Any,
    perms: UserPermissions,
) -> list[dict]:
    """Validate `pipeline` and return a new, scoped pipeline ready for `aggregate()`.

    Non-staff callers get `{"$match": {"catalog": {"$in": catalogs}}}` at index 0. The
    returned list never aliases the input, so the object validated is the object executed.
    `pipeline` is typed `Any` on purpose: it's untrusted input, and checking its shape is part
    of this function's job. Raises PipelineRejected with a message safe to show the model.
    """
    if not isinstance(pipeline, list) or not pipeline:
        raise PipelineRejected("The pipeline must be a non-empty list of stages.")

    # Bound the input before walking it, so a deeply nested bomb can't exhaust the stack.
    try:
        size = len(json.dumps(pipeline, default=str).encode("utf-8"))
    except (RecursionError, ValueError):
        raise PipelineRejected("The pipeline is too deeply nested.") from None
    if size > MAX_PIPELINE_BYTES:
        raise PipelineRejected(f"The pipeline is too large ({size} bytes; the limit is {MAX_PIPELINE_BYTES}).")

    if not perms.is_staff and not perms.catalogs:
        raise PipelineRejected("Your account is not authorized for any catalogs.")

    _check_pipeline(pipeline, depth=1, budget=[0])

    safe: list[dict] = []
    if not perms.is_staff:
        safe.append({"$match": {"catalog": {"$in": list(perms.catalogs)}}})
    safe.extend(copy.deepcopy(pipeline))
    return safe


def _check_pipeline(stages: Any, *, depth: int, budget: list[int]) -> None:
    """Validate a pipeline or `$facet` sub-pipeline: stage shape, allowlist, banned operators,
    and the depth and stage-count budgets. `budget` is shared across all nesting levels.
    """
    if depth > MAX_DEPTH:
        raise PipelineRejected(f"Sub-pipelines may be nested at most {MAX_DEPTH} levels deep.")
    if not isinstance(stages, list):
        raise PipelineRejected("Every pipeline and sub-pipeline must be a list of stages.")

    for stage in stages:
        budget[0] += 1
        if budget[0] > MAX_STAGES:
            raise PipelineRejected(f"The pipeline may contain at most {MAX_STAGES} stages, including nested ones.")
        if not isinstance(stage, dict) or len(stage) != 1:
            raise PipelineRejected('Each stage must be an object with exactly one key, e.g. {"$match": {...}}.')

        name, body = next(iter(stage.items()))
        if name not in ALLOWED_STAGES:
            raise PipelineRejected(f"Stage '{name}' is not allowed in reports. Allowed stages: {', '.join(sorted(ALLOWED_STAGES))}.")

        _scan_for_banned(body, depth=0)

        if name == "$facet":
            _check_facet(body, depth=depth, budget=budget)


def _check_facet(body: Any, *, depth: int, budget: list[int]) -> None:
    """Validate a `$facet` body: branch count, branch names, and each branch as a sub-pipeline."""
    if not isinstance(body, dict) or not body:
        raise PipelineRejected("$facet must be a non-empty object of named sub-pipelines.")
    if len(body) > MAX_FACET_BRANCHES:
        raise PipelineRejected(f"$facet may have at most {MAX_FACET_BRANCHES} branches.")
    for branch_name, branch in body.items():
        if not isinstance(branch_name, str) or not branch_name or branch_name.startswith("$") or "." in branch_name:
            raise PipelineRejected(f"Invalid $facet branch name {branch_name!r}: it must be non-empty, not start with '$', and not contain '.'.")
        _check_pipeline(branch, depth=depth + 1, budget=budget)


def _scan_for_banned(node: Any, *, depth: int) -> None:
    """Reject banned operators and variables anywhere inside a stage body, at any nesting depth."""
    if depth > MAX_VALUE_DEPTH:
        raise PipelineRejected("A stage is too deeply nested.")
    if isinstance(node, dict):
        for key, value in node.items():
            if key in BANNED_OPERATORS:
                raise PipelineRejected(f"Operator '{key}' is not allowed in reports.")
            _scan_for_banned(value, depth=depth + 1)
    elif isinstance(node, list):
        for value in node:
            _scan_for_banned(value, depth=depth + 1)
    elif isinstance(node, str):
        for variable in BANNED_VARIABLES:
            if node == variable or node.startswith(variable + "."):
                raise PipelineRejected(f"Variable '{variable}' is not allowed in reports.")


def read_capped(cursor: Iterable[Mapping[str, Any]], max_bytes: int) -> tuple[list, str | None]:
    """Drain `cursor` until the next document would exceed a BSON byte budget.

    The budget is in bytes, not documents, because a document count is defeated by
    `{"$group": {"_id": null, "all": {"$push": "$$ROOT"}}}`, which returns everything in one
    document. Stopping early and closing the cursor also stops the query server-side.
    Returns `(docs, truncation_note_or_None)`.
    """
    docs: list = []
    total_bytes = 0
    for doc in cursor:
        size = len(bson.encode(doc))
        if total_bytes + size > max_bytes:
            return docs, (
                f"Results were truncated at {len(docs)} documents because the next one would exceed the "
                f"{max_bytes // 1024} KB result budget. Add a $limit or $group, $project only the fields you need, and don't $push whole documents."
            )
        docs.append(doc)
        total_bytes += size
    return docs, None


def jsonify(node: Any) -> Any:
    """Recursively convert BSON values into JSON-serializable ones."""
    if isinstance(node, Mapping):
        return {str(key): jsonify(value) for key, value in node.items()}
    if isinstance(node, (list, tuple)):
        return [jsonify(value) for value in node]
    if node is None or isinstance(node, (bool, int, float, str)):
        return node
    if isinstance(node, ObjectId):
        return str(node)
    if isinstance(node, (datetime.datetime, datetime.date)):
        return node.isoformat()
    if isinstance(node, Decimal128):
        return str(node)
    if isinstance(node, bytes):  # includes bson.Binary
        return f"<binary: {len(node)} bytes>"
    return str(node)
