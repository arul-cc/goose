# Veza Query MCP

Read-only MCP server for querying the **Veza Access Graph** with VQL. Discovers the
graph schema, generates VQL, **validates it against the schema**, and executes it
without destroying the caller's token budget.

## Why validation is the point

```
SHOW NotARealType LIMIT 1;                      →  400  "NotARealType is not a valid NodeType"
SHOW OktaUser WHERE bogus_attr = true LIMIT 1;  →  200  count = 0     ← no error
```

Veza rejects invalid **node types** but accepts invalid **attribute names**, returning
zero rows. A typo is therefore indistinguishable from a legitimate "nothing matched" —
so a broken compliance check reads as a pass.

This server validates every node type *and attribute* against a cached copy of the
graph schema before anything executes. That is its main reason to exist.

## Install

Requires [`uv`](https://docs.astral.sh/uv/) and Python 3.12+. `uv` fetches the
interpreter itself, so Python does not need to be installed first.

**From the source bundle** (`veza_query_mcp-<version>.tar.gz`) — use this if you want
to read, modify, or test the code:

```bash
tar -xzf veza_query_mcp-0.1.0.tar.gz && cd veza_query_mcp-0.1.0
uv sync          # installs the exact versions in uv.lock
uv run pytest -q # 37 tests, offline — verifies the build with no tenant needed
```

**From the wheel** (`veza_query_mcp-<version>-py3-none-any.whl`) — use this to just run
it, with no checkout:

```bash
uvx --from ./veza_query_mcp-0.1.0-py3-none-any.whl veza-query-mcp
```

The wheel carries no lockfile, so `uvx` resolves `mcp` and `httpx` fresh. Prefer the
source bundle when a reproducible dependency set matters.

## Credentials

Never commit these. Keep them outside the repo:

```bash
cat > ~/.veza.env <<'EOF'
export VEZA_URL="https://<tenant>.vezacloud.com"
export VEZA_API_KEY="<key>"
EOF
chmod 600 ~/.veza.env
source ~/.veza.env
```

An `OAuth2 Client` with `viewer` scope would be the better production credential
(read-only is then enforced by the credential itself, not just by this code), but it
is Early Access and untested here.

## Run

Transport is **stdio by default**. `streamable-http` and `sse` are opt-in.

```bash
source ~/.veza.env

uv run veza-query-mcp                                    # stdio (default)
uv run veza-query-mcp --transport streamable-http        # http://127.0.0.1:8000/mcp
uv run veza-query-mcp --transport streamable-http --port 9000 --path /mcp
```

Equivalent env vars, for launchers that can't pass args: `VEZA_MCP_TRANSPORT`,
`VEZA_MCP_HOST`, `VEZA_MCP_PORT`, `VEZA_MCP_PATH`.

Credentials are checked at startup, so a misconfiguration fails immediately rather
than as a confusing error inside every tool call.

### Which transport?

| | stdio | streamable-http |
|---|---|---|
| Use when | A desktop/CLI client launches the server itself | The server runs as a shared service, in a container, or on another host |
| Credential exposure | Key stays in the child process env | Key lives with the server; **the port has no auth** |
| Lifecycle | One process per client | Long-running |

**Prefer stdio.** It has no listening socket, so there is nothing to expose. The HTTP
transport has **no authentication of its own** — anyone who can reach the port gets
read access to the Veza tenant. It binds to `127.0.0.1` by default, and warns loudly
if you override that. Put an authenticating proxy in front before exposing it.

### Register with a client (stdio)

```json
{
  "mcpServers": {
    "veza-query": {
      "command": "uv",
      "args": ["run", "--directory", "/absolute/path/to/veza-query-mcp", "veza-query-mcp"],
      "env": {
        "VEZA_URL": "https://<tenant>.vezacloud.com",
        "VEZA_API_KEY": "<key>"
      }
    }
  }
}
```

For Claude Code specifically:

```bash
claude mcp add veza-query --env VEZA_URL=https://<tenant>.vezacloud.com --env VEZA_API_KEY=<key> \
  -- uv run --directory /absolute/path/to/veza-query-mcp veza-query-mcp
```

`--directory` matters: without it `uv` resolves the project from the client's working
directory, not this one.

### Verify

```bash
source ~/.veza.env
uv run python -c "
from veza_query_mcp.server import veza_health
print(veza_health())"
```

Expect `connected: True` and `node_types: 851`. First call fetches the ~4MB schema and
caches it to `~/.cache/veza-query-mcp/`; later calls are instant.

## Tools

| Tool | Purpose |
|---|---|
| `veza_health` | Connectivity, credentials, schema cache state |
| `veza_search_entity_types` | Find node types by keyword (851 exist; returns ~10) |
| `veza_describe_entity_type` | Attributes, groupings, relationship count. `sample_population=true` also reports which attributes are *actually populated* |
| `veza_list_relationships` | Valid `RELATED TO` targets, filtered |
| `veza_validate_vql` | Schema validation — node types, casing, relatedness, **attributes** |
| `veza_plan_query` | Requirement → candidate node types and relationships, before committing to a query |
| `veza_generate_vql` | Natural language → validated VQL (see below) |
| `veza_execute_vql` | `mode="count"` (~17 tokens) or `mode="rows"` (sample inline, full set to file) |
| `veza_find_example_queries` | Search Veza's 500+ built-in queries for worked examples |

### Generation strategy

`veza_generate_vql` asks **Veza's own `nl2vql`** first, then validates the result:

```
requirement ──▶ nl2vql ──▶ validate ──┬─ valid ──▶ return VQL
                                      └─ invalid ─▶ return schema hints for the
                                                    caller to author it directly
```

`nl2vql` produces good VQL (`"active Okta users with access to S3 buckets"` →
`Show OktaUser WHERE is_active = true related to S3Bucket`), but it lives on an
unsupported `/api/private/` path and is not schema-checked — so its output is always
validated, and the fallback also covers it disappearing.

## Token discipline

Measured against a live tenant (byte/4 approximation — a floor, not a ceiling):

| Payload | ≈ Tokens |
|---|---|
| Full graph schema (851 types) | **~1,000,000** |
| 500 saved-queries listing | **~409,000** |
| `describe_entity_type` (lean) | ~1,200 |
| One access-path row | ~730–1,100 |
| **Count** | **~17** |

Consequences, baked into the tools:

- The schema is indexed server-side and cached to `~/.cache/veza-query-mcp/`. It is
  never returned raw.
- `reachable_node_types` is returned as a **count** (`AzureADUser` reaches 543 types —
  ~3.7k tokens as a list).
- `execute_vql` defaults to `mode="count"`.
- `mode="rows"` writes the full result set to a file and returns a 5-row sample plus
  the path. 500 rows inline would be ~365k tokens.
- Property projection (`SHOW X { a, b }`) cuts a source-only row from 44 properties to
  4. Use it.

Authoring and verifying one control should land around **3–6k tokens**.

## Schema presence ≠ data populated

The API omits unpopulated properties, so a property existing on a type does not mean
it has values:

| Field | In schema | Populated (25 sampled) |
|---|---|---|
| `AzureADUser.last_login_at` | yes | 8/25 |
| `AwsIamUser.last_used_at` | yes | 1/25 |
| `AwsIamUser.programmatic_access_count` | yes | 25/25 |

Use `veza_describe_entity_type(..., sample_population=True)` before building a control
on a sparse field. **Never conclude a field is absent from a single instance.**

## Known Veza behaviours encoded here

| Behaviour | Handling |
|---|---|
| Invalid attribute → 200/0 rows | Local schema validation (the core feature) |
| Node types & attributes case-sensitive; keywords are not; `;` optional | Casing corrected with a suggestion |
| `values` and `path_values` both always present | Both read; shape reported |
| `RESULT INCLUDE PATH SUMMARY` returns empty `path_summary_nodes` | Warned; query group membership as a direct relationship instead |
| `ENRICH` must follow `RESULT INCLUDE`, and needs correlated types | Warned |
| `IS NULL` / `IS NOT NULL` work but are undocumented | Parsed and validated |
| `result_type` is `"NUMBER"` on all built-ins, yet `:nodes` returns rows | Not treated as a gate |
| Enums serialize as **strings** despite the spec declaring integers | Read as strings |
| Errors carry `request_id` + line/column detail | Surfaced verbatim for repair |

## Read-only guarantee

The client exposes a fixed allowlist of read endpoints — there is no generic
passthrough, so this server cannot mutate the tenant. Note the honest limit: with a
**personal API key**, read-only comes from this code plus the role on the key's user,
not from the credential. Use an `OAuth2 Client` with `viewer` scope for
credential-level enforcement.

## Namespace risk

The schema endpoints (`/graph/private/schema*`) and `nl2vql` are on Veza's
**private** namespace — 70% of their API surface, with no stability commitment.
Mitigations: the on-disk schema cache, and the option to ship a pre-built schema
snapshot. Worth asking Veza to promote the graph schema endpoint to GA.

## Tests

```bash
uv run pytest -q      # 37 tests, offline — no tenant or network needed
```

## Background

Full API analysis (auth, wire protocol, endpoints by namespace tier, live-tenant
findings) is in the parent directory:
`Veza-ReadOnly-Integration-Analysis.md` (§12 = live verification).
