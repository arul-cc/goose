"""Does the query ask what the requirement asked? And what does a zero mean?

Schema validation answers "will this run?". Measured against a live tenant, the
failures that remain once nl2vql produces schema-valid VQL are about meaning:

  - relationship negation inverted: "users NOT in any team" came back as
    `RELATED TO GithubTeam` (count 0) where `NOT RELATED TO` returns 4;
  - qualifiers dropped: "service accounts with ADMIN roles" lost "admin";
  - a zero that cannot be told apart from a broken filter.

These checks are heuristics: cheap, deterministic, advisory. They can flag a
query as suspect; they cannot prove one correct, so the absence of a warning
means "unchecked", never "verified".
"""

from __future__ import annotations

import re
from typing import Any

# Cues that a requirement negates something.
_NEGATION = re.compile(r"\b(?:not|no|without|never|none|neither|nor|lack(?:s|ing)?|missing)\b|n't\b", re.I)

# VQL constructs that express negation or exclusion. IS NOT NULL is deliberately
# absent: it is the opposite of "never logged in", not evidence of it.
_NEGATING_VQL = re.compile(
    r"\bNOT\s+(?:RELATED|WITH|IN)\b|\bIS\s+NULL\b|=\s*false\b|!=|=\s*0\b|<", re.I
)

# Words that narrow a population. If one is in the requirement and the query has
# no WHERE/HAVING at all, the narrowing was probably dropped.
_QUALIFIER = re.compile(
    r"\b(?:admin(?:istrator|istrative)?s?|privileged|sensitive|public(?:ly)?|external|guest|"
    r"stale|dormant|inactive|disabled|expired|unused|orphaned?|terminated|suspended|locked|"
    r"root|critical|high[- ]risk|over[- ]?provisioned|mfa|2fa|older than|more than|less than|"
    r"at least|within|last (?:login|used|active|logged)|\d+\s*(?:days?|weeks?|months?|years?))\b",
    re.I,
)

_STRING = re.compile(r"'[^']*'|\"[^\"]*\"")


def _finding(kind: str, message: str, fix: str) -> dict[str, str]:
    return {"kind": kind, "message": message, "fix": fix}


def lint(requirement: str, query: str) -> list[dict[str, str]]:
    """Advisory warnings where the query probably does not match the requirement."""
    out: list[dict[str, str]] = []
    q = " ".join(query.split())
    q_code = _STRING.sub(" ", q)
    has_filter = bool(re.search(r"\b(?:WHERE|HAVING)\b", q_code, re.I))

    # 1. Negation in the requirement, none in the query.
    cue = _NEGATION.search(requirement)
    if cue and not _NEGATING_VQL.search(q_code):
        if re.search(r"\bRELATED\s+TO\b", q_code, re.I):
            msg = (
                f"The requirement negates something ('{cue.group(0)}') but the query has no "
                "negating construct. RELATED TO returns entities that HAVE the relationship; "
                "if the intent is entities WITHOUT it, VQL needs NOT RELATED TO."
            )
        else:
            msg = (
                f"The requirement negates something ('{cue.group(0)}') but the query has no "
                "negating construct (NOT RELATED TO, IS NULL, = false, !=, <)."
            )
        out.append(_finding(
            "negation_not_expressed", msg,
            "Compare the query with the requirement; for 'not a member of' / 'without' "
            "relationships use NOT RELATED TO.",
        ))

    # 2. Qualifiers in the requirement, no filter in the query.
    if not has_filter:
        low = q.lower()
        dropped: list[str] = []
        for m in _QUALIFIER.finditer(requirement):
            token = m.group(0).lower()
            stem = re.sub(r"[^a-z0-9]", "", token)[:5]
            if stem not in low and token not in dropped:
                dropped.append(token)
        if dropped:
            out.append(_finding(
                "qualifier_dropped",
                f"The requirement mentions {', '.join(repr(t) for t in dropped[:4])} but the query "
                "has no WHERE/HAVING filter, so it likely answers a broader question than asked.",
                "Add the missing condition, or confirm the unfiltered population is what you want.",
            ))

    # 3. Literal values from the requirement that never reached the query.
    missing: list[str] = []
    for n in re.findall(r"(?<![\w.])\d+(?!\w)", requirement):
        if not re.search(rf"(?<![\w.]){re.escape(n)}(?!\w)", q) and n not in missing:
            missing.append(n)
    for a, b in re.findall(r"'([^']{2,})'|\"([^\"]{2,})\"", requirement):
        lit = a or b
        if lit.lower() not in q.lower() and lit not in missing:
            missing.append(lit)
    if missing:
        out.append(_finding(
            "literal_missing",
            f"The requirement contains {', '.join(repr(m) for m in missing[:4])} but the query "
            "does not, so a threshold or value may have been dropped.",
            "Check the comparison values in the WHERE clause.",
        ))
    return out


def interpret_zero(
    source: str,
    destination: str | None,
    source_total: int | None,
    related_total: int | None,
    *,
    negated: bool = False,
) -> dict[str, Any]:
    """Explain a zero count using the population sizes around it.

    A zero is ambiguous: an empty type, an empty relationship, filters that
    excluded everything, and a wrong filter value all read the same. `negated`
    marks NOT RELATED TO, where an empty relationship would return everyone, so
    "relationship_empty" must not be reported.
    """
    out: dict[str, Any] = {"source_type": source, "source_total": source_total}
    if destination:
        out["destination_type"] = destination
        out["related_total"] = related_total

    if source_total is None:
        out["diagnosis"] = "unknown"
        out["interpretation"] = "Could not size the surrounding population."
    elif source_total == 0:
        out["diagnosis"] = "type_unpopulated"
        out["interpretation"] = (
            f"No {source} entities exist in this tenant. The type is valid but empty, so a "
            "zero says nothing about your filters (is that integration ingesting data?)."
        )
    elif destination and negated:
        out["diagnosis"] = "negated_relationship_excluded_all"
        out["interpretation"] = (
            f"{source_total} {source} entities exist, and each one is either related to "
            f"{destination} (so NOT RELATED TO drops it) or removed by your filters."
        )
    elif destination and related_total == 0:
        out["diagnosis"] = "relationship_empty"
        out["interpretation"] = (
            f"{source_total} {source} entities exist but none is related to any {destination}. "
            "The relationship itself has no data, independent of your filters."
        )
    else:
        tail = f", {related_total} related to {destination}" if destination else ""
        out["diagnosis"] = "filters_excluded_all"
        out["interpretation"] = (
            f"{source_total} {source} entities exist{tail}. Your filters excluded every one: "
            "either a genuine 'no findings' or a wrong value / sparsely populated attribute. "
            "Check population with veza_describe_entity_type(sample_population=true)."
        )
    return out
