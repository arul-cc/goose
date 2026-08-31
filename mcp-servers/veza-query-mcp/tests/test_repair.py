"""Offline tests for the repair layer.

The behaviour under test is not "does it fix things" but "does it know when NOT
to". A compliance query silently rewritten into a different question is worse
than one that failed, so refusals matter as much as fixes.
"""

from __future__ import annotations

import pytest

from veza_query_mcp.repair import interpret_api_error, repair, repair_until_valid
from veza_query_mcp.schema import NodeType, SchemaIndex


def _index() -> SchemaIndex:
    def nt(t, props, reach=(), labels=()):
        return NodeType(
            type=t, name=t, group="G", integration="test",
            labels=list(labels) or [t],
            properties={p: {"type": None, "optional": None} for p in props},
            reachable=set(reach),
        )

    nodes = {
        "OktaUser": nt("OktaUser", ["id", "name", "is_active", "last_login_at", "mfa_active"],
                       reach=["OktaGroup", "S3Bucket"],
                       labels=["OktaUser", "Identity", "User"]),
        "OktaGroup": nt("OktaGroup", ["id", "name"], labels=["OktaGroup", "Group"]),
        "S3Bucket": nt("S3Bucket", ["id", "name", "created_at"], labels=["S3Bucket", "Resource"]),
        "GithubTeam": nt("GithubTeam", ["id", "name"], labels=["GithubTeam"]),
        "WorkdayWorker": nt("WorkdayWorker", ["id", "hire_date"], labels=["WorkdayWorker"]),
    }
    return SchemaIndex(nodes, fetched_at=0.0)


@pytest.fixture
def idx() -> SchemaIndex:
    return _index()


# ── safe fixes: cannot change meaning ──────────────────────────────────────

def test_fixes_node_type_casing_as_safe(idx):
    r = repair("SHOW oktauser LIMIT 5;", idx)
    assert "SHOW OktaUser" in r.query
    assert any(f.kind == "safe" and "casing" in f.what for f in r.fixes)


def test_fixes_github_prefix_casing(idx):
    # Real trap: the integration is "Github", not "GitHub".
    r = repair("SHOW GitHubTeam LIMIT 5;", idx)
    assert "GithubTeam" in r.query


def test_adds_limit_when_absent(idx):
    r = repair_until_valid("SHOW OktaUser WHERE is_active = true", idx, limit=50)
    assert "LIMIT 50" in r["query"]


def test_respects_existing_limit(idx):
    r = repair_until_valid("SHOW OktaUser LIMIT 7;", idx, limit=50)
    assert "LIMIT 7" in r["query"] and "LIMIT 50" not in r["query"]


def test_limit_applied_even_when_otherwise_valid(idx):
    """Regression: safe fixes used to be skipped for already-valid queries, so a
    query only got bounded if it also had an unrelated error."""
    r = repair_until_valid("SHOW OktaUser", idx, limit=100)
    assert r["valid"] and "LIMIT 100" in r["query"]


def test_repair_is_idempotent(idx):
    once = repair_until_valid("SHOW OktaUser WHERE is_active = true", idx, limit=100)["query"]
    twice = repair_until_valid(once, idx, limit=100)["query"]
    assert once == twice


# ── substitutions: reported, never silent ──────────────────────────────────

def test_substitutes_close_attribute_and_reports_it(idx):
    r = repair("SHOW OktaUser WHERE is_activ = true LIMIT 5;", idx)
    assert "is_active" in r.query
    subs = r.substitutions
    assert subs and subs[0].before == "is_activ" and subs[0].after == "is_active"


def test_substitutes_close_node_type(idx):
    r = repair("SHOW OktaUsr LIMIT 5;", idx)
    assert "OktaUser" in r.query
    assert r.substitutions


# ── refusals: the important half ───────────────────────────────────────────

def test_refuses_unrecognisable_attribute(idx):
    r = repair("SHOW OktaUser WHERE flibbertigibbet = true LIMIT 5;", idx)
    assert "flibbertigibbet" in r.query, "must not silently drop or guess"
    assert r.refusals
    assert "0 rows" in r.refusals[0].detail


def test_refuses_to_rewrite_an_unrelated_destination(idx):
    """Changing the destination changes the question, so it is never auto-fixed."""
    r = repair("SHOW OktaUser RELATED TO WorkdayWorker LIMIT 5;", idx)
    assert "WorkdayWorker" in r.query
    assert any(f.what == "relationship" for f in r.refusals)


def test_refused_query_reports_invalid(idx):
    r = repair_until_valid("SHOW OktaUser WHERE nope_not_real = 1 LIMIT 5;", idx)
    assert not r["valid"]
    assert r["blocked_by"]


def test_does_not_touch_string_literals(idx):
    q = "SHOW OktaUser WHERE name = 'oktauser is_activ' LIMIT 5;"
    r = repair(q, idx)
    assert "'oktauser is_activ'" in r.query


# ── API error interpretation ───────────────────────────────────────────────

def test_interprets_invalid_node_type_error(idx):
    out = interpret_api_error(["OktaUsr is not a valid NodeType"], idx)
    assert "not a valid node type" in out["interpretation"]
    assert out.get("auto_fix", {}).get("with") == "OktaUser"


def test_interprets_not_related_error(idx):
    out = interpret_api_error(["Node types are not related"], idx)
    assert "no relationship" in out["interpretation"]


def test_interprets_syntax_error_with_clause_order_hint(idx):
    out = interpret_api_error(
        ["Syntax error line 1:62 no viable alternative at input 'RESULT INCLUDE'"], idx
    )
    assert "clause order" in out["interpretation"].lower()


def test_interprets_missing_access_patterns(idx):
    out = interpret_api_error(
        ["no access patterns relationship for source_node_type = AwsIamRole"], idx
    )
    assert "Access Monitoring" in out["interpretation"]
