import copy
import datetime
import json

import bson
import pytest
from bson import Binary, Decimal128, ObjectId

from identity import UserPermissions
from pipeline_guard import (
    ALLOWED_STAGES,
    MAX_RESULT_BYTES,
    PipelineRejected,
    build_safe_pipeline,
    jsonify,
    read_capped,
)

COLL = "schemaJsonRecords"


USER = UserPermissions("auth0|user", catalogs=("CloudMaven", "InstaFi"))
STAFF = UserPermissions("auth0|staff", is_staff=True)
ACCESS_FILTER = {"$match": {"catalog": {"$in": ["CloudMaven", "InstaFi"]}}}


def build(pipeline, perms=USER):
    return build_safe_pipeline(pipeline, perms)


def nested_facets(levels: int) -> list:
    pipeline = [{"$match": {}}]
    for _ in range(levels):
        pipeline = [{"$facet": {"a": pipeline}}]
    return pipeline


# --- Rejections -----------------------------------------------------------------------------

LOOKUP_CLASSIC = {"$lookup": {"from": COLL, "localField": "record_id", "foreignField": "record_id", "as": "leak"}}
LOOKUP_UNCORRELATED = {"$lookup": {"from": COLL, "pipeline": [], "as": "leak"}}
LOOKUP_CORRELATED = {
    "$lookup": {"from": COLL, "let": {"r": "$record_id"}, "pipeline": [{"$match": {"$expr": {"$ne": ["$record_id", "$$r"]}}}], "as": "leak"}
}

REJECTED_FOR_USERS = {
    "lookup classic": [LOOKUP_CLASSIC],
    "lookup uncorrelated": [LOOKUP_UNCORRELATED],
    "lookup correlated": [LOOKUP_CORRELATED],
    "lookup inside facet": [{"$facet": {"a": [LOOKUP_UNCORRELATED]}}],
    "unionWith inside facet": [{"$facet": {"a": [{"$unionWith": {"coll": COLL, "pipeline": []}}]}}],
    "unionWith top level": [{"$unionWith": COLL}],
    "graphLookup": [{"$graphLookup": {"from": COLL, "startWith": "$x", "connectFromField": "x", "connectToField": "y", "as": "z"}}],
    "documents": [{"$documents": [{"catalog": "Ursara"}]}],
    "search": [{"$search": {"text": {"query": "x", "path": "y"}}}],
    "collStats": [{"$collStats": {"count": {}}}],
    "currentOp": [{"$currentOp": {}}],
    "sample": [{"$sample": {"size": 5}}],
    "multi-key stage": [{"$match": {}, "$out": "x"}],
    "empty stage": [{}],
    "string stage": ["$match"],
    "null stage": [None],
    "function": [{"$addFields": {"x": {"$function": {"body": "function() {}", "args": [], "lang": "js"}}}}],
    "accumulator": [{"$group": {"_id": None, "x": {"$accumulator": {}}}}],
    "where": [{"$match": {"$where": "true"}}],
    "function inside facet": [{"$facet": {"a": [{"$addFields": {"x": {"$function": {}}}}]}}],
    "where inside facet": [{"$facet": {"a": [{"$match": {"$where": "true"}}]}}],
    "text": [{"$match": {"$text": {"$search": "x"}}}],
    "user roles": [{"$addFields": {"r": "$$USER_ROLES"}}],
    "user roles subfield": [{"$addFields": {"r": "$$USER_ROLES.role"}}],
    "search meta": [{"$addFields": {"r": "$$SEARCH_META"}}],
    "depth-6 facet chain": nested_facets(6),
    "60 stages": [{"$match": {}}] * 60,
    "10x10 nested stages": [{"$facet": {f"b{i}": [{"$match": {}}] * 10 for i in range(10)}}],
    "too many facet branches": [{"$facet": {f"b{i}": [{"$match": {}}] for i in range(17)}}],
    "oversize pipeline": [{"$match": {"x": "a" * (65 * 1024)}}],
    "facet branch $evil": [{"$facet": {"$evil": [{"$match": {}}]}}],
    "facet branch a.b": [{"$facet": {"a.b": [{"$match": {}}]}}],
    "facet branch empty": [{"$facet": {"": [{"$match": {}}]}}],
    "facet branch not a list": [{"$facet": {"a": {"$match": {}}}}],
    "facet not an object": [{"$facet": [[{"$match": {}}]]}],
    "unknown stage": [{"$densify": {}}],
    "not a list": {"$match": {}},
    "empty pipeline": [],
}


@pytest.mark.parametrize("perms", [USER, STAFF], ids=["user", "staff"])
@pytest.mark.parametrize("pipeline", REJECTED_FOR_USERS.values(), ids=REJECTED_FOR_USERS.keys())
def test_rejected_for_everyone(pipeline, perms):
    with pytest.raises(PipelineRejected):
        build(pipeline, perms)


def test_rejection_names_the_stage_and_lists_allowed_ones():
    with pytest.raises(PipelineRejected, match=r"Stage '\$lookup' is not allowed.*Allowed stages: .*\$match"):
        build([LOOKUP_UNCORRELATED], STAFF)


WRITES = {
    "out": [{"$out": "x"}],
    "merge": [{"$merge": {"into": "x"}}],
    "out inside facet": [{"$facet": {"a": [{"$out": "x"}]}}],
    "merge inside lookup pipeline": [{"$lookup": {"from": COLL, "pipeline": [{"$merge": {"into": "x"}}], "as": "y"}}],
}


@pytest.mark.parametrize("perms", [USER, STAFF], ids=["user", "staff"])
@pytest.mark.parametrize("pipeline", WRITES.values(), ids=WRITES.keys())
def test_writes_rejected_for_everyone(pipeline, perms):
    with pytest.raises(PipelineRejected):
        build(pipeline, perms)


def test_empty_catalogs_without_staff_is_rejected():
    with pytest.raises(PipelineRejected, match="not authorized"):
        build([{"$match": {}}], UserPermissions("auth0|nobody"))


def test_deeply_nested_values_are_rejected_not_crashed():
    value: object = 1
    for _ in range(5000):
        value = [value]
    with pytest.raises(PipelineRejected):
        build([{"$match": {"x": value}}])


# --- Acceptances ----------------------------------------------------------------------------

ACCEPTED = {
    "group by catalog": [{"$group": {"_id": "$catalog", "n": {"$sum": 1}}}],
    "project drops catalog": [{"$project": {"catalog": 0}}, {"$group": {"_id": None, "n": {"$sum": 1}}}],
    "facet": [{"$facet": {"count": [{"$count": "n"}], "recent": [{"$sort": {"record.lastModified": -1}}, {"$limit": 5}]}}],
    "new stream-local stages": [{"$set": {"a": 1}}, {"$unset": "a"}, {"$sortByCount": "$catalog"}],
    "replaceWith and bucket": [{"$replaceWith": "$record"}, {"$bucket": {"groupBy": "$n", "boundaries": [0, 10]}}],
    "expr is fine": [{"$match": {"$expr": {"$gt": ["$a", "$b"]}}}],
    "depth-3 facet chain": nested_facets(3),
}


@pytest.mark.parametrize("pipeline", ACCEPTED.values(), ids=ACCEPTED.keys())
def test_accepted_pipelines_are_scoped(pipeline):
    out = build(pipeline)
    assert out[0] == ACCESS_FILTER
    assert out[1:] == pipeline


def test_staff_get_no_injected_match():
    pipeline = [{"$group": {"_id": "$catalog", "n": {"$sum": 1}}}]
    assert build(pipeline, STAFF) == pipeline


def test_input_is_not_mutated_or_aliased():
    pipeline = [{"$facet": {"a": [{"$match": {"x": [1, 2]}}]}}]
    original = copy.deepcopy(pipeline)
    out = build(pipeline)
    assert pipeline == original
    # Mutating the input after validation must not change what gets executed.
    pipeline[0]["$facet"]["a"].append(LOOKUP_UNCORRELATED)
    pipeline[0]["$facet"]["a"][0]["$match"]["x"].append(3)
    assert out[1] == original[0]


# --- Invariants: written independently of the module's own walker -----------------------------


def all_stage_names(pipeline: list) -> list[str]:
    names = []
    stack = [pipeline]
    while stack:
        current = stack.pop()
        for stage in current:
            (name, body), = stage.items()
            names.append(name)
            if name == "$facet":
                stack.extend(body.values())
    return names


def candidate_pipelines():
    yield from ACCEPTED.values()
    yield from REJECTED_FOR_USERS.values()
    yield from WRITES.values()


def test_every_accepted_pipeline_starts_with_the_access_filter():
    accepted = 0
    for pipeline in candidate_pipelines():
        try:
            out = build(pipeline)
        except PipelineRejected:
            continue
        accepted += 1
        assert out[0] == ACCESS_FILTER
    assert accepted == len(ACCEPTED)


def test_every_output_stage_is_allowlisted():
    for perms in (USER, STAFF):
        for pipeline in candidate_pipelines():
            try:
                out = build(pipeline, perms)
            except PipelineRejected:
                continue
            assert set(all_stage_names(out)) <= ALLOWED_STAGES


# Stages that would break catalog isolation (read other documents/collections) or write data.
# Kept here, not in the module: the module only needs its allowlist, but this catches someone
# adding one of these to it.
ISOLATION_BREAKING_STAGES = {
    "$lookup", "$unionWith", "$graphLookup", "$documents", "$sample", "$search", "$searchMeta",
    "$vectorSearch", "$changeStream", "$collStats", "$indexStats", "$planCacheStats", "$queryStats",
    "$listSessions", "$listLocalSessions", "$listSearchIndexes", "$listSampledQueries",
    "$currentOp", "$shardedDataDistribution", "$out", "$merge",
}


def test_allowlist_contains_no_isolation_breaking_stage():
    assert not ALLOWED_STAGES & ISOLATION_BREAKING_STAGES


# --- Result caps and BSON coercion ----------------------------------------------------------


def test_one_huge_document_is_truncated():
    huge = {"_id": None, "all": [{"blob": "x" * 1024} for _ in range(5 * 1024)]}  # ~5 MB, like $push: "$$ROOT"
    docs, note = read_capped([huge], MAX_RESULT_BYTES)
    assert docs == [] and note is not None


def test_many_small_documents_are_capped_by_bytes():
    docs, note = read_capped(({"i": i} for i in range(1_000_000)), MAX_RESULT_BYTES)
    assert note is not None
    assert sum(len(bson.encode(d)) for d in docs) <= MAX_RESULT_BYTES


def test_byte_budget_stops_before_the_crossing_document():
    small = [{"s": "x" * 1000} for _ in range(3)]
    docs, note = read_capped(small + [{"s": "x" * 5000}, {"s": "y"}], max_bytes=5000)
    assert docs == small and note is not None


def test_small_results_are_not_truncated():
    docs, note = read_capped([{"a": 1}, {"a": 2}, {"a": 3}], MAX_RESULT_BYTES)
    assert len(docs) == 3 and note is None


def test_exactly_filling_the_budget_is_not_truncated():
    docs = [{"s": "x" * 100} for _ in range(4)]
    budget = sum(len(bson.encode(d)) for d in docs)
    out, note = read_capped(docs, max_bytes=budget)
    assert out == docs and note is None


def test_jsonify_makes_bson_values_serializable():
    doc = {
        "_id": ObjectId(),
        "record": {"lastModified": datetime.datetime(2026, 9, 23, 12, 0, tzinfo=datetime.UTC)},
        "amount": Decimal128("12.50"),
        "blob": Binary(b"\x00\x01"),
        "nested": [{"id": ObjectId()}],
    }
    out = jsonify(doc)
    json.dumps(out)
    assert out["record"]["lastModified"] == "2026-09-23T12:00:00+00:00"
    assert out["amount"] == "12.50"
