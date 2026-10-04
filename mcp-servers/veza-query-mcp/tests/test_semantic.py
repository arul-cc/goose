"""Offline tests for requirement-vs-query linting and zero-result interpretation.

Every lint case here is a real nl2vql output observed against a live tenant.
These checks can only flag a query as suspect — a clean result means
"unchecked", never "correct" — so the tests pin both what must be flagged and
what must not be (false positives train callers to ignore the warnings).
"""

from __future__ import annotations

import pytest

from veza_query_mcp.semantic import interpret_zero, lint


def kinds(requirement: str, query: str) -> list[str]:
    return [f["kind"] for f in lint(requirement, query)]


# ── negation ───────────────────────────────────────────────────────────────

def test_flags_inverted_relationship_negation():
    """Observed live: 'not in any team' → RELATED TO (count 0); NOT RELATED TO → 4."""
    r = lint("GitHub users who are not members of any team",
             "Show GithubPersonalAccount related to GithubTeam")
    assert [f["kind"] for f in r] == ["negation_not_expressed"]
    assert "NOT RELATED TO" in r[0]["message"]


@pytest.mark.parametrize("requirement,query", [
    ("GitHub users who are not members of any team",
     "Show GithubPersonalAccount NOT RELATED TO GithubTeam"),
    ("AWS IAM users without MFA", "Show AwsIamUser WHERE mfa_active = false"),
    ("Azure AD users who have never logged in", "Show AzureADUser WHERE last_login_at IS NULL"),
    ("Okta users with no login in 90 days",
     "Show OktaUser WHERE last_login_at < CURRENT_DATE - 90"),
    ("users who are not service accounts", "Show User WHERE identity_type != 'service'"),
])
def test_accepts_queries_that_express_the_negation(requirement, query):
    assert "negation_not_expressed" not in kinds(requirement, query)


def test_is_not_null_is_not_evidence_of_negation():
    # 'never logged in' → IS NOT NULL is the exact opposite.
    assert "negation_not_expressed" in kinds(
        "users who have never logged in", "Show OktaUser WHERE last_login_at IS NOT NULL")


def test_negating_text_inside_a_string_literal_does_not_count():
    assert "negation_not_expressed" in kinds(
        "users without a manager", "Show OktaUser WHERE name = 'a != b'")


def test_no_negation_no_warning():
    assert lint("active Okta users with access to S3 buckets",
                "Show OktaUser WHERE is_active = true related to S3Bucket") == []


# ── dropped qualifiers ─────────────────────────────────────────────────────

def test_flags_dropped_admin_qualifier():
    r = lint("service accounts with admin roles", "Show ServiceAccount related to AzureRole")
    assert any(f["kind"] == "qualifier_dropped" and "admin" in f["message"] for f in r)


def test_flags_dropped_sensitive_qualifier():
    assert "qualifier_dropped" in kinds(
        "AI agents with access to sensitive data", "Show AIAgent related to Resource")


def test_qualifier_reflected_in_a_type_name_is_not_dropped():
    assert "qualifier_dropped" not in kinds(
        "users with admin roles", "Show OktaUser related to OktaAdminRole")


def test_qualifier_with_a_where_clause_is_not_flagged():
    assert "qualifier_dropped" not in kinds(
        "inactive users in Active Directory", "Show ActiveDirectoryUser WHERE is_active = false")


# ── literals ───────────────────────────────────────────────────────────────

def test_flags_threshold_missing_from_query():
    assert "literal_missing" in kinds(
        "Okta users inactive for more than 90 days", "Show OktaUser WHERE is_active = false")


def test_threshold_present_in_query_passes():
    assert "literal_missing" not in kinds(
        "Okta users whose last login was more than 90 days ago",
        "Show OktaUser WHERE last_login_at < CURRENT_DATE - 90")


def test_digits_inside_identifiers_are_not_literals():
    assert "literal_missing" not in kinds(
        "S3 buckets that are public", "Show S3Bucket WHERE has_public_policy = true")


def test_flags_quoted_literal_missing_from_query():
    assert "literal_missing" in kinds(
        "Snowflake users with the 'ACCOUNTADMIN' role", "Show SnowflakeUser related to SnowflakeRole")


def test_quoted_literal_present_passes():
    assert "literal_missing" not in kinds(
        "Snowflake users with the 'ACCOUNTADMIN' role",
        "Show SnowflakeUser related to SnowflakeRole WHERE name = 'ACCOUNTADMIN'")


# ── zero-result interpretation ─────────────────────────────────────────────

def test_zero_on_empty_type():
    r = interpret_zero("OktaUser", None, 0, None)
    assert r["diagnosis"] == "type_unpopulated"
    assert "says nothing about your filters" in r["interpretation"]


def test_zero_on_empty_relationship():
    r = interpret_zero("OktaUser", "S3Bucket", 40, 0)
    assert r["diagnosis"] == "relationship_empty"
    assert r["related_total"] == 0


def test_zero_from_filters_excluding_everything():
    r = interpret_zero("OktaUser", "S3Bucket", 40, 13)
    assert r["diagnosis"] == "filters_excluded_all"
    assert "40" in r["interpretation"] and "13" in r["interpretation"]


def test_zero_from_filters_without_a_relationship():
    r = interpret_zero("S3Bucket", None, 120, None)
    assert r["diagnosis"] == "filters_excluded_all"
    assert "related" not in r["interpretation"]


def test_negated_relationship_is_never_reported_as_an_empty_relationship():
    """With NOT RELATED TO an empty relationship returns everyone, so a zero means
    the opposite of 'relationship_empty'."""
    r = interpret_zero("GithubPersonalAccount", "GithubTeam", 30, None, negated=True)
    assert r["diagnosis"] == "negated_relationship_excluded_all"


def test_unknown_when_population_cannot_be_sized():
    assert interpret_zero("OktaUser", None, None, None)["diagnosis"] == "unknown"
