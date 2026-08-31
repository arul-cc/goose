"""Offline tests for VQL parsing and validation.

These use a stub schema index so they run without a tenant or network. The point
is to lock in the behaviours that matter most — above all, that an invalid
attribute is reported as an ERROR, because Veza returns HTTP 200 with zero rows
for that case and a compliance check would silently read as a pass.
"""

from __future__ import annotations

import pytest

from veza_query_mcp.schema import NodeType, SchemaIndex
from veza_query_mcp.vql import add_limit, parse, project, validate


def _index() -> SchemaIndex:
    def nt(t, props, reach=(), labels=()):
        return NodeType(
            type=t, name=t, group="G", integration="test",
            labels=list(labels) or [t],
            properties={p: {"type": None, "optional": None} for p in props},
            reachable=set(reach),
        )

    nodes = {
        "OktaUser": nt("OktaUser", ["id", "name", "is_active", "last_login_at"],
                       reach=["OktaGroup", "S3Bucket"],
                       labels=["OktaUser", "Identity", "User", "IdPUser"]),
        "OktaGroup": nt("OktaGroup", ["id", "name"], labels=["OktaGroup", "Group"]),
        "S3Bucket": nt("S3Bucket", ["id", "name", "created_at"], labels=["S3Bucket", "Resource"]),
        "WorkdayWorker": nt("WorkdayWorker", ["id", "name", "hire_date"],
                            labels=["WorkdayWorker", "Identity"]),
    }
    return SchemaIndex(nodes, fetched_at=0.0)


@pytest.fixture
def idx() -> SchemaIndex:
    return _index()


# ── parsing ────────────────────────────────────────────────────────────────

def test_parses_source_and_destination():
    p = parse("SHOW OktaUser RELATED TO S3Bucket LIMIT 5;")
    assert p.source_type == "OktaUser"
    assert p.destination_type == "S3Bucket"
    assert p.has_limit


def test_parses_projection():
    p = parse("SHOW OktaUser { name, last_login_at } LIMIT 5;")
    assert p.projected["OktaUser"] == ["name", "last_login_at"]


@pytest.mark.parametrize("clause,expected", [
    ("WHERE is_active = true", ["is_active"]),
    ("WHERE last_login_at IS NULL", ["last_login_at"]),
    ("WHERE last_login_at IS NOT NULL", ["last_login_at"]),
    ("WHERE name CONTAINS 'admin'", ["name"]),
])
def test_parses_where_attributes(clause, expected):
    # IS [NOT] NULL is undocumented but real, and Veza's nl2vql emits it. If the
    # parser misses it, an attribute typo inside such a clause goes unvalidated.
    assert parse(f"SHOW OktaUser {clause}").where_attrs == expected


def test_where_parsing_ignores_string_literals():
    p = parse("SHOW OktaUser WHERE name = 'is_active = true'")
    assert p.where_attrs == ["name"]


# ── validation: the cases that actually matter ─────────────────────────────

def test_valid_query_passes(idx):
    r = validate("SHOW OktaUser WHERE is_active = true LIMIT 5;", idx)
    assert r["valid"], r["errors"]


def test_unknown_attribute_is_an_error(idx):
    """The critical case: Veza returns 200/0-rows, so we must error locally."""
    r = validate("SHOW OktaUser WHERE bogus_attr = true LIMIT 5;", idx)
    assert not r["valid"]
    assert any("bogus_attr" in e["message"] for e in r["errors"])


def test_unknown_attribute_in_is_null_is_an_error(idx):
    r = validate("SHOW OktaUser WHERE bogus_attr IS NULL LIMIT 5;", idx)
    assert not r["valid"]
    assert any("bogus_attr" in e["message"] for e in r["errors"])


def test_unknown_node_type_is_an_error(idx):
    r = validate("SHOW NotARealType LIMIT 5;", idx)
    assert not r["valid"]


def test_wrong_casing_is_an_error_with_correction(idx):
    r = validate("SHOW oktauser LIMIT 5;", idx)
    assert not r["valid"]
    assert any("OktaUser" in (e.get("fix") or "") for e in r["errors"])


def test_unrelated_types_is_an_error(idx):
    r = validate("SHOW OktaUser RELATED TO WorkdayWorker LIMIT 5;", idx)
    assert not r["valid"]
    assert any("not related" in e["message"] for e in r["errors"])


def test_related_type_passes(idx):
    r = validate("SHOW OktaUser RELATED TO S3Bucket LIMIT 5;", idx)
    assert r["valid"], r["errors"]


def test_grouping_is_accepted_as_target(idx):
    # Groupings (User, Identity, Resource) are valid VQL targets even though they
    # are not concrete node types and never appear in reachable_node_types.
    r = validate("SHOW OktaUser RELATED TO Resource LIMIT 5;", idx)
    assert r["valid"], r["errors"]


def test_bad_projected_field_is_an_error(idx):
    r = validate("SHOW OktaUser { name, nope } LIMIT 5;", idx)
    assert not r["valid"]


# ── warnings (should run, but may not mean what was intended) ──────────────

def test_missing_limit_warns(idx):
    r = validate("SHOW OktaUser WHERE is_active = true", idx)
    assert r["valid"]
    assert any("LIMIT" in w["message"] for w in r["warnings"])


def test_path_summary_warns(idx):
    # path_summary_nodes came back empty on every live query tested.
    r = validate("SHOW OktaUser RELATED TO S3Bucket RESULT INCLUDE PATH SUMMARY LIMIT 5;", idx)
    assert any("PATH SUMMARY" in w["message"] for w in r["warnings"])


def test_enrich_without_result_include_warns(idx):
    r = validate("SHOW OktaUser RELATED TO S3Bucket ENRICH WITH WorkdayWorker LIMIT 5;", idx)
    assert any("ENRICH only populates" in w["message"] for w in r["warnings"])


# ── helpers ────────────────────────────────────────────────────────────────

def test_add_limit_is_idempotent():
    assert add_limit("SHOW OktaUser LIMIT 7;", 50) == "SHOW OktaUser LIMIT 7;"
    assert add_limit("SHOW OktaUser", 50) == "SHOW OktaUser LIMIT 50;"


def test_project_injects_once_and_respects_existing():
    assert project("SHOW OktaUser LIMIT 5;", "OktaUser", ["name"]) == \
        "SHOW OktaUser { name } LIMIT 5;"
    already = "SHOW OktaUser { name } LIMIT 5;"
    assert project(already, "OktaUser", ["id"]) == already
