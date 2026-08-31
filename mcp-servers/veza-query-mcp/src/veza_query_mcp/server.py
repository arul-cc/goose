"""Veza Query MCP — read-only access to the Veza Access Graph via VQL.

Design constraints that shape every tool here:

1. The graph schema is ~4MB / ~1M tokens. It never enters model context; it is
   indexed server-side and only slices are returned.
2. Result rows cost ~730-1,100 tokens each. Full result sets are written
   out-of-band and only a small sample is returned inline.
3. A count costs ~17 tokens. Validate and size with counts before fetching rows.
4. Invalid attribute names return HTTP 200 with 0 rows rather than an error, so
   validation happens locally against the schema.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Literal

from mcp.server.mcpserver import MCPServer

from .client import VezaClient, VezaError
from .schema import SchemaIndex
from . import repair as repairmod
from . import vql as vqlmod

mcp = MCPServer("veza-query")

_client: VezaClient | None = None
_index: SchemaIndex | None = None

# Rows returned inline to the model. Everything else goes to a file.
INLINE_SAMPLE = 5
DEFAULT_ROW_LIMIT = 100


def client() -> VezaClient:
    global _client
    if _client is None:
        _client = VezaClient()
    return _client


def index(force_refresh: bool = False) -> SchemaIndex:
    global _index
    if _index is None or force_refresh:
        _index = SchemaIndex.load(client(), force_refresh=force_refresh)
    return _index


def _out_dir() -> Path:
    root = os.environ.get("VEZA_MCP_OUTPUT_DIR")
    base = Path(root) if root else Path(tempfile.gettempdir()) / "veza-query-mcp"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _err(exc: VezaError) -> dict[str, Any]:
    """Surface Veza's own diagnostics — they carry line/column and the exact
    offending identifier, which is what makes repair possible."""
    return {"ok": False, **exc.as_dict()}


# ─────────────────────────────────────────────── discovery


@mcp.tool()
def veza_health() -> dict[str, Any]:
    """Verify Veza connectivity and credentials, and report schema cache state.

    Cheap. Call this first if anything else behaves unexpectedly.
    """
    try:
        client().readiness()
    except VezaError as exc:
        return _err(exc)
    try:
        return {"ok": True, "connected": True, "schema": index().stats()}
    except VezaError as exc:
        return {"ok": True, "connected": True, "schema_error": exc.as_dict()}


@mcp.tool()
def veza_search_entity_types(
    keyword: str, integration: str | None = None, limit: int = 10
) -> dict[str, Any]:
    """Find Veza node types (the 'tables' of the access graph) by keyword.

    Start here when you don't know the exact type name. There are 851 node types;
    this returns only the closest matches with their integration and property
    count, so it stays small.

    Args:
        keyword: e.g. "okta user", "s3", "group", "role"
        integration: optionally restrict to one integration ("aws", "okta", "azure")
        limit: max results (default 10)
    """
    try:
        idx = index()
    except VezaError as exc:
        return _err(exc)
    return {"ok": True, "matches": idx.search(keyword, integration=integration, limit=limit)}


@mcp.tool()
def veza_describe_entity_type(
    entity_type: str, sample_population: bool = False
) -> dict[str, Any]:
    """Describe a node type: its queryable attributes, groupings, and relationships.

    This is the schema tool to use before writing VQL — attribute names are
    case-sensitive and an invalid one silently returns zero rows.

    Returns a compact view. `reachable_type_count` is a number rather than a list
    because large types reach 500+ others; use veza_list_relationships for those.

    Args:
        entity_type: exact or near-exact type name (e.g. "AzureADUser")
        sample_population: if true, query one page of live entities and report
            which attributes are actually populated. Costs a live query, but the
            schema lists attributes that may be sparsely populated or empty —
            e.g. AzureADUser.last_login_at was set on only 8 of 25 sampled users,
            and AwsIamUser.last_used_at on 1 of 25.
    """
    try:
        idx = index()
    except VezaError as exc:
        return _err(exc)

    nt = idx.resolve(entity_type)
    if not nt:
        if idx.is_grouping(entity_type):
            return {
                "ok": True,
                "grouping": entity_type,
                "note": "This is an entity-type grouping, not a concrete type. It is a valid "
                        "VQL target and spans all member types.",
                "members_sample": idx.grouping_members(entity_type),
            }
        return {
            "ok": False,
            "error": f"unknown node type: {entity_type}",
            "suggestions": idx.search(entity_type, limit=5),
        }

    out = {"ok": True, **nt.lean()}
    if nt.type != entity_type:
        out["casing_corrected_from"] = entity_type

    if sample_population:
        try:
            resp = client().vql_nodes(f"SHOW {nt.type} LIMIT 25;")
            rows = resp.get("values") or []
            counts: dict[str, int] = {}
            for r in rows:
                for k in (r.get("properties") or {}):
                    counts[k] = counts.get(k, 0) + 1
            n = len(rows) or 1
            out["population"] = {
                "sampled_entities": len(rows),
                "populated": {
                    k: f"{v}/{len(rows)}"
                    for k, v in sorted(counts.items(), key=lambda x: -x[1])
                },
                "never_populated_in_sample": sorted(
                    p for p in nt.properties if p not in counts
                ),
            }
        except VezaError as exc:
            out["population_error"] = exc.as_dict()
    return out


@mcp.tool()
def veza_list_relationships(
    from_type: str, contains: str | None = None, limit: int = 40
) -> dict[str, Any]:
    """List valid `RELATED TO` targets for a node type.

    VQL rejects unrelated types with 400 "Node types are not related", so check
    here before composing a relationship query. Large types reach hundreds of
    others — use `contains` to filter.

    Args:
        from_type: source node type
        contains: substring filter on target type names (e.g. "Group", "S3")
        limit: max targets returned (default 40)
    """
    try:
        idx = index()
    except VezaError as exc:
        return _err(exc)
    return {"ok": True, **idx.relationships(from_type, to_filter=contains, limit=limit)}


# ─────────────────────────────────────────────── authoring


@mcp.tool()
def veza_validate_vql(query: str) -> dict[str, Any]:
    """Validate VQL against the live schema BEFORE running it.

    Catches the failure Veza does not report: an invalid attribute name returns
    HTTP 200 with zero rows, which is indistinguishable from a legitimate empty
    result. For a compliance check that means a broken control silently reads as
    a pass — so always validate before executing.

    Also checks node-type existence and casing (both case-sensitive), whether the
    two types are actually related, and flags advisory issues (missing LIMIT,
    missing projection, ENRICH ordering, PATH SUMMARY being non-functional).
    """
    try:
        idx = index()
    except VezaError as exc:
        return _err(exc)
    return {"ok": True, **vqlmod.validate(query, idx)}


@mcp.tool()
def veza_plan_query(requirement: str, max_types: int = 4) -> dict[str, Any]:
    """Resolve a natural-language requirement into concrete schema context.

    Use this when you want to author VQL yourself, or when veza_generate_vql
    returns `blocked_by` items it refused to guess at. Returns the candidate node
    types with their *actual* attribute names, so you are composing against real
    identifiers rather than plausible-sounding ones.
    """
    try:
        idx = index()
    except VezaError as exc:
        return _err(exc)

    stop = {
        "the", "and", "who", "that", "with", "have", "has", "for", "all", "any",
        "show", "find", "list", "which", "their", "them", "are", "not", "from",
        "get", "give", "user", "users", "access", "than", "more", "less", "over",
    }
    terms = [w.strip(",.?").lower() for w in requirement.split()]
    terms = [w for w in terms if len(w) > 2 and w not in stop]

    seen: dict[str, dict[str, Any]] = {}
    for term in terms:
        for m in idx.search(term, limit=3):
            if m["type"] not in seen:
                seen[m["type"]] = {**m, "matched_term": term}
    ranked = list(seen.values())[:max_types]

    detail = []
    for cand in ranked:
        nt = idx.resolve(cand["type"])
        if not nt:
            continue
        # Surface the attributes most likely to matter for a control, rather than
        # all 85 — the full list is what blows the token budget.
        interesting = [
            p for p in sorted(nt.properties)
            if any(k in p for k in (
                "active", "admin", "login", "last_", "created", "mfa", "guest",
                "enabled", "status", "risk", "email", "name", "used", "password",
            ))
        ]
        detail.append({
            "type": nt.type,
            "integration": nt.integration,
            "matched_term": cand.get("matched_term"),
            "groupings": nt.labels,
            "total_attributes": len(nt.properties),
            "likely_relevant_attributes": interesting[:18],
            "reachable_type_count": len(nt.reachable),
        })

    return {
        "ok": True,
        "requirement": requirement,
        "candidate_types": detail,
        "groupings_available": ["Identity", "User", "IdPUser", "LocalUser",
                               "ServiceAccount", "AIAgent", "Resource"],
        "template": ("SHOW <SourceType> [{ projected, fields }] "
                     "[WHERE <attr> <op> <value>] [RELATED TO <DestType>] "
                     "[WITH PATH <IntermediateType>] "
                     "[HAVING entity_result_count > N] "
                     "[RESULT INCLUDE DESTINATION NODES] LIMIT <n>;"),
        "next": "Compose the VQL, then call veza_validate_vql, then veza_execute_vql(mode='count').",
    }


@mcp.tool()
def veza_generate_vql(
    requirement: str,
    verify: bool = True,
    limit: int = 100,
    max_attempts: int = 3,
) -> dict[str, Any]:
    """Generate VQL for a requirement, repair it, and prove it executes.

    Pipeline:
      1. nl2vql            — ask Veza's own natural-language translator
      2. repair            — fix casing / bad identifiers against the schema
      3. validate          — catch the errors Veza won't report (bad attributes
                             return 200 with 0 rows)
      4. execute count     — prove it actually runs; the local parser is shallow,
                             so syntax errors only surface here
      5. repair from error — interpret Veza's diagnostic and retry

    Returns `verified: true` only when the query executed successfully, with its
    row count. Anything the repairer refused to guess at is returned under
    `blocked_by` rather than silently patched — a query quietly rewritten into a
    different question is worse than one that failed, because its results get
    believed.

    Args:
        requirement: plain-language description of what to find
        verify: execute a count to prove the query runs (default true)
        limit: LIMIT injected if absent
        max_attempts: repair/execute cycles before giving up
    """
    try:
        idx = index()
    except VezaError as exc:
        return _err(exc)

    log: list[dict[str, Any]] = []
    candidate: str | None = None

    # 1. nl2vql first — it is good, and free of our own biases about phrasing.
    try:
        resp = client().nl2vql(requirement)
        candidate = (resp.get("value") or "").strip() or None
        log.append({"step": "nl2vql", "ok": bool(candidate), "query": candidate})
    except VezaError as exc:
        # /api/private/ — treat unavailability as expected, not exceptional.
        log.append({"step": "nl2vql", "ok": False, "error": exc.code})

    if not candidate:
        plan = veza_plan_query(requirement)
        return {
            "ok": True,
            "verified": False,
            "requirement": requirement,
            "attempts": log,
            "reason": "nl2vql unavailable or empty; no candidate to repair",
            "plan": plan,
            "next": "Author VQL from `plan`, then veza_validate_vql + veza_execute_vql.",
        }

    fixes_applied: list[dict[str, Any]] = []
    for attempt in range(1, max_attempts + 1):
        # 2+3. repair to schema-validity
        rr = repairmod.repair_until_valid(candidate, idx, limit=limit)
        candidate = rr["query"]
        if rr["fixes"]:
            fixes_applied.extend(rr["fixes"])
            log.append({"step": f"repair#{attempt}", "fixes": len(rr["fixes"]),
                        "query": candidate})

        if not rr["valid"]:
            return {
                "ok": True,
                "verified": False,
                "requirement": requirement,
                "vql": candidate,
                "attempts": log,
                "fixes": fixes_applied,
                "errors": rr.get("errors", []),
                "blocked_by": rr.get("blocked_by", []),
                "plan": veza_plan_query(requirement),
                "next": ("Schema validation failed on something that cannot be safely "
                         "auto-corrected. Use `plan` to pick the right types/attributes."),
            }

        if not verify:
            return {"ok": True, "verified": False, "vql": candidate,
                    "validated": True, "fixes": fixes_applied, "attempts": log,
                    "warnings": rr.get("warnings", [])}

        # 4. prove it runs — this is where syntax errors surface
        try:
            res = client().vql_count(candidate)
            log.append({"step": f"execute#{attempt}", "ok": True})
            return {
                "ok": True,
                "verified": True,
                "requirement": requirement,
                "vql": candidate,
                "count": int(res.get("number_value") or 0),
                "fixes": fixes_applied,
                "substitutions": [f for f in fixes_applied if f.get("kind") == "substituted"],
                "warnings": rr.get("warnings", []),
                "attempts": log,
                "next": "Fetch rows with veza_execute_vql(vql, mode='rows').",
            }
        except VezaError as exc:
            # 5. interpret and retry
            interp = repairmod.interpret_api_error(exc.violations or [exc.message], idx)
            log.append({"step": f"execute#{attempt}", "ok": False,
                        "error": interp.get("interpretation")})
            auto = interp.get("auto_fix")
            if auto and attempt < max_attempts:
                candidate = candidate.replace(auto["replace"], auto["with"])
                log.append({"step": f"error_repair#{attempt}",
                            "applied": auto, "query": candidate})
                fixes_applied.append({
                    "kind": "substituted", "what": "node type (from API error)",
                    "detail": f"server rejected '{auto['replace']}'",
                    "before": auto["replace"], "after": auto["with"],
                })
                continue
            return {
                "ok": True,
                "verified": False,
                "requirement": requirement,
                "vql": candidate,
                "attempts": log,
                "fixes": fixes_applied,
                "api_error": {**exc.as_dict(), **interp},
                "plan": veza_plan_query(requirement),
                "next": "The query is schema-valid but the server rejected it. See api_error.interpretation.",
            }

    return {"ok": True, "verified": False, "vql": candidate, "attempts": log,
            "fixes": fixes_applied, "reason": f"exhausted {max_attempts} attempts"}


# ─────────────────────────────────────────────── execution


@mcp.tool()
def veza_execute_vql(
    query: str,
    mode: Literal["count", "rows"] = "count",
    limit: int = DEFAULT_ROW_LIMIT,
    skip_validation: bool = False,
) -> dict[str, Any]:
    """Execute VQL. Defaults to `count` because counts are ~17 tokens and rows are not.

    mode="count": total population size. Use this to verify a query works and to
        size the result before fetching anything.
    mode="rows": fetches rows, writes the full set to a file, and returns only a
        small inline sample plus the file path. Rows cost ~730-1,100 tokens each,
        so a 500-row result would otherwise consume ~365k tokens.

    Args:
        query: the VQL string
        mode: "count" (default) or "rows"
        limit: max rows to fetch when mode="rows" (a LIMIT is injected if absent)
        skip_validation: bypass local schema validation (not recommended — an
            invalid attribute returns 0 rows without erroring)
    """
    if not skip_validation:
        try:
            verdict = vqlmod.validate(query, index())
            if not verdict["valid"]:
                return {
                    "ok": False,
                    "error": "validation_failed",
                    "hint": "An invalid attribute would return 0 rows without an error. "
                            "Fix these, or pass skip_validation=true to override.",
                    "errors": verdict["errors"],
                    "warnings": verdict["warnings"],
                }
        except VezaError as exc:
            return _err(exc)

    if mode == "count":
        try:
            resp = client().vql_count(query)
        except VezaError as exc:
            return _err(exc)
        return {
            "ok": True,
            "mode": "count",
            "count": int(resp.get("number_value") or 0),
            "result_type": resp.get("result_type"),
            "warnings": resp.get("warnings") or [],
        }

    bounded = vqlmod.add_limit(query, limit)
    try:
        resp = client().vql_nodes(bounded)
    except VezaError as exc:
        return _err(exc)

    # Both keys are always present; which one is populated depends on query shape.
    values = resp.get("values") or []
    paths = resp.get("path_values") or []
    rows = paths or values
    shape = "path_values" if paths else "values"

    path = _out_dir() / f"vql_{int(time.time()*1000)}.json"
    try:
        path.write_text(json.dumps({"query": bounded, "shape": shape, "rows": rows}, indent=1))
        written: str | None = str(path)
    except OSError:
        written = None

    def summarize(r: dict[str, Any]) -> dict[str, Any]:
        if shape == "values":
            return {
                "id": r.get("id"),
                "type": r.get("type"),
                "risk_level": r.get("risk_level"),
                "properties": r.get("properties") or {},
            }
        src, dst = r.get("source") or {}, r.get("destination") or {}
        return {
            "source": {"type": src.get("type"), "properties": src.get("properties") or {}},
            "destination": {"type": dst.get("type"), "name": (dst.get("properties") or {}).get("name")},
            "abstract_permissions": r.get("abstract_permissions"),
            "concrete_permissions": r.get("concrete_permissions"),
        }

    fields = sorted((rows[0].get("properties") or {}).keys()) if (rows and shape == "values") else []
    return {
        "ok": True,
        "mode": "rows",
        "query_executed": bounded,
        "shape": shape,
        "rows_returned": len(rows),
        "has_more": bool(resp.get("has_more")),
        "next_page_token": resp.get("next_page_token") or None,
        "field_names": fields,
        "sample": [summarize(r) for r in rows[:INLINE_SAMPLE]],
        "full_results_path": written,
        "note": (
            f"Showing {min(len(rows), INLINE_SAMPLE)} of {len(rows)} rows inline to conserve "
            "context. The complete result set is at full_results_path."
        ),
        "warnings": resp.get("warnings") or [],
    }


@mcp.tool()
def veza_find_example_queries(topic: str, limit: int = 10) -> dict[str, Any]:
    """Search Veza's built-in saved queries for worked examples.

    Veza ships 500+ curated queries annotated with risk level and risk profile —
    useful grounding for what a control should look like. The full listing is
    ~1.6MB, so this searches server-side and returns only matches.

    Note: the built-ins' `vql_query` field is empty (they are defined structurally,
    not as VQL text), so these give you intent and metadata rather than copyable VQL.
    """
    try:
        resp = client().saved_queries(page_size=500)
    except VezaError as exc:
        return _err(exc)
    t = topic.lower().strip()
    out = []
    for q in resp.get("values") or []:
        name = q.get("name") or ""
        if t and t not in name.lower() and not any(
            t in (lab or "").lower() for lab in (q.get("labels") or [])
        ):
            continue
        out.append({
            "id": q.get("id"),
            "name": name,
            "risk_level": q.get("risk_level"),
            "risk_profiles": q.get("risk_profiles"),
            "node_relationship_type": q.get("node_relationship_type"),
            "integration_types": (q.get("integration_types") or [])[:6],
        })
        if len(out) >= limit:
            break
    return {
        "ok": True,
        "matches": out,
        "note": "Fetch rows for a saved query with GET /api/v1/assessments/queries/{id}:nodes. "
                "result_type is 'NUMBER' on all built-ins but :nodes still returns rows.",
    }


def main() -> None:
    """Entry point. Defaults to stdio; streamable-http is opt-in.

    stdio is the right default — it is what desktop MCP clients launch, and it
    keeps the Veza API key in the child process's environment rather than
    exposing a listening port.

    Override with --transport, or VEZA_MCP_TRANSPORT for environments where
    passing args is awkward:

        veza-query-mcp                                   # stdio
        veza-query-mcp --transport streamable-http        # http on 127.0.0.1:8000/mcp
        veza-query-mcp --transport streamable-http --host 0.0.0.0 --port 9000
    """
    import argparse

    parser = argparse.ArgumentParser(prog="veza-query-mcp", description=__doc__)
    parser.add_argument(
        "--transport",
        choices=["stdio", "streamable-http", "sse"],
        default=os.environ.get("VEZA_MCP_TRANSPORT", "stdio"),
        help="Transport to serve on (default: stdio)",
    )
    parser.add_argument("--host", default=os.environ.get("VEZA_MCP_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("VEZA_MCP_PORT", "8000")))
    parser.add_argument("--path", default=os.environ.get("VEZA_MCP_PATH", "/mcp"))
    args = parser.parse_args()

    # Fail fast with a clear message rather than surfacing auth errors per-tool.
    try:
        VezaClient()
    except RuntimeError as exc:
        raise SystemExit(f"veza-query-mcp: {exc}") from exc

    if args.transport == "stdio":
        mcp.run("stdio")
        return

    # Anything network-facing has no authentication of its own — it would expose
    # read access to the Veza tenant to whoever can reach the port. Keep it bound
    # to localhost unless the operator explicitly opts out.
    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        print(
            f"veza-query-mcp: WARNING serving on {args.host}:{args.port} with no "
            "authentication — anyone who can reach this port gets read access to "
            "the Veza tenant. Bind to 127.0.0.1 or put an authenticating proxy in front.",
            flush=True,
        )
    mcp.run(args.transport, host=args.host, port=args.port, streamable_http_path=args.path)


if __name__ == "__main__":
    main()
