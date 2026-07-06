# ComplianceCow Fork — Feature Registry

This document tracks every ComplianceCow-specific customization layered on top of
upstream `goose`, plus the companion changes in **CowGooseService** (the Go
WebSocket↔SSE bridge). Use it as the single source of truth when rebasing onto a
new upstream, onboarding, or restoring a feature.

- **goose fork (Rust):** `/Users/Arul/Documents/rust/goose/` — sync branch
  `sync-upstream-20260705` (rebased onto `block/goose` `main`)
- **CowGooseService (Go):** `/Users/Arul/Documents/projects/continube/ComplianceCow/src/cowgooseservice/`
- **cow-mcp (Python):** `http://0.0.0.0:45678/mcp`

> **Upstream is `block/goose`** (the `upstream` remote → `https://github.com/block/goose`).
> The `.agents/workflows/sync-upstream.md` doc historically referenced
> `aaif-goose/goose`; the live remote is `block/goose`.

## 2026-07-05 sync — major upstream refactor

Upstream **relocated the entire provider layer into new crates**, and has since
**independently absorbed (or re-implemented, often better) most of the fork's
features**. This changed the sync from a mechanical rebase into a
feature-by-feature reconciliation. New crate layout:

| Old (pre-refactor) | New (`block/goose`) |
|---|---|
| `crates/goose/src/providers/anthropic.rs` | `crates/goose-providers/src/anthropic.rs` |
| `crates/goose/src/providers/openai.rs` | `crates/goose-providers/src/openai.rs` |
| `crates/goose/src/providers/formats/anthropic.rs` | `crates/goose-provider-types/src/formats/anthropic.rs` |
| `crates/goose/src/providers/formats/openai.rs` | `crates/goose-provider-types/src/formats/openai.rs` |
| `crates/goose/src/providers/declarative/deepseek.json` | `crates/goose-providers/src/declarative/definitions/deepseek.json` |
| `ModelConfig` | `crates/goose-provider-types/src/model.rs` |
| built-in provider construction | `crates/goose/src/providers/{openai_def,anthropic_def}.rs` (via builders) |
| `crates/goose/src/providers/init.rs` | *same path* |
| `crates/goose/src/agents/*.rs`, `crates/goose-server/src/routes/*.rs` | *same paths* |

---

## Part 1 — goose fork (Rust) feature status after the sync

### ✅ Now provided by upstream — NOT ported (would duplicate/regress)

- **§5 Custom Anthropic provider-config header.** `from_declarative_config`
  applies `config.headers` to the `ApiClient` via `with_headers`
  (`goose-providers/src/anthropic.rs`). Declarative custom providers get their
  headers for free.
- **§7 `available_tools` filtering.** Upstream carries `available_tools` on the
  extension config variants and filters via `ExtensionManager::filter_tools`
  ("empty = all tools available" — identical semantics to the fork).
- **§8 Reasoning-content accumulation (upstream issue #9675).** The agent reply
  loop reuses thinking captured in an earlier stream chunk for tool-call-only
  chunks (`surfaced_thinking_in_turn` / `response_thinking` in
  `crates/goose/src/agents/agent.rs`).
- **§11 Anthropic safety-refusal surfacing.** Upstream implements this **better**
  than the fork did: on `stop_reason: "refusal"` it raises a typed
  `ProviderError::Refusal { details, category }` and flushes usage
  (`goose-provider-types/src/formats/anthropic.rs`). The fork's plain-text
  message version was intentionally dropped.
- **request_params plumbing.** `ModelConfig.request_params` +
  `ModelConfig::with_merged_request_params` exist upstream, and the
  `update_agent_provider` route already merges `request_params`. `custom_deepseek`
  is already in `PROVIDERS_NEEDING_MAX_TOKENS_REMAP`.

### ✅ Ported onto the new structure this sync

- **§1–3 Dynamic per-session provider from an explicit API key + host.**
  - `from_api_key(api_key, host_override)` added to
    `crates/goose/src/providers/openai_def.rs` and `anthropic_def.rs` (built via
    the new provider builders; `preserve_thinking_context` derived from whether
    the resolved host is OpenAI's own API).
  - `create_with_api_key(name, api_key, host)` in
    `crates/goose/src/providers/init.rs`, exported via `providers/mod.rs`.
  - `UpdateProviderRequest` gained `api_key` + `host`; `update_agent_provider`
    (`crates/goose-server/src/routes/agent.rs`) branches to `create_with_api_key`
    when `api_key` is present, else the normal `create`.
  - **Dropped as obsolete:** the fork's `Agent::ensure_session_provider` /
    `session_providers` map + auto-reading `websocket_headers` for provider
    selection. Upstream now runs **one Agent per session**
    (`agent_manager.get_or_create_agent`), so per-session isolation comes from
    per-session Agents + `update_provider` — a shared-agent provider map is no
    longer needed and would fight that model.
- **§4 Dynamic header injection into MCP tool calls.**
  - `allowed_headers: Vec<String>` field added to the `StreamableHttp` extension
    config variant (`crates/goose/src/agents/extension.rs`) + accessor
    `allowed_headers()`, threaded through `resolve()` and the recipe adapters.
  - `DynamicHeaderClient` (in `crates/goose/src/agents/extension_manager.rs`)
    wraps the reqwest client, implements `rmcp`'s `StreamableHttpClient`, and on
    every request injects the session's `websocket_headers.v0` entries whose
    names appear in `allowed_headers` (case-insensitive). Header **values** are
    read fresh per request (tokens rotate).
  - Wired in `create_streamable_http_client` (gained `allowed_headers` +
    `session_id` params); each MCP client serves exactly one session (enforced by
    `GooseClient::set_session_id`'s assert), so headers cannot leak across tenants.
  - Added `sse-stream` (0.2) as a direct dep of `goose` (needed to name
    `sse_stream::Sse` in the trait impl).
  - **Injection side (the source of `websocket_headers.v0`)** — required for §4 to
    have anything to forward; ported alongside it:
    - `StartAgentRequest.extension_data: Option<ExtensionData>` +
      merge in `start_agent` (`crates/goose-server/src/routes/agent.rs`) — lands
      the initial headers before background extension loading.
    - `PUT /sessions/{session_id}/extension_data` (`update_extension_data` in
      `crates/goose-server/src/routes/session.rs`) — CowGooseService's follow-up /
      token-rotation path; merges keys into the session's `extension_data`.
    - Read back via `ExtensionData::get_extension_state("websocket_headers","v0")`,
      keyed `"websocket_headers.v0"`. `SessionManager::instance()` shares the
      static `SESSION_STORAGE`, so the DynamicHeaderClient reads exactly what these
      routes write.
    - **This was missed in the first sync pass** (dropped with §1-3 Layer B),
      which is why headers didn't forward on first test; restored 2026-07-05.
  - **⚠️ Still needs a full end-to-end integration check** against CowGooseService,
    but the injection→read→filter→forward path is now complete and compiles.
- **§9 DeepSeek thinking-disable + v4 models.**
  - `crates/goose-provider-types/src/formats/openai.rs`:
    `thinking_disable_model_patterns()` (env `GOOSE_THINKING_DISABLE_MODELS`,
    default `deepseek-v4`) defaults `thinking` to disabled for matching models on
    the openai-compatible endpoint, unless `request_params` already set it.
  - `crates/goose-provider-types/src/formats/anthropic.rs`: an explicit
    `request_params["thinking"]` directive now takes precedence in
    `apply_thinking_config` (for the Anthropic-compatible DeepSeek endpoint).
  - `deepseek.json` gained `deepseek-v4-flash` / `deepseek-v4-pro`.
  - The old declarative `default_request_params` config field was **not**
    re-introduced — the env-based `thinking_disable_model_patterns` supersedes it.
- **§6 (partial) Anthropic `metadata.user_id`.** Attributes Anthropic requests to
  the ComplianceCow user. Because the provider now lives in `goose-providers`
  (no `SessionManager` access) and `stream` has no `session_id`, this is split:
  - `crates/goose-providers/src/anthropic.rs` — `stream` copies
    `model_config.request_params["metadata"]` into the request body.
  - `crates/goose-server/src/routes/agent.rs` — `update_agent_provider`, for the
    `anthropic` provider, reads the session's `websocket_headers.v0`
    → `x-cow-security-context` → JSON `ID`, and merges
    `{"metadata": {"user_id": ID}}` into `request_params` (persisted in the
    session's model_config). Helper: `anthropic_user_metadata`.
  - **Limitation vs the old fork:** the old code read the session fresh on every
    `stream`; this captures `user_id` when `update_provider` is called (once per
    session — which CowGooseService always does for `anthropic`). `user_id` is
    stable per session, so this is equivalent in practice, but a session that
    never calls `update_provider` won't get the metadata.
  - **Was missed in the first sync pass** (dropped with the rest of §6);
    restored 2026-07-06.
- **§6 (partial) Configurable Anthropic prompt caching.** Upstream inserts
  `cache_control: {type: ephemeral}` on the system prompt, the last tool spec, and
  message breakpoints **unconditionally**. DeepSeek's Anthropic-compatible endpoint
  can reject `cache_control`, so the fork gated it by env:
  - `crates/goose-provider-types/src/formats/anthropic.rs` — `cache_control_disabled()`
    (`ANTHROPIC_DISABLE_CACHE`) skips all three insertions; `ephemeral_cache_control()`
    honors `ANTHROPIC_CACHE_TTL`.
  - **Was missed in the first sync pass** (part of §6); restored 2026-07-06.
  - No env-mutating unit test added — it would race the parallel `cache_control`
    presence tests; verified by compile + the existing suite (caching on by default).
- **§10 Recipe instruction injection.** `start_agent`
  (`crates/goose-server/src/routes/agent.rs`) now applies the recipe to the agent
  (`apply_recipe_to_agent` → `extend_system_prompt("recipe", …)`), matching the
  idiom upstream already uses in `resume_agent`/`update_from_session`/`restart`.
  (Upstream applied recipes on those paths but not on `start_agent`.)

### Operational tweaks (not in §1-11, found via identifier audit)

- **Configurable SQLite pool size.** `crates/goose/src/session/session_manager.rs` —
  `GOOSE_DB_MAX_CONNECTIONS` (default 50) on the session-store pool; upstream uses
  sqlx's small default, which can exhaust under concurrent sessions.

### Conscious architectural divergences (fork code deliberately NOT copied)

- **`call_tool` `allowed_headers` threading.** The fork added an `allowed_headers`
  parameter to `McpClientTrait::call_tool` and plumbed it through every impl
  (`mcp_client.rs`, all `platform_extensions/*`, `acp/*`, `skills/client.rs`).
  The sync instead attaches `allowed_headers` to the `DynamicHeaderClient` at
  transport-creation time, so the signature change is unnecessary. The real
  cow-mcp extension is a `StreamableHttp` extension routed through
  `create_streamable_http_client` (where the client lives); platform/skills
  clients never call cow-mcp, so they need no header forwarding.
- **`ModelConfig.default_request_params` (declarative config field).** Replaced by
  the env-based `GOOSE_THINKING_DISABLE_MODELS` mechanism (§9).

### ❌ Intentionally not ported

- **§6 (partial) — caching config + `create_session_with_id`.** Prompt caching
  itself is upstream (`supports_cache_control` / `cache_control`). The fork's
  `create_session_with_id` had **no caller** (dead code), and the per-request
  `session_id` request tracking would require invasive changes to upstream's
  shared `ApiClient` for telemetry only. Revisit only if a concrete need appears.
  **Caveat:** the §6 commit (`1d2dec92f`) also bundled the Anthropic
  `metadata.user_id` injection — see below; that part **was** needed and is ported.

---

## Part 2 — CowGooseService (Go) features

Bridge architecture details: see `ARCHITECTURE.md` and `CLAUDE.md` in the
CowGooseService repo. Unchanged by the goose sync — the Go side still drives
per-session provider/model/key and `request_params` via the `update_provider`
route (which the fork extended with `api_key`/`host` again).

### 1. WebSocket ↔ SSE bridge (core)
Browser WS ⇄ goose HTTP+SSE; multiplexes many goose sessions over one WS.
- `bridge/connection_manager.go`, `bridge/session_worker.go`, `bridge/sse_ws_bridge.go`.

### 2. Header forwarding
Captures whitelisted WS-handshake headers and stores them in the goose session as
`websocket_headers.v0` (consumed by goose §4 above).
- `bridge/sse_ws_bridge.go` — `HeaderWhitelist`, `ExtractWhitelistedHeaders`.

### 3. Per-session provider resolution
Resolves provider/model/API-key per session; handshake headers first, then
vault/prompty fallback. Sends them in the `update_provider` request body.
- `main.go` — `resolveProvider`; `utils/llm_tools_vault.go`.

### 4. DeepSeek thinking control (request_params injection)
`utils/deepseek_thinking.go` — `BuildDeepSeekThinkingParams(model)` sets
`{"thinking": {"type": "disabled"}}` (env `DEEPSEEK_THINKING_MODE`, default
`disabled`). Threaded through `UpdateProviderRequest.RequestParams`.
Note: goose now *also* defaults DeepSeek-v4 thinking off via
`GOOSE_THINKING_DISABLE_MODELS` (§9), so the two are complementary defenses.

---

## Relevant config / env reference
| Where | Key | Purpose |
|---|---|---|
| goose `config.yaml` | `OPENAI_HOST=https://api.deepseek.com` | Routes `openai` provider to DeepSeek |
| goose `config.yaml` | `ANTHROPIC_HOST=https://api.deepseek.com/anthropic` | DeepSeek's Anthropic-compatible endpoint |
| goose `config.yaml` | `GOOSE_MODEL=deepseek-v4-pro[1m]` | Default model |
| goose env | `GOOSE_THINKING_DISABLE_MODELS` | Comma-sep model substrings to default `thinking:disabled` (default `deepseek-v4`) |
| goose env | `ANTHROPIC_DISABLE_CACHE` | Set to skip `cache_control` on Anthropic requests (needed for DeepSeek's Anthropic endpoint) |
| goose env | `ANTHROPIC_CACHE_TTL` | Optional TTL applied to Anthropic `cache_control` blocks |
| goose env | `GOOSE_DB_MAX_CONNECTIONS` | Session-store SQLite pool size (default 50; raise for high session concurrency) |
| goose extension YAML | `allowed_headers` | Session headers forwarded to the MCP server (§4) |
| CowGooseService env | `DEEPSEEK_THINKING_MODE` | `disabled` (default) / `enabled` / `passthrough` |
| CowGooseService env | `DEEPSEEK_REASONING_EFFORT` | optional effort when enabled |

## Build / verify
- **goose:** `source bin/activate-hermit && cargo build && cargo clippy --all-targets -- -D warnings && cargo fmt`
- After goose server-route changes: `just generate-openapi` (the sync added
  `api_key`/`host` to `UpdateProviderRequest`).
- **CowGooseService:** `/usr/local/go/bin/go build ./... && /usr/local/go/bin/go vet ./...`
