"""Offline tests for tenant-queryable node types.

The graph schema lists more types than a tenant will run: observed live, 335 of
898 schema types were rejected by VQL with 400 "not a valid NodeType" because
their integration is not enabled. nl2vql emitted one (CustomHRISEmployee) and a
schema-only validator waved it through. vql:autocomplete after `SHOW ` returns
the names the tenant really accepts.
"""

from __future__ import annotations

import json

import httpx
import pytest

from veza_query_mcp import schema as schemamod
from veza_query_mcp.client import VezaClient, VezaConfig, VezaError
from veza_query_mcp.repair import repair, repair_until_valid
from veza_query_mcp.schema import NodeType, SchemaIndex
from veza_query_mcp.vql import validate


def _nt(t, props=(), reach=(), labels=()):
    return NodeType(
        type=t, name=t, group="G", integration="test",
        labels=list(labels) or [t],
        properties={p: {"type": None, "optional": None} for p in props},
        reachable=set(reach),
    )


def _index(queryable: set[str] | None) -> SchemaIndex:
    nodes = {
        "OktaUser": _nt("OktaUser", ["id", "is_active"], reach=["OktaGroup"],
                        labels=["OktaUser", "Identity", "User"]),
        "OktaGroup": _nt("OktaGroup", ["id"], labels=["OktaGroup", "Group"]),
        # In the schema, not enabled on the tenant:
        "CustomHRISEmployee": _nt("CustomHRISEmployee", ["employment_status"],
                                  labels=["CustomHRISEmployee", "Identity", "User", "HRISUser"]),
        # Enabled, same narrow grouping:
        "WorkdayWorker": _nt("WorkdayWorker", ["hire_date"],
                             labels=["WorkdayWorker", "Identity", "User", "HRISUser"]),
        "AnthropicUser": _nt("AnthropicUser", ["id"], labels=["AnthropicUser", "Identity", "User"]),
    }
    return SchemaIndex(nodes, fetched_at=0.0, queryable=queryable)


QUERYABLE = {"OktaUser", "OktaGroup", "WorkdayWorker", "Identity", "User"}


@pytest.fixture
def idx() -> SchemaIndex:
    return _index(QUERYABLE)


# ── index ──────────────────────────────────────────────────────────────────

def test_is_queryable(idx):
    assert idx.is_queryable("OktaUser") is True
    assert idx.is_queryable("oktauser") is True        # casing-insensitive lookup
    assert idx.is_queryable("CustomHRISEmployee") is False


def test_unknown_queryability_is_none_not_false():
    assert _index(None).is_queryable("CustomHRISEmployee") is None


def test_search_hides_types_the_tenant_cannot_run(idx):
    assert [m["type"] for m in idx.search("customhris")] == []
    assert [m["type"] for m in idx.search("customhris", queryable_only=False)] == ["CustomHRISEmployee"]


def test_search_is_unfiltered_when_queryability_unknown():
    assert [m["type"] for m in _index(None).search("customhris")] == ["CustomHRISEmployee"]


def test_alternatives_use_the_narrowest_shared_grouping(idx):
    # 'Identity'/'User' are shared with OktaUser too; HRISUser is the specific one.
    assert idx.alternatives("CustomHRISEmployee") == ["WorkdayWorker"]


def test_stats_report_the_gap(idx):
    s = idx.stats()
    assert s["node_types"] == 5
    assert s["queryable_node_types"] == 3          # OktaUser, OktaGroup, WorkdayWorker
    assert s["schema_only_node_types"] == 2


# ── validation ─────────────────────────────────────────────────────────────

def test_validate_rejects_a_schema_type_the_tenant_cannot_run(idx):
    """The live regression: schema-valid, but Veza answers 400."""
    r = validate("SHOW CustomHRISEmployee WHERE employment_status = 'terminated' LIMIT 5;", idx)
    assert not r["valid"]
    err = r["errors"][0]
    assert "not queryable on this tenant" in err["message"]
    assert "WorkdayWorker" in err["fix"]


def test_validate_does_not_cascade_attribute_errors_from_a_rejected_type(idx):
    r = validate("SHOW CustomHRISEmployee WHERE bogus = 1 LIMIT 5;", idx)
    assert len(r["errors"]) == 1


def test_validate_flags_a_non_queryable_destination(idx):
    r = validate("SHOW OktaUser RELATED TO CustomHRISEmployee LIMIT 5;", idx)
    assert any("not queryable" in e["message"] for e in r["errors"])


def test_validate_unchanged_when_queryability_unknown():
    r = validate("SHOW CustomHRISEmployee LIMIT 5;", _index(None))
    assert r["valid"]


# ── repair ─────────────────────────────────────────────────────────────────

def test_repair_refuses_with_alternatives_instead_of_passing_it_through(idx):
    r = repair("SHOW CustomHRISEmployee LIMIT 5;", idx)
    assert "CustomHRISEmployee" in r.query, "must not silently swap the type"
    assert r.refusals and "WorkdayWorker" in r.refusals[0].detail


def test_repair_reports_the_refusal_once(idx):
    out = repair_until_valid("SHOW CustomHRISEmployee", idx, limit=50)
    assert not out["valid"]
    assert len(out["blocked_by"]) == 1


def test_substitution_candidates_exclude_non_queryable_types():
    # 'CustomHRISEmploye' is one letter off a non-queryable type; it must not be
    # "corrected" into something the tenant would reject.
    r = repair("SHOW CustomHRISEmploye LIMIT 5;", _index(QUERYABLE))
    assert "CustomHRISEmployee" not in r.query


# ── client + cache ─────────────────────────────────────────────────────────

def _client_with(handler) -> VezaClient:
    c = VezaClient(VezaConfig(base_url="https://t.example", api_key="k"))
    c._client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://t.example")
    return c


def test_autocomplete_sends_cursor_position():
    """Without it the server ignores the query and always answers ["SHOW"]."""
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen.update(json.loads(req.content))
        return httpx.Response(200, json={"suggestions": []})

    _client_with(handler).vql_autocomplete("SHOW OktaUser WHERE ")
    assert seen == {"query": "SHOW OktaUser WHERE ", "cursor_position": 20}


def test_node_types_keeps_only_node_type_suggestions():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"suggestions": [
            {"suggestion": "OktaUser", "label": "NODE_TYPE"},
            {"suggestion": "SHOW", "label": "KEYWORD"},
            {"suggestion": "AIAgent", "label": "NODE_TYPE"},
        ]})

    assert _client_with(handler).vql_node_types() == ["AIAgent", "OktaUser"]


class _StubClient:
    def __init__(self, names=None, error=None):
        self.names, self.error, self.calls = names, error, 0

    def vql_node_types(self):
        self.calls += 1
        if self.error:
            raise self.error
        return self.names


@pytest.fixture
def cache_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("VEZA_MCP_CACHE_DIR", str(tmp_path))
    return tmp_path


def test_queryable_set_is_cached_between_loads(cache_dir):
    stub = _StubClient(names=["OktaUser", "AIAgent"])
    assert schemamod._load_queryable(stub, force_refresh=False) == {"OktaUser", "AIAgent"}
    assert schemamod._load_queryable(stub, force_refresh=False) == {"OktaUser", "AIAgent"}
    assert stub.calls == 1


def test_force_refresh_bypasses_the_cache(cache_dir):
    stub = _StubClient(names=["OktaUser"])
    schemamod._load_queryable(stub, force_refresh=False)
    schemamod._load_queryable(stub, force_refresh=True)
    assert stub.calls == 2


def test_failure_to_list_types_degrades_to_unknown_not_an_error(cache_dir):
    stub = _StubClient(error=VezaError("Internal", "boom", status=500))
    assert schemamod._load_queryable(stub, force_refresh=False) is None
