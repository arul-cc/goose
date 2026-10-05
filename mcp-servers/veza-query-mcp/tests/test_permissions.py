"""Offline tests for permission clauses and function-call syntax.

Regression for a separation-of-duties report: asked for "AWS users that have both
s3:PutObject and s3:DeleteObject", the calling agent wrote

    WHERE FN_LIST_CONTAINS(permissions, "s3:PutObject") AND ...

which is not VQL (Veza answers HTTP 500), and our validator passed it. The
correct form is a permissions clause on the relationship. Permission NAMES are a
silent-zero trap too: a misspelled or wrongly-cased one returns 200 with 0 rows,
which a SoD check reads as "no conflicts".
"""

from __future__ import annotations

import pytest

from veza_query_mcp.repair import interpret_api_error, repair, repair_until_valid
from veza_query_mcp.schema import NodeType, SchemaIndex
from veza_query_mcp.vql import parse, validate

BAD = (
    'SHOW AwsIamUser { name, aws_account_id } RELATED TO AwsIamEffectivePermission '
    'WHERE FN_LIST_CONTAINS(permissions, "s3:PutObject") '
    'AND FN_LIST_CONTAINS(permissions, "s3:DeleteObject") LIMIT 1000;'
)
GOOD = (
    "SHOW AwsIamUser { name, aws_account_id } RELATED TO S3Bucket "
    "WITH SYSTEM PERMISSIONS = ALL ('s3:PutObject', 's3:DeleteObject') LIMIT 1000;"
)

VOCAB = {
    "SYSTEM": {"s3:PutObject", "s3:DeleteObject", "s3:GetObject", "s3:PutBucketPolicy"},
    "EFFECTIVE": {"DATA_READ", "DATA_DELETE", "DATA_WRITE"},
}


def _index() -> SchemaIndex:
    def nt(t, props, reach=()):
        return NodeType(
            type=t, name=t, group="G", integration="aws", labels=[t],
            properties={p: {"type": None, "optional": None} for p in props},
            reachable=set(reach),
        )

    return SchemaIndex({
        "AwsIamUser": nt("AwsIamUser", ["name", "aws_account_id", "id"],
                         reach=["S3Bucket", "AwsIamEffectivePermission"]),
        "S3Bucket": nt("S3Bucket", ["name"]),
        "AwsIamEffectivePermission": nt("AwsIamEffectivePermission", ["name"]),
    }, fetched_at=0.0)


class Lookup:
    def __init__(self, vocab=VOCAB):
        self.vocab, self.calls = vocab, []

    def __call__(self, src, dst, kind):
        self.calls.append((src, dst, kind))
        return self.vocab.get(kind) if (src, dst) == ("AwsIamUser", "S3Bucket") else None


@pytest.fixture
def idx() -> SchemaIndex:
    return _index()


# ── parsing ────────────────────────────────────────────────────────────────

def test_parses_the_permission_clause():
    assert parse(GOOD).permissions == [("SYSTEM", "ALL", ["s3:PutObject", "s3:DeleteObject"])]


def test_parses_effective_any_and_double_quotes():
    p = parse('SHOW AwsIamUser RELATED TO S3Bucket WITH effective permissions = any ("DATA_READ")')
    assert p.permissions == [("EFFECTIVE", "ANY", ["DATA_READ"])]


def test_flags_function_call_syntax_in_where():
    assert parse(BAD).function_calls == ["FN_LIST_CONTAINS"]


@pytest.mark.parametrize("where", [
    "name IN ('a', 'b')",
    "name NOT IN ('a')",
    "groups LIST_CONTAINS 'x'",
    "name = 'looks_like_a_call(x)'",
])
def test_legitimate_where_forms_are_not_function_calls(where):
    assert parse(f"SHOW OktaUser WHERE {where}").function_calls == []


# ── validation: the reported query ─────────────────────────────────────────

def test_rejects_the_reported_function_call_query(idx):
    r = validate(BAD, idx, Lookup())
    assert not r["valid"]
    err = next(e for e in r["errors"] if "FN_LIST_CONTAINS" in e["message"])
    assert "not VQL" in err["message"]
    assert "WITH SYSTEM PERMISSIONS" in err["fix"]
    assert "LIST_CONTAINS" in err["fix"]


def test_accepts_the_correct_query(idx):
    r = validate(GOOD, idx, Lookup())
    assert r["valid"], r["errors"]
    assert not r["warnings"]


# ── validation: permission names ───────────────────────────────────────────

def test_misspelled_permission_is_an_error_with_suggestion(idx):
    q = GOOD.replace("s3:PutObject", "s3:PutObjct")
    r = validate(q, idx, Lookup())
    assert not r["valid"]
    err = r["errors"][0]
    assert "0 rows" in err["message"] and "no conflicts" in err["message"]
    assert "s3:PutObject" in err["fix"]


def test_wrong_case_permission_is_an_error_naming_the_fix(idx):
    r = validate(GOOD.replace("s3:PutObject", "s3:putobject"), idx, Lookup())
    assert not r["valid"]
    assert "wrong casing" in r["errors"][0]["message"]
    assert "'s3:PutObject'" in r["errors"][0]["fix"]


def test_made_up_permission_with_no_near_match_points_at_the_lister(idx):
    r = validate(GOOD.replace("s3:DeleteObject", "xyz:Frobnicate"), idx, Lookup())
    assert not r["valid"]
    assert "veza_list_permissions" in r["errors"][0]["fix"]


def test_effective_kind_is_looked_up_separately(idx):
    look = Lookup()
    q = "SHOW AwsIamUser RELATED TO S3Bucket WITH EFFECTIVE PERMISSIONS = ANY ('DATA_DELETE') LIMIT 5;"
    assert validate(q, idx, look)["valid"]
    assert look.calls == [("AwsIamUser", "S3Bucket", "EFFECTIVE")]
    assert not validate(q.replace("DATA_DELETE", "DATA_SHRED"), idx, look)["valid"]


def test_permissions_unchecked_without_a_lookup(idx):
    assert validate(GOOD.replace("s3:PutObject", "s3:PutObjct"), idx)["valid"]


def test_unknown_vocabulary_skips_the_check_rather_than_flagging_everything(idx):
    # Groupings and pairs with nothing modelled come back empty from the server.
    assert validate(GOOD, idx, lambda s, d, k: None)["valid"]
    assert validate(GOOD, idx, lambda s, d, k: set())["valid"]


# ── repair ─────────────────────────────────────────────────────────────────

def test_repair_fixes_permission_casing_as_a_safe_change(idx):
    r = repair(GOOD.replace("s3:DeleteObject", "S3:deleteobject"), idx, permission_lookup=Lookup())
    assert "'s3:DeleteObject'" in r.query
    assert any(f.kind == "safe" and f.what == "permission casing" for f in r.fixes)


def test_repair_never_guesses_a_misspelled_permission(idx):
    """Permissions are the substance of a SoD check. Even a one-letter typo is
    reported, not rewritten — s3:PutBucketAcl vs s3:PutBucketPolicy are also one
    short edit apart and mean different things."""
    q = GOOD.replace("s3:PutObject", "s3:PutObjct")
    r = repair(q, idx, permission_lookup=Lookup())
    assert "s3:PutObjct" in r.query
    assert r.refusals and r.refusals[0].what == "permission"
    assert "no guess is made" in r.refusals[0].detail


def test_repair_until_valid_blocks_on_a_bad_permission(idx):
    out = repair_until_valid(GOOD.replace("s3:PutObject", "s3:PutObjct"), idx,
                             permission_lookup=Lookup())
    assert not out["valid"]
    assert len(out["blocked_by"]) == 1


def test_repair_until_valid_passes_a_correct_query_through(idx):
    out = repair_until_valid(GOOD, idx, permission_lookup=Lookup())
    assert out["valid"], out.get("errors")


# ── HTTP 500 ───────────────────────────────────────────────────────────────

def test_http_500_is_explained_as_possibly_malformed_not_an_outage(idx):
    out = interpret_api_error(
        ["Internal Server Error, please retry and if the error persists contact support"], idx
    )
    text = out["interpretation"]
    assert "not necessarily an outage" in text
    assert "veza_health" in text and "LIST_CONTAINS" in text
