"""Graph schema index.

The full Veza graph schema is ~4MB / ~1M tokens for 851 node types. It must
never reach a model's context. This module fetches it once, indexes it
server-side, caches it to disk, and exposes only small slices.

It is also the only place that can catch VQL's worst failure mode: an invalid
*attribute* name returns HTTP 200 with zero rows rather than an error, so a
typo is indistinguishable from a legitimate "nothing matched". Node types do
hard-fail (400), but attributes do not — hence local validation.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .client import VezaClient

CACHE_TTL_SECONDS = 24 * 3600


def _cache_path() -> Path:
    root = os.environ.get("VEZA_MCP_CACHE_DIR")
    base = Path(root) if root else Path.home() / ".cache" / "veza-query-mcp"
    base.mkdir(parents=True, exist_ok=True)
    return base / "graph_schema.json"


@dataclass
class NodeType:
    type: str
    name: str
    group: str
    integration: str | None
    labels: list[str]
    properties: dict[str, dict[str, Any]]  # name -> {type?, optional?}
    reachable: set[str]
    out_edges: list[tuple[str, str]] = field(default_factory=list)

    def lean(self, include_reachable_sample: int = 0) -> dict[str, Any]:
        """Compact form safe to return to a model (~1.2k tokens for a big type).

        Notably returns reachable as a COUNT, not a list: AzureADUser has 543
        reachable types (~14.7KB / 3.7k tokens on its own).
        """
        out: dict[str, Any] = {
            "type": self.type,
            "display_name": self.name,
            "group": self.group,
            "integration": self.integration,
            "groupings": self.labels,
            "property_count": len(self.properties),
            "properties": [
                {"name": n, **{k: v for k, v in meta.items() if v is not None}}
                for n, meta in sorted(self.properties.items())
            ],
            "reachable_type_count": len(self.reachable),
        }
        if include_reachable_sample:
            out["reachable_sample"] = sorted(self.reachable)[:include_reachable_sample]
        return out


class SchemaIndex:
    def __init__(self, nodes: dict[str, NodeType], fetched_at: float) -> None:
        self.nodes = nodes
        self.fetched_at = fetched_at
        # Case-insensitive lookup so we can correct a model's casing rather than
        # just rejecting it — VQL node types and attributes ARE case-sensitive.
        self._ci = {t.lower(): t for t in nodes}

    # ── construction ──────────────────────────────────────────────────────

    @staticmethod
    def _index(raw: dict[str, Any]) -> dict[str, NodeType]:
        nodes: dict[str, NodeType] = {}
        for n in raw.get("schema", {}).get("nodes", []):
            props: dict[str, dict[str, Any]] = {}
            for p in n.get("properties") or []:
                name = p.get("name")
                if not name:
                    continue
                props[name] = {
                    "type": p.get("type"),
                    "optional": p.get("optional"),
                }
            nodes[n["type"]] = NodeType(
                type=n["type"],
                name=n.get("name") or n["type"],
                group=n.get("group") or "",
                integration=n.get("integration_type"),
                labels=list(n.get("labels") or []),
                properties=props,
                reachable=set(n.get("reachable_node_types") or []),
                out_edges=[
                    (e.get("label", ""), e.get("node_type", ""))
                    for e in (n.get("out_edges") or [])
                ],
            )
        return nodes

    @classmethod
    def load(cls, client: VezaClient, *, force_refresh: bool = False) -> SchemaIndex:
        path = _cache_path()
        if not force_refresh and path.exists():
            age = time.time() - path.stat().st_mtime
            if age < CACHE_TTL_SECONDS:
                try:
                    raw = json.loads(path.read_text())
                    return cls(cls._index(raw), path.stat().st_mtime)
                except (json.JSONDecodeError, KeyError):
                    pass  # fall through to refetch

        raw = client.graph_schema(unfiltered=True)
        try:
            path.write_text(json.dumps(raw))
        except OSError:
            pass  # cache is an optimisation, not a requirement
        return cls(cls._index(raw), time.time())

    # ── lookups ───────────────────────────────────────────────────────────

    def resolve(self, type_name: str) -> NodeType | None:
        """Exact match, else case-insensitive — so we can tell a caller the
        correct casing instead of a bare failure."""
        if type_name in self.nodes:
            return self.nodes[type_name]
        canonical = self._ci.get(type_name.lower())
        return self.nodes.get(canonical) if canonical else None

    def canonical_name(self, type_name: str) -> str | None:
        nt = self.resolve(type_name)
        return nt.type if nt else None

    def search(
        self, keyword: str, *, integration: str | None = None, limit: int = 10
    ) -> list[dict[str, Any]]:
        kw = keyword.lower().strip()
        scored: list[tuple[int, NodeType]] = []
        for nt in self.nodes.values():
            if integration and (nt.integration or "").lower() != integration.lower():
                continue
            hay_type = nt.type.lower()
            if kw and kw not in hay_type and kw not in nt.name.lower() and not any(
                kw in lab.lower() for lab in nt.labels
            ):
                continue
            # exact > prefix > substring, so "OktaUser" beats "OktaUserFactor"
            if hay_type == kw:
                rank = 0
            elif hay_type.startswith(kw):
                rank = 1
            elif kw in hay_type:
                rank = 2
            else:
                rank = 3
            scored.append((rank, nt))
        scored.sort(key=lambda x: (x[0], len(x[1].type), x[1].type))
        return [
            {
                "type": nt.type,
                "group": nt.group,
                "integration": nt.integration,
                "property_count": len(nt.properties),
            }
            for _, nt in scored[:limit]
        ]

    def relationships(
        self, from_type: str, *, to_filter: str | None = None, limit: int = 40
    ) -> dict[str, Any]:
        nt = self.resolve(from_type)
        if not nt:
            return {"error": f"unknown node type: {from_type}", "suggestions": self.search(from_type, limit=5)}
        reach = sorted(nt.reachable)
        if to_filter:
            f = to_filter.lower()
            reach = [r for r in reach if f in r.lower()]
        return {
            "from": nt.type,
            "total_reachable": len(nt.reachable),
            "returned": min(len(reach), limit),
            "reachable": reach[:limit],
            "direct_edges": [
                {"label": lab, "node_type": tgt} for lab, tgt in nt.out_edges[:limit]
            ],
        }

    def can_relate(self, from_type: str, to_type: str) -> bool | None:
        """None means we cannot tell (unknown source type)."""
        nt = self.resolve(from_type)
        if not nt:
            return None
        target = self.canonical_name(to_type) or to_type
        if target in nt.reachable:
            return True
        # Groupings (User, Identity, Resource...) are valid query targets but are
        # not themselves listed in reachable_node_types, so treat any reachable
        # member of that grouping as satisfying the relation.
        for r in nt.reachable:
            rt = self.nodes.get(r)
            if rt and target in rt.labels:
                return True
        return False

    def is_grouping(self, name: str) -> bool:
        """True if `name` is used as a grouping label rather than a concrete type."""
        canonical = self.canonical_name(name)
        if canonical and canonical in self.nodes:
            return False
        lowered = name.lower()
        return any(
            lowered == lab.lower() for nt in self.nodes.values() for lab in nt.labels
        )

    def grouping_members(self, label: str, limit: int = 25) -> list[str]:
        low = label.lower()
        return sorted(
            nt.type
            for nt in self.nodes.values()
            if any(low == lab.lower() for lab in nt.labels)
        )[:limit]

    def unknown_properties(self, type_name: str, props: Iterable[str]) -> list[str]:
        nt = self.resolve(type_name)
        if not nt:
            return []
        return [p for p in props if p not in nt.properties]

    def stats(self) -> dict[str, Any]:
        integrations = sorted({nt.integration for nt in self.nodes.values() if nt.integration})
        return {
            "node_types": len(self.nodes),
            "integrations": len(integrations),
            "integration_list": integrations,
            "cache_age_seconds": int(time.time() - self.fetched_at),
        }
