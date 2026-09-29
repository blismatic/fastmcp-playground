import pytest

import identity

SUB = "waad|MixedCaseSub123"


class FakeDynamo:
    """Just enough of the DynamoDB client for _lookup_permissions_uncached."""

    def __init__(self, user_rows, org_ids=(), catalogs=None):
        self.user_rows = user_rows
        self.org_ids = org_ids
        self.catalogs = catalogs or {}
        self.queried_subs = []

    def query(self, **kwargs):
        self.queried_subs.append(kwargs["ExpressionAttributeValues"][":s"]["S"])
        return {"Items": self.user_rows[: kwargs["Limit"]]}

    def get_paginator(self, _name):
        rows = [{"organizationId": {"S": org_id}} for org_id in self.org_ids]
        return type("Paginator", (), {"paginate": lambda _self, **_kw: [{"Items": rows}]})()

    def batch_get_item(self, RequestItems):
        (table, request), = RequestItems.items()
        orgs = [{"catalogName": {"S": self.catalogs[key["id"]["S"]]}} for key in request["Keys"] if key["id"]["S"] in self.catalogs]
        return {"Responses": {table: orgs}}


@pytest.fixture
def use_fake(monkeypatch):
    for env_var in identity._TABLE_ENV_VARS.values():
        monkeypatch.setenv(env_var, env_var.lower())

    def install(fake):
        monkeypatch.setattr(identity, "_dynamo", lambda: fake)
        return fake

    return install


def test_queries_the_index_with_the_exact_sub(use_fake):
    fake = use_fake(FakeDynamo([{"globalRole": {"S": "staff"}}]))
    assert identity._lookup_permissions_uncached(SUB).is_staff
    assert fake.queried_subs == [SUB]


def test_unknown_sub_gets_no_access(use_fake):
    use_fake(FakeDynamo([]))
    assert not identity._lookup_permissions_uncached(SUB).has_any_access


def test_duplicate_auth0_user_id_is_denied(use_fake):
    use_fake(FakeDynamo([{"globalRole": {"S": "staff"}}, {"globalRole": {"S": "staff"}}]))
    assert not identity._lookup_permissions_uncached(SUB).has_any_access


def test_archived_user_is_denied(use_fake):
    use_fake(FakeDynamo([{"globalRole": {"S": "staff"}, "isArchived": {"BOOL": True}}]))
    assert not identity._lookup_permissions_uncached(SUB).has_any_access


def test_non_archived_non_staff_gets_their_catalogs(use_fake):
    use_fake(FakeDynamo([{"isArchived": {"BOOL": False}}], org_ids=["o1", "o2"], catalogs={"o1": "PKCap", "o2": "Renovo"}))
    perms = identity._lookup_permissions_uncached(SUB)
    assert not perms.is_staff and perms.catalogs == ("PKCap", "Renovo")


@pytest.fixture
def whoami_as(monkeypatch):
    """Run describe_current_identity() for a caller with the given permissions, no token or DynamoDB."""

    def run(perms):
        monkeypatch.setattr(identity, "get_access_token", lambda: None)
        monkeypatch.setattr(identity, "get_current_sub", lambda: perms.sub)
        monkeypatch.setattr(identity, "get_user_permissions", lambda _sub: perms)
        return identity.describe_current_identity()

    return run


def test_staff_access_says_all_catalogs_not_an_empty_list(whoami_as):
    info = whoami_as(identity.UserPermissions(SUB, is_staff=True))
    assert info["is_staff"] is True and info["all_catalogs"] is True
    assert info["catalogs"] is None  # never [], which agents read as "no access"
    assert "every catalog" in info["access"]


def test_non_staff_access_lists_only_their_catalogs(whoami_as):
    info = whoami_as(identity.UserPermissions(SUB, catalogs=("PKCap", "Renovo")))
    assert info["all_catalogs"] is False
    assert info["catalogs"] == ["PKCap", "Renovo"]
    assert "PKCap, Renovo" in info["access"]


def test_no_access_is_stated_plainly(whoami_as):
    info = whoami_as(identity.UserPermissions(SUB))
    assert info["all_catalogs"] is False and info["catalogs"] == []
    assert info["access"].startswith("No report access")


def test_permissions_failure_is_reported_not_raised(monkeypatch):
    def unavailable(_sub):
        raise identity.PermissionsUnavailableError("The permissions service is unavailable. Try again shortly.")

    monkeypatch.setattr(identity, "get_access_token", lambda: None)
    monkeypatch.setattr(identity, "get_current_sub", lambda: SUB)
    monkeypatch.setattr(identity, "get_user_permissions", unavailable)
    info = identity.describe_current_identity()
    assert info["permissions_error"] and info["catalogs"] is None and info["all_catalogs"] is False
    assert info["access"].startswith("No report access")
