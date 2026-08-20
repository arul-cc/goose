# ACP Migration Plan (Option C)

Upstream removed the REST `goose-server` crate (commit `6d699361a`, #10224) and is
now **ACP-only** (`crates/goose/src/acp/`). To keep tracking upstream, the
ComplianceCow stack must move from REST/SSE to ACP. This is the plan.

> **Hard constraint: no ComplianceCow feature may be lost.** Every feature in
> `COMPLIANCECOW_FORK_FEATURES.md` must be preserved or consciously re-homed, and
> verified against the golden reference below.

## Safety net (already in place)
- **`golden/rest-features-20260707`** (tag → `eda34b93f`) — the last fully-working,
  all-features REST-era branch. **Behavioral source of truth.** Never delete; every
  migrated feature is validated to behave identically to this.
- Branches `backup/dev_2-pre-sync-20260705`, `backup/pre-sync-20260707`, and
  `sync-upstream-20260705` (== golden) are kept until migration is fully validated.
- Last REST-capable upstream commit (fallback base): `651dc973c`.

## Two tracks

### Track A — goose (Rust): re-home fork features into ACP
| Fork feature | REST location (deleted) | New ACP home (target) | Notes |
|---|---|---|---|
| §1–3 per-session provider (api_key/host) | `goose-server/routes/agent.rs` `update_agent_provider` | `acp/server/providers.rs` + the `update_provider` ACP method | ACP `update_provider` exists; add `api_key`/`host`. `create_with_api_key`/`from_api_key` in `providers/{init,openai_def,anthropic_def}.rs` are core — survive as-is. |
| §4 header injection — DynamicHeaderClient | `agents/extension_manager.rs` (core) | *unchanged* (core crate) | Survives the goose-server removal; only normal rebase conflicts. |
| §4 injection — `websocket_headers.v0` into session | `StartAgentRequest.extension_data` + `PUT /sessions/{id}/extension_data` | `acp/server/new_session.rs` (`extension_data`/`meta`) + `acp/server/extensions.rs` | **SPIKE:** confirm ACP can set *raw* `extension_data` keys (not just extensions). If not, add a custom ACP method. |
| §6 Anthropic `metadata.user_id` | `routes/agent.rs` `update_agent_provider` | `acp/server/providers.rs` (update_provider path) | provider-side (`goose-providers/anthropic.rs` stream) is core — survives. |
| §6 configurable caching (`ANTHROPIC_DISABLE_CACHE`/TTL) | `goose-provider-types/formats/anthropic.rs` (core) | *unchanged* | Survives. |
| §9 DeepSeek thinking-disable + v4 | `goose-provider-types/formats/openai.rs`, `deepseek.json` (core) | *unchanged* | Survives. |
| §10 recipe instruction injection | `routes/agent.rs` `start_agent` | `acp/server/new_session.rs` / `acp/server/recipe/` | ACP already applies recipes on new_session — verify our injection point is covered or add it. |
| DB pool size (`GOOSE_DB_MAX_CONNECTIONS`) | `session/session_manager.rs` (core) | *unchanged* | Survives. |
| Branding (moocp/ComplianceCow) | `prompts/*.md` (core) | *unchanged* | Re-check after each rebase. |

**Takeaway:** most features are *core* (provider/format/session/prompt) and survive
the goose-server removal — they need only normal rebase conflict resolution. Only
the **REST-route features (§1–3, §4 injection, §6 user_id, §10)** must be re-homed
into `acp/server/`.

### Track B — CowGooseService (Go): REST/SSE client → ACP client
The bridge's job changes from "WS ⇄ goose REST+SSE" to "WS ⇄ goose **ACP** (JSON-RPC
2.0)". This is the larger lift and lives in the CowGooseService repo.
- Replace the REST client (`gooseclient/`) + SSE bridge with an ACP JSON-RPC client
  over the ACP transport (stdio or WS — decide in the spike).
- Map each REST call to its ACP method: `/agent/start` → `session/new`,
  `/sessions/{id}/reply` → `session/prompt`, `/sessions/{id}/events` → ACP
  streaming notifications, `update_provider` → ACP `update_provider`,
  `extension_data` → the §4 mechanism decided in Track A, `fork` → `session/fork`,
  `cancel` → ACP cancel.
- Per-session provider/model/key + `request_params` + `websocket_headers` still flow
  the same conceptually — only the transport/encoding changes.

## Phase 1 progress (branch `acp-migration`, from post-ACP `upstream/main`)
Core features re-applied and compiling (`cargo check -p goose` green):
- ✅ Branding (moocp/ComplianceCow prompts), ✅ `GOOSE_DB_MAX_CONNECTIONS`.
- ✅ §9 openai thinking-disable (`GOOSE_THINKING_DISABLE_MODELS`) + anthropic
  `request_params[thinking]` override. (deepseek v4 models are already upstream.)
- ✅ §6 cache-disable — wired `ANTHROPIC_DISABLE_CACHE` into upstream's new
  `AnthropicFormatOptions.prompt_cache_disabled`. (§6 `ANTHROPIC_CACHE_TTL` deferred:
  upstream's cache toggle is boolean, no TTL slot.)
- ✅ §6 user_id — `stream_for_model` forwards `request_params[metadata]`.
- ✅ §4 DynamicHeaderClient + `allowed_headers` (client side) — re-applied; rmcp
  3.1.2's `get_stream` now takes `Option<Arc<str>>`; `sse-stream` dep re-added;
  `filter_allowed_headers` unit-tested (3 tests). The websocket_headers *injection*
  side remains a Phase 2 ACP custom method.

**Phase 1 complete** — `cargo check -p goose --all-targets` compiles, clippy clean.
(Workspace-wide `--all-targets` is blocked only by the pre-existing `v8-goose`
`rusty_v8` native-lib gap in this environment, unrelated to the migration.)
- Deferred to Phase 2 (would be dead code until their ACP method exists):
  `from_api_key` / `create_with_api_key` (§1–3 building blocks).

Fork docs (this file, the feature registry, the sync workflow) were carried onto
`acp-migration` from `sync-upstream-20260705` since the branch started from clean
upstream.

## Phase 2 progress (branch `acp-migration`) — goose side COMPLETE
Ex-REST-route features re-homed as ACP custom methods (`goose` compiles, clippy clean):
- ✅ §1–3 — `UpdateSessionProviderRequest` (`_goose/unstable/session/provider/update`):
  ephemeral per-session api_key/host via `create_with_api_key`/`from_api_key`
  (re-added) + `Agent::update_provider`; falls back to `recreate_provider_for_session`
  with no key.
- ✅ §6 user_id — `on_update_session_provider` attaches `metadata.user_id` from the
  session's `x-cow-security-context` for the anthropic provider.
- ✅ §4 injection — `SetSessionExtensionDataRequest`
  (`_goose/unstable/session/extension_data/set`): merges keys (e.g.
  `websocket_headers.v0`) into session `extension_data`.
- ✅ §10 — already upstream: ACP `new_session` → `apply_recipe` →
  `extend_system_prompt("recipe", …)`. Not re-ported.
- ✅ `acp-schema.json` / `acp-meta.json` regenerated with both methods.

**Remaining for the migration: Track B — the CowGooseService (Go) ACP client
rewrite** (REST/SSE → ACP JSON-RPC over `goose serve` WebSocket), then end-to-end
validation against `golden/rest-features-20260707`.

New ACP methods CowGooseService will call (camelCase params):
| Was (REST) | Now (ACP method) |
|---|---|
| `POST /agent/start` + `extension_data` | `session/new` (`_meta`) + `.../session/extension_data/set` |
| `POST /agent/update_provider` (api_key/host) | `.../session/provider/update` |
| `PUT /sessions/{id}/extension_data` | `.../session/extension_data/set` |
| `POST /sessions/{id}/reply` / `/events` / `/cancel` / `/fork` | ACP `session/prompt` (+notifications) / cancel / `session/fork` |

## Phases
0. **Spike (do first):** stand up upstream ACP server; learn `session/new`,
   `session/prompt` streaming, `update_provider`, and whether raw `extension_data`
   can be set. Decide ACP transport (stdio vs WS) for CowGooseService.
1. **goose core on ACP:** branch from post-removal `upstream/main`; rebase/re-apply
   the *core* features (§4 client, §6 caching, §9, DB pool, branding); get green.
2. **goose ACP-route features:** re-home §1–3, §4 injection, §6 user_id, §10 into
   `acp/server/`.
3. **CowGooseService ACP client:** rewrite the bridge against ACP.
4. **Validate against `golden/rest-features-20260707`:** every feature behaves
   identically; run the registry audit + a live cow-mcp header-forwarding test.

## Preservation gate (every phase)
- Run the identifier sweep from `.agents/workflows/sync-upstream.md` §5.
- Re-check branding prompts.
- Diff behavior against the golden tag for each feature before calling it done.

## Phase 0 spike findings (2026-07-07) — GO

Explored upstream `main` (`7c4ba2219`). Migration is viable; no blocker.

- **Transport — decided: WebSocket via `goose serve`.** Two ACP transports exist:
  `goose acp` (stdio, one process per client) and **`goose serve` (ACP over HTTP +
  WebSocket**, axum router in `crates/goose/src/acp/transport/mod.rs`, `acp-session-id`
  header, `GOOSE_SERVER__SECRET_KEY` auth, TLS + allowed-origins). `goose serve` is
  the drop-in for goose-server's network role; CowGooseService connects over WS.
- **`session/new`** (`NewSessionRequest { cwd, _meta }`): provider/model are resolved
  via `resolve_provider_and_model(_meta, recipe_settings)` — recipe `goose_provider`/
  `goose_model` or `_meta`. `extension_data` is built from enabled extensions.
- **Streaming**: `session/prompt` emits `SessionNotification` / `SessionUpdate::AgentMessageChunk`
  via `cx.send_notification`. Usage/tool-calls ride the same notification stream —
  the SSE consumer becomes an ACP-notification consumer. Parity is fine.
- **Custom methods are a first-class, easy extension point** — `#[custom_method(XxxRequest)]`
  in `acp/server/custom_dispatch.rs`. The existing surface has ~40 (session extensions,
  tools, provider *config*/secrets, prompts, config).

### The two features that need NEW custom ACP methods (no stock equivalent)
1. **§1–3 per-session ephemeral provider (api_key/host).** ACP is **config/secret-centric**
   (persistent `ProviderConfigSave` + keyring secrets); there is **no** runtime "use this
   api_key for this session" method (`update_provider` at `server.rs:2185` is an internal
   helper, not exposed). → Add a custom method e.g. `UpdateSessionProviderRequest`
   { provider, model, api_key, host, request_params }, calling our existing
   `create_with_api_key` + `agent.update_provider`.
2. **§4 raw session `extension_data` (`websocket_headers.v0`).** ACP exposes extension
   *management* (`AddSessionExtension`/`Get`/`Remove`) but **no raw `extension_data`
   setter**. → Add a custom method e.g. `SetSessionExtensionDataRequest` (merge keys),
   and/or accept `websocket_headers` via `_meta` on `session/new` for the initial set.
   (Token rotation still needs the setter method.)

Both are the same *kind* of customization the fork already applied to the REST routes,
now expressed as custom ACP methods — well-supported, low structural risk.

## Resolved risks
- Raw `extension_data` over ACP → **needs a custom method** (above); not a blocker.
- Transport → **WebSocket via `goose serve`** (decided).
- Streaming parity → **confirmed** (ACP notifications).
