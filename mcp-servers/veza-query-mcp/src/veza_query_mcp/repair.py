"""Deterministic VQL repair.

Given a query and the schema, fix what can be fixed *without changing what the
query means*, and refuse to guess at anything else.

That distinction is the whole design. This runs inside a compliance tool: a query
that was silently "repaired" into asking a different question is worse than one
that failed loudly, because its results will be believed. So repairs are graded:

  safe        — cannot change semantics (casing, missing LIMIT)
  substituted — changes an identifier to a high-confidence match; ALWAYS reported
  refused     — ambiguous or semantic; returned to the caller to decide
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from typing import Any

from .schema import SchemaIndex
from .vql import parse, validate

# Below this ratio a "close match" is a guess, not a correction.
SUBSTITUTION_THRESHOLD = 0.72


@dataclass
class Fix:
    kind: str            # "safe" | "substituted" | "refused"
    what: str
    detail: str
    before: str | None = None
    after: str | None = None


@dataclass
class RepairResult:
    query: str
    fixes: list[Fix] = field(default_factory=list)
    changed: bool = False

    @property
    def substitutions(self) -> list[Fix]:
        return [f for f in self.fixes if f.kind == "substituted"]

    @property
    def refusals(self) -> list[Fix]:
        return [f for f in self.fixes if f.kind == "refused"]

    def as_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "changed": self.changed,
            "fixes": [
                {k: v for k, v in
                 {"kind": f.kind, "what": f.what, "detail": f.detail,
                  "before": f.before, "after": f.after}.items() if v is not None}
                for f in self.fixes
            ],
        }


def _closest(target: str, candidates: list[str]) -> tuple[str | None, float]:
    if not candidates:
        return None, 0.0
    best = difflib.get_close_matches(target, candidates, n=1, cutoff=0.0)
    if not best:
        return None, 0.0
    ratio = difflib.SequenceMatcher(None, target.lower(), best[0].lower()).ratio()
    return best[0], ratio


def _replace_identifier(query: str, old: str, new: str) -> str:
    """Replace a whole-word identifier, leaving quoted literals alone."""
    parts = re.split(r"('[^']*'|\"[^\"]*\")", query)
    for i in range(0, len(parts), 2):  # even indices are outside quotes
        parts[i] = re.sub(rf"\b{re.escape(old)}\b", new, parts[i])
    return "".join(parts)


def repair(query: str, index: SchemaIndex, *, add_limit_default: int | None = 100) -> RepairResult:
    """Attempt one pass of repair. Call repeatedly until `changed` is False."""
    res = RepairResult(query=query)
    p = parse(query)

    # ── node types: casing is safe; a wrong name is a substitution ──────────
    for raw, role in (
        [(p.source_type, "source type")] if p.source_type else []
    ) + (
        [(p.destination_type, "destination type")] if p.destination_type else []
    ) + [(t, "intermediate type") for t in p.intermediate_types] \
      + [(t, "enrich type") for t in p.enrich_types]:
        if not raw or index.is_grouping(raw):
            continue
        canonical = index.canonical_name(raw)
        if canonical == raw:
            continue
        if canonical:
            # Same identifier, different case — cannot change meaning.
            res.query = _replace_identifier(res.query, raw, canonical)
            res.changed = True
            res.fixes.append(Fix("safe", f"{role} casing",
                                 f"node types are case-sensitive", raw, canonical))
            continue
        # Unknown type — try a confident substitution.
        cand, ratio = _closest(raw, list(index.nodes.keys()))
        if cand and ratio >= SUBSTITUTION_THRESHOLD:
            res.query = _replace_identifier(res.query, raw, cand)
            res.changed = True
            res.fixes.append(Fix("substituted", f"{role}",
                                 f"'{raw}' is not a valid node type (match confidence {ratio:.2f})",
                                 raw, cand))
        else:
            hints = [m["type"] for m in index.search(raw, limit=5)]
            res.fixes.append(Fix("refused", f"{role}",
                                 f"'{raw}' is not a valid node type and no close match was found. "
                                 f"Candidates: {', '.join(hints) if hints else 'none'}", raw, None))

    # ── attributes: the dangerous ones (Veza returns 200/0 rows, not an error) ─
    p2 = parse(res.query)
    owner = p2.source_type
    if owner and not index.is_grouping(owner):
        nt = index.resolve(owner)
        if nt:
            props = list(nt.properties.keys())
            for attr in index.unknown_properties(owner, p2.where_attrs):
                cand, ratio = _closest(attr, props)
                if cand and ratio >= SUBSTITUTION_THRESHOLD:
                    res.query = _replace_identifier(res.query, attr, cand)
                    res.changed = True
                    res.fixes.append(Fix(
                        "substituted", "where attribute",
                        f"'{attr}' does not exist on {owner} (match confidence {ratio:.2f}). "
                        "Veza would have returned 0 rows without erroring.",
                        attr, cand))
                else:
                    near = [x for x in props if attr.lower() in x.lower() or x.lower() in attr.lower()][:5]
                    res.fixes.append(Fix(
                        "refused", "where attribute",
                        f"'{attr}' does not exist on {owner} and no close match was found. "
                        f"⚠️ Left as-is it returns 0 rows with no error. "
                        f"Similar: {', '.join(near) if near else 'none'}", attr, None))

            for holder, fields_ in (p2.projected or {}).items():
                canonical = index.canonical_name(holder)
                if not canonical:
                    continue
                for attr in index.unknown_properties(canonical, fields_):
                    cand, ratio = _closest(attr, list(index.nodes[canonical].properties.keys()))
                    if cand and ratio >= SUBSTITUTION_THRESHOLD:
                        res.query = _replace_identifier(res.query, attr, cand)
                        res.changed = True
                        res.fixes.append(Fix("substituted", "projected field",
                                             f"'{attr}' not on {canonical}", attr, cand))
                    else:
                        res.fixes.append(Fix("refused", "projected field",
                                             f"'{attr}' does not exist on {canonical}", attr, None))

    # ── relationship validity: never silently rewrite; the target IS the question ─
    p3 = parse(res.query)
    if p3.source_type and p3.destination_type and not index.is_grouping(p3.destination_type):
        if index.can_relate(p3.source_type, p3.destination_type) is False:
            alt = index.relationships(p3.source_type, to_filter=None, limit=200).get("reachable", [])
            cand, ratio = _closest(p3.destination_type, alt)
            suggestion = f" Closest reachable type: {cand}." if cand and ratio > 0.5 else ""
            res.fixes.append(Fix(
                "refused", "relationship",
                f"'{p3.source_type}' cannot relate to '{p3.destination_type}' — Veza returns 400. "
                f"Changing the destination would change the question being asked, so this is left "
                f"for you to decide.{suggestion}",
                p3.destination_type, None))

    # ── unbounded result sets: safe to bound ───────────────────────────────
    if add_limit_default and not parse(res.query).has_limit:
        q = res.query.rstrip().rstrip(";")
        res.query = f"{q} LIMIT {add_limit_default};"
        res.changed = True
        res.fixes.append(Fix("safe", "limit",
                             f"no LIMIT present; bounded to {add_limit_default}"))

    return res


def repair_until_valid(
    query: str, index: SchemaIndex, *, max_passes: int = 4, limit: int | None = 100
) -> dict[str, Any]:
    """Repair repeatedly until schema-valid or no further progress is possible.

    Note the loop runs repair BEFORE checking validity. An earlier version
    short-circuited on `valid` first, which meant safe fixes (notably bounding an
    unbounded query with LIMIT) were only applied to queries that happened to
    have some *other* error — so whether you got a LIMIT depended on whether you
    also made a typo. Safe fixes should apply unconditionally.
    """
    applied: list[Fix] = []
    current = query
    for _ in range(max_passes):
        r = repair(current, index, add_limit_default=limit)
        applied.extend(r.fixes)
        if r.changed:
            current = r.query

        verdict = validate(current, index)
        if verdict["valid"]:
            return {
                "query": current,
                "valid": True,
                "fixes": [f.__dict__ for f in applied],
                "warnings": verdict["warnings"],
            }
        if not r.changed:
            break  # nothing more we can safely do

    verdict = validate(current, index)
    return {
        "query": current,
        "valid": verdict["valid"],
        "fixes": [f.__dict__ for f in applied],
        "errors": verdict.get("errors", []),
        "warnings": verdict.get("warnings", []),
        "blocked_by": [f.__dict__ for f in applied if f.kind == "refused"],
    }


# ─────────────────────────────────────── error-driven repair


_NODE_TYPE_ERR = re.compile(r"'?([A-Za-z_][A-Za-z0-9_]*)'? is not a valid NodeType", re.I)
_NOT_RELATED = re.compile(r"Node types are not related", re.I)
_SYNTAX_AT = re.compile(r"line (\d+):?(\d+)?.*?no viable alternative at input '([^']*)'", re.I)


def interpret_api_error(violations: list[str], index: SchemaIndex) -> dict[str, Any]:
    """Turn Veza's server-side error into actionable next steps.

    Veza's diagnostics are unusually precise (offending identifier, line/column),
    which is what makes an automated second pass worthwhile. This is needed
    because the local parser is deliberately shallow — it validates identifiers,
    not the full grammar, so syntax errors only surface on execution.
    """
    joined = " ".join(violations)
    out: dict[str, Any] = {"raw": violations, "interpretation": None, "suggestions": []}

    m = _NODE_TYPE_ERR.search(joined)
    if m:
        bad = m.group(1)
        cand, ratio = _closest(bad, list(index.nodes.keys()))
        out["interpretation"] = f"'{bad}' is not a valid node type"
        out["suggestions"] = [c["type"] for c in index.search(bad, limit=5)]
        if cand and ratio >= SUBSTITUTION_THRESHOLD:
            out["auto_fix"] = {"replace": bad, "with": cand, "confidence": round(ratio, 2)}
        return out

    if _NOT_RELATED.search(joined):
        out["interpretation"] = (
            "The two node types have no relationship in this graph. Use "
            "veza_list_relationships on the source type to find valid targets."
        )
        return out

    m = _SYNTAX_AT.search(joined)
    if m:
        out["interpretation"] = (
            f"VQL syntax error at line {m.group(1)}, near '{m.group(3)}'. "
            "Check clause order: SHOW → RELATED TO → WHERE → WITH PATH → HAVING → "
            "RESULT INCLUDE → WITH QUERY OPTIONS → ENRICH."
        )
        return out

    if "no access patterns relationship" in joined.lower():
        out["interpretation"] = (
            "This query needs activity/access-pattern data (e.g. WITH QUERY OPTIONS "
            "over_provisioned_score), which requires Access Monitoring ingestion to be "
            "configured for that integration. Not available in every tenant."
        )
        return out

    out["interpretation"] = "Unrecognised server error; see raw."
    return out
