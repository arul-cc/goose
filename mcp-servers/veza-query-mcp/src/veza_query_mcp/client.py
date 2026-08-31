"""Read-only HTTP client for the Veza API.

Deliberately narrow: only the endpoints this MCP needs are reachable, and every
one of them reads. There is no generic passthrough — a caller cannot use this to
mutate the tenant.
"""

from __future__ import annotations

import math
import os
import time
from dataclasses import dataclass
from typing import Any

import httpx

# Retry policy mirrors Veza's own SDK. 401 is deliberately absent: bad
# credentials must fail fast rather than hammer the tenant.
RETRY_STATUS = {429, 500, 502, 503, 504}
MAX_RETRIES = 5
BACKOFF_FACTOR = 0.6
BACKOFF_CAP = 30.0

# No rate limits are published, but Veza's own CLI paces query calls ~300ms
# apart and its SDK retries 429 — so limiting exists. Be conservative.
MIN_CALL_INTERVAL = 0.3

DEFAULT_TIMEOUT = 120.0


class VezaError(RuntimeError):
    """An error returned by the Veza API, with the detail needed to repair it."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int | None = None,
        request_id: str | None = None,
        violations: list[str] | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.status = status
        self.request_id = request_id
        self.violations = violations or []
        detail = "; ".join(self.violations) if self.violations else message
        # request_id is the only handle Veza support can act on — always carry it.
        super().__init__(f"{code}: {detail} (http={status} request_id={request_id})")

    def as_dict(self) -> dict[str, Any]:
        return {
            "error": self.code,
            "message": self.message,
            "violations": self.violations,
            "http_status": self.status,
            "request_id": self.request_id,
        }


@dataclass
class VezaConfig:
    base_url: str
    api_key: str
    timeout: float = DEFAULT_TIMEOUT

    @classmethod
    def from_env(cls) -> VezaConfig:
        url = os.environ.get("VEZA_URL", "").strip().rstrip("/")
        key = os.environ.get("VEZA_API_KEY", "").strip()
        if not url or not key:
            raise RuntimeError(
                "VEZA_URL and VEZA_API_KEY must be set. Typically: source ~/.veza.env"
            )
        if not url.startswith("http"):
            url = f"https://{url}"
        return cls(base_url=url, api_key=key)


def _parse_error(resp: httpx.Response) -> VezaError:
    """Extract Veza's structured error detail.

    Veza's errors are unusually good: details[].field_violations[].description
    carries the specific problem, often with line/column for VQL syntax errors
    ("no viable alternative at input 'RESULT INCLUDE'") or the exact offending
    identifier ("WorkdayEmployee is not a valid NodeType"). That is what makes
    an automated repair loop viable, so surface it rather than flattening it.
    """
    code, message, request_id = "HTTPError", resp.reason_phrase or "request failed", None
    violations: list[str] = []
    try:
        body = resp.json()
    except Exception:
        return VezaError(code, message, status=resp.status_code)

    code = str(body.get("code") or code)
    message = str(body.get("message") or message)
    request_id = body.get("request_id")
    for detail in body.get("details") or []:
        for fv in detail.get("field_violations") or []:
            desc = (fv.get("description") or "").strip()
            if desc:
                violations.append(" ".join(desc.split()))
    return VezaError(
        code, message, status=resp.status_code, request_id=request_id, violations=violations
    )


class VezaClient:
    def __init__(self, config: VezaConfig | None = None) -> None:
        self.config = config or VezaConfig.from_env()
        self._client = httpx.Client(
            base_url=self.config.base_url,
            timeout=self.config.timeout,
            headers={
                "authorization": f"Bearer {self.config.api_key}",
                "content-type": "application/json",
                "user-agent": "veza-query-mcp/0.1",
            },
        )
        self._last_call = 0.0

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> VezaClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_call
        if elapsed < MIN_CALL_INTERVAL:
            time.sleep(MIN_CALL_INTERVAL - elapsed)
        self._last_call = time.monotonic()

    def _request(self, method: str, path: str, *, json_body: Any = None) -> dict[str, Any]:
        last_error: VezaError | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            self._throttle()
            try:
                resp = self._client.request(method, path, json=json_body)
            except httpx.TimeoutException as exc:
                last_error = VezaError("Timeout", str(exc))
                if attempt == MAX_RETRIES:
                    raise last_error from exc
                time.sleep(min(BACKOFF_FACTOR * math.pow(2, attempt - 1), BACKOFF_CAP))
                continue

            if resp.status_code in RETRY_STATUS and attempt < MAX_RETRIES:
                time.sleep(min(BACKOFF_FACTOR * math.pow(2, attempt - 1), BACKOFF_CAP))
                continue
            if resp.status_code >= 400:
                raise _parse_error(resp)

            body = resp.json()
            # A 200 can still carry warnings — Veza documents that "successful
            # responses and warnings are always 200 OK". Never treat 200 as
            # unconditionally clean; propagate warnings to the caller.
            return body
        raise last_error or VezaError("Unknown", "request failed")

    def get(self, path: str) -> dict[str, Any]:
        return self._request("GET", path)

    def post(self, path: str, body: Any) -> dict[str, Any]:
        return self._request("POST", path, json_body=body)

    # ── Endpoints (read-only allowlist) ────────────────────────────────────

    def readiness(self) -> dict[str, Any]:
        """Cheapest call that validates host + credentials; 401 on bad key."""
        return self.get("/api/v1/providers/custom/templates")

    def vql_count(self, query: str) -> dict[str, Any]:
        """~17 tokens of response. Use for validation and population sizing."""
        return self.post("/api/v1/assessments/vql:result", {"query": query})

    def vql_nodes(self, query: str) -> dict[str, Any]:
        return self.post("/api/v1/assessments/vql:nodes", {"query": query})

    def vql_autocomplete(self, partial: str) -> dict[str, Any]:
        return self.post("/api/v1/assessments/vql:autocomplete", {"query": partial})

    def nl2vql(self, requirement: str) -> dict[str, Any]:
        """Veza's own natural-language → VQL. NOTE: /api/private/ — unsupported
        namespace, may change or disappear without notice. Always validate its
        output and be prepared to fall back."""
        return self.post("/api/private/assessments/nl2vql", {"query": requirement})

    def graph_schema(self, unfiltered: bool = True) -> dict[str, Any]:
        """~4MB. Fetch once, cache, never return raw to a model."""
        return self.post("/graph/private/schema", {"unfiltered": unfiltered})

    def saved_queries(self, page_size: int = 500, page_token: str | None = None) -> dict[str, Any]:
        path = f"/api/v1/assessments/queries?page_size={page_size}"
        if page_token:
            path += f"&page_token={page_token}"
        return self.get(path)

    def providers(self, page_size: int = 200) -> dict[str, Any]:
        return self.get(f"/api/v1/providers?page_size={page_size}")
