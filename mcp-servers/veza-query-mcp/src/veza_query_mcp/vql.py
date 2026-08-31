"""VQL parsing, validation and projection.

This is a deliberately shallow parser. It does not aim to fully implement the
grammar — Veza's server does that, and its syntax errors are precise (they carry
line/column). What it *does* aim to catch is the one class of error Veza will
NOT report: an invalid attribute name silently returns HTTP 200 with zero rows.

    SHOW NotARealType LIMIT 1;                      -> 400, clear error
    SHOW OktaUser WHERE bogus_attr = true LIMIT 1;  -> 200, count = 0   (!)

For a compliance tool that second case is the dangerous one: a broken check
reports "no findings", which reads as a pass.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .schema import SchemaIndex

# Keywords are case-insensitive in VQL, and the trailing semicolon is optional —
# verified live: `Show OktaUser WHERE is_active = true related to S3Bucket` runs.
# Only node types and attribute names are case-sensitive.
_KEYWORDS = {
    "show", "related", "to", "not", "with", "path", "where", "and", "or",
    "having", "result", "include", "destination", "nodes", "node", "count",
    "summary", "query", "options", "enrich", "source", "limit", "after",
    "cursor", "true", "false", "null", "in", "contains", "starts_with",
    "ends_with", "regex", "list_contains", "list_any_element_eq",
    "current_date", "entity_result_count", "percentage_of_total_count", "is",
    "over_provisioned_score", "engagement_score", "last_used",
}

_IDENT = r"[A-Za-z_][A-Za-z0-9_]*"


@dataclass
class ParsedVQL:
    source_type: str | None = None
    destination_type: str | None = None
    intermediate_types: list[str] = field(default_factory=list)
    enrich_types: list[str] = field(default_factory=list)
    where_attrs: list[str] = field(default_factory=list)
    projected: dict[str, list[str]] = field(default_factory=dict)
    has_limit: bool = False
    result_include: str | None = None


def parse(query: str) -> ParsedVQL:
    """Extract the identifiers we need to validate. Best-effort, not a full parse."""
    q = " ".join(query.split())
    p = ParsedVQL()

    m = re.search(rf"\bSHOW\s+({_IDENT})", q, re.I)
    if m:
        p.source_type = m.group(1)

    m = re.search(rf"\bRELATED\s+TO\s+({_IDENT})", q, re.I)
    if m:
        p.destination_type = m.group(1)

    p.intermediate_types = re.findall(rf"\bWITH\s+PATH\s+({_IDENT})", q, re.I)

    # ENRICH [SOURCE|DESTINATION] WITH X  |  WITH (X, Y)
    for chunk in re.findall(r"\bENRICH\s+(?:SOURCE\s+|DESTINATION\s+)?WITH\s+([^;]+)", q, re.I):
        head = re.split(r"\b(?:LIMIT|RESULT|WITH|ENRICH|HAVING)\b", chunk, maxsplit=1, flags=re.I)[0]
        p.enrich_types += re.findall(_IDENT, head.replace("(", " ").replace(")", " "))

    # Property projection:  SHOW X { a, b }  /  RELATED TO Y { c }
    for owner, body in re.findall(rf"({_IDENT})\s*\{{([^}}]*)\}}", q):
        p.projected[owner] = re.findall(_IDENT, body)

    m = re.search(r"\bWHERE\b(.*?)(?:\bRELATED\b|\bWITH\b|\bHAVING\b|\bRESULT\b|\bENRICH\b|\bLIMIT\b|$)", q, re.I)
    if m:
        clause = m.group(1)
        # Left-hand identifiers of comparisons; skip quoted literals and keywords.
        clause_wo_strings = re.sub(r"'[^']*'|\"[^\"]*\"", " ", clause)
        # Comparison and function-style operators, plus IS [NOT] NULL. The latter
        # is undocumented but real — verified live: `WHERE last_login_at IS NULL`
        # executes and returns 878. Veza's nl2vql emits it, so it must be parsed
        # or an attribute typo inside an IS NULL clause slips through unvalidated.
        op = (
            r"=|!=|<=|>=|<|>|\bIS\s+NOT\s+NULL\b|\bIS\s+NULL\b|\bIN\b|\bCONTAINS\b"
            r"|\bSTARTS_WITH\b|\bENDS_WITH\b|\bREGEX\b|\bLIST_CONTAINS\b|\bLIST_ANY_ELEMENT_EQ\b"
        )
        for ident in re.findall(rf"({_IDENT})\s*(?:{op})", clause_wo_strings, re.I):
            if ident.lower() not in _KEYWORDS:
                p.where_attrs.append(ident)

    p.has_limit = bool(re.search(r"\bLIMIT\s+\d+", q, re.I))
    m = re.search(r"\bRESULT\s+INCLUDE\s+([A-Za-z ]+?)(?:\bWITH\b|\bENRICH\b|\bLIMIT\b|;|$)", q, re.I)
    if m:
        p.result_include = " ".join(m.group(1).split()).upper()
    return p


@dataclass
class Finding:
    severity: str  # "error" | "warning"
    message: str
    fix: str | None = None


def validate(query: str, index: SchemaIndex) -> dict[str, Any]:
    """Validate a VQL string against the cached schema.

    Errors are things that will either fail or — worse — silently return nothing.
    Warnings are things that will run but may not mean what the author intended.
    """
    findings: list[Finding] = []
    p = parse(query)

    if not p.source_type:
        findings.append(Finding("error", "No SHOW clause found — every VQL query starts with SHOW <NodeType>."))
        return _result(query, p, findings)

    def check_type(name: str, role: str) -> str | None:
        """Returns the canonical type name, or None if unresolvable."""
        if index.is_grouping(name):
            return name  # groupings are valid query targets (User, Identity, Resource…)
        canonical = index.canonical_name(name)
        if not canonical:
            sugg = index.search(name, limit=3)
            hint = ", ".join(s["type"] for s in sugg)
            findings.append(
                Finding(
                    "error",
                    f"{role} '{name}' is not a valid node type. Veza rejects this with HTTP 400.",
                    f"Try: {hint}" if hint else "Use veza_search_entity_types to find the right type.",
                )
            )
            return None
        if canonical != name:
            findings.append(
                Finding(
                    "error",
                    f"{role} '{name}' has the wrong casing — node types are case-sensitive.",
                    f"Use '{canonical}'.",
                )
            )
        return canonical

    src = check_type(p.source_type, "Source type")
    dst = check_type(p.destination_type, "Destination type") if p.destination_type else None
    for it in p.intermediate_types:
        check_type(it, "Intermediate type (WITH PATH)")
    for et in p.enrich_types:
        canonical = check_type(et, "Enrich type")
        # ENRICH additionally requires the types to be correlated in the graph;
        # otherwise Veza returns 400 "Node types are not related" and joined_nodes
        # comes back empty. Verified: Okta<->Workday is not correlated by default.
        if canonical and src and index.can_relate(src, canonical) is False:
            findings.append(
                Finding(
                    "warning",
                    f"ENRICH WITH {canonical}: '{src}' and '{canonical}' are not related in this "
                    "graph, so joined_nodes will be empty.",
                    "Identity correlation must be configured in Veza for this join to work.",
                )
            )

    # Relationship validity
    if src and dst and not index.is_grouping(p.destination_type or ""):
        relatable = index.can_relate(src, dst)
        if relatable is False:
            findings.append(
                Finding(
                    "error",
                    f"'{src}' is not related to '{dst}' in the graph — Veza returns "
                    "400 'Node types are not related'.",
                    "Use veza_list_relationships to see valid targets.",
                )
            )

    # ── The important one: attribute names ────────────────────────────────
    # An unknown attribute does NOT error. It returns 200 with zero rows, which
    # is indistinguishable from a genuine empty result set.
    owner_for_where = src if src and not index.is_grouping(p.source_type or "") else None
    if owner_for_where and p.where_attrs:
        unknown = index.unknown_properties(owner_for_where, p.where_attrs)
        for attr in unknown:
            nt = index.resolve(owner_for_where)
            close = []
            if nt:
                low = attr.lower()
                close = [k for k in nt.properties if low in k.lower() or k.lower() in low][:3]
            findings.append(
                Finding(
                    "error",
                    f"Attribute '{attr}' does not exist on {owner_for_where}. "
                    "⚠️ Veza will NOT error — it returns 200 with 0 rows, so this looks "
                    "like 'no matches' rather than a bug.",
                    f"Did you mean: {', '.join(close)}?" if close else
                    f"Use veza_describe_entity_type('{owner_for_where}') to list valid attributes.",
                )
            )

    # Projected fields must exist too
    for owner, fields_ in p.projected.items():
        canonical = index.canonical_name(owner)
        if canonical:
            for attr in index.unknown_properties(canonical, fields_):
                findings.append(
                    Finding("error", f"Projected field '{attr}' does not exist on {canonical}.")
                )

    # ── Advisory ──────────────────────────────────────────────────────────
    if p.enrich_types and p.result_include is None:
        findings.append(
            Finding(
                "warning",
                "ENRICH only populates data when the query uses RESULT INCLUDE "
                "DESTINATION NODES or PATH SUMMARY.",
                "Add RESULT INCLUDE DESTINATION NODES before the ENRICH clause.",
            )
        )
    if p.result_include and "PATH SUMMARY" in p.result_include:
        findings.append(
            Finding(
                "warning",
                "RESULT INCLUDE PATH SUMMARY returned an empty path_summary_nodes on every "
                "query tested against a live tenant.",
                "To get group/role membership, query it as a direct relationship "
                "(e.g. SHOW OktaUser RELATED TO OktaGroup) and join client-side.",
            )
        )
    if not p.has_limit:
        findings.append(
            Finding(
                "warning",
                "No LIMIT — result sets can be very large (a single 'users related to "
                "resources' query returned 974 rows in a small sandbox).",
                "Add LIMIT, and check the population size with mode='count' first.",
            )
        )
    if p.destination_type and not p.projected:
        findings.append(
            Finding(
                "warning",
                "No property projection — unprojected rows cost ~4.5KB each (~1.1k tokens).",
                "Add { field, field } after the node type to cut payload ~35%.",
            )
        )

    return _result(query, p, findings)


def _result(query: str, p: ParsedVQL, findings: list[Finding]) -> dict[str, Any]:
    errors = [f for f in findings if f.severity == "error"]
    return {
        "valid": not errors,
        "query": query,
        "parsed": {
            "source_type": p.source_type,
            "destination_type": p.destination_type,
            "intermediate_types": p.intermediate_types,
            "enrich_types": p.enrich_types,
            "where_attributes": p.where_attrs,
            "projected": p.projected,
            "result_include": p.result_include,
            "has_limit": p.has_limit,
        },
        "errors": [{"message": f.message, "fix": f.fix} for f in errors],
        "warnings": [
            {"message": f.message, "fix": f.fix} for f in findings if f.severity == "warning"
        ],
    }


def add_limit(query: str, limit: int) -> str:
    """Append a LIMIT if absent — a guard against unbounded result sets."""
    if re.search(r"\bLIMIT\s+\d+", query, re.I):
        return query
    q = query.rstrip().rstrip(";")
    return f"{q} LIMIT {limit};"


def project(query: str, node_type: str, fields: list[str]) -> str:
    """Inject `{ a, b }` projection after a node type if not already projected.

    Verified: source-side projection cuts payload ~35%. Destination-side
    projection had no measurable effect, so only apply where it helps.
    """
    if not fields or re.search(rf"\b{re.escape(node_type)}\s*\{{", query):
        return query
    return re.sub(
        rf"\b({re.escape(node_type)})\b",
        lambda m: f"{m.group(1)} {{ {', '.join(fields)} }}",
        query,
        count=1,
    )
