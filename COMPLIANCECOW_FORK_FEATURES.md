# ComplianceCow Fork — Feature Registry

This document tracks every ComplianceCow-specific customization layered on top of
upstream `goose`, plus the companion changes in **CowGooseService** (the Go
WebSocket↔SSE bridge). Use it as the single source of truth when rebasing onto a
new upstream, onboarding, or restoring a feature.

- **goose fork (Rust):** `/Users/Arul/Documents/rust/goose/` — sync branch
  `sync-upstream-20260705` (rebased onto `block/goose` `main`)
- **CowGooseService (Go):** `/Users/Arul/Documents/projects/continube/ComplianceCow/src/cowgooseservice/`
- **cow-mcp (Python):** `http://0.0.0.0:45678/mcp`

> **Upstream is `block/goose`** (the `upstream` remote → `https://github.com/block/goose`,
> which GitHub redirects to `aaif-goose/goose` — same repo). Follow
> `.agents/workflows/sync-upstream.md` to run a sync; this registry is its
> feature checklist.

> **⚠️ 2026-07-07 — upstream removed the REST `goose-server` crate (#10224); it is
> now ACP-only.** A straight sync past that point deletes the REST interface
> CowGooseService depends on. Decision: **migrate to ACP (Option C)** — see
> `ACP_MIGRATION.md` for the plan and the feature→ACP-home mapping. Behavioral
> baseline is tagged `golden/rest-features-20260707`; do not sync `main` past
> `651dc973c` (last REST-capable) until the ACP migration lands.

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

### Branding (moocp / ComplianceCow)

- **Agent self-identity rebranded** goose → **moocp**, AAIF → **ComplianceCow**, in the
  system prompts: `crates/goose/src/prompts/system.md`,
  `subagent_system.md`, `tiny_model_system.md`. This is the only user-facing
  branding surface for a headless goose-server + custom-UI deployment (the API
  returns no goose branding of its own). **Re-check these three files after each
  upstream sync** — upstream edits them and a rebase will reintroduce "goose".
- Deliberately NOT changed: the `GOOSE_*` env prefix, `~/.config/goose` config dir,
  and the `goosed` binary name (internal only; users see just our UI). Set
  `GOOSE_DISABLE_TELEMETRY=1` in deployment. Desktop/Electron branding (distros
  guide §D) is N/A — we ship goose-server + our own UI (§E).

### §12 Subagent (summon) multi-tenancy — ported 2026-07-13

From Surendhar's commits `49eeb4d9`, `d7cf62a6`, `7252e54e` (written on the
pre-ACP base `0764f508c`, forward-ported onto post-ACP upstream).

- **Subagent inherits tenant context.** `create_subagent_session`
  (`agents/platform_extensions/summon.rs`) copies the parent's
  `cow_tenant.v0` + `websocket_headers.v0` (`INHERITED_SUBAGENT_EXTENSION_STATES`)
  onto the subagent session, so the subagent's own MCP calls forward allow-listed
  headers to cow-mcp. **Without this, §4 is broken for subagents.**
- **Subagent uses the tenant's API key.** `resolve_provider` reads `x-api-key`
  (+ `x-openai-host`/`x-anthropic-host`/`x-host`) from the parent session via
  `session_provider_credentials()` and builds the provider with
  `create_with_api_key` (§1-3). Errors propagate deliberately — running a subagent
  on another tenant's global key is worse than failing.
- **Subagent session resume.** `DelegateParams.subagent_session_id`, env-gated by
  **`GOOSE_SUBAGENT_RESUME`** (default off); when on, the delegate tool exposes the
  param and the result text carries `[Subagent Session ID: <id>]`.
- **Session naming:** skip generation when a session has no user messages.
- **`GOOSE_LOG_CONSOLE`** enables CLI console logging (`goose-cli/src/logging.rs`).

**Deliberately NOT ported** from those commits:
- `reply_parts.rs` `tracing::info!("Sending LLM Payload: …")` — logs the full system
  prompt and every message at info level. Unacceptable for a multi-tenant GRC
  deployment (writes tenant conversation content to logs) and contrary to the
  repo's logging guidance. Use `GOOSE_LOG_CONSOLE` + debug-level tracing instead.
- Removal of the recipe-title session-naming branch — a product decision, not a bug
  fix; upstream asserts that behavior in
  `test_maybe_update_name_uses_recipe_title_for_recipe_session`. Raise it if
  ComplianceCow wants LLM-generated names for recipe sessions.

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

## Verifying the fork features

```bash
cargo test -p goose --test compliancecow_features_test   # 6 tests
cargo test -p goose --lib summon                         # incl. 4 §12 tests
```
Both run **offline** — no API keys, no ports, no network — and use
`GOOSE_PATH_ROOT` + tempdirs, so they never touch `~/.config/goose` or a real
session database. Run them after every upstream sync; they are the executable
half of the preservation gate in `.agents/workflows/sync-upstream.md`.

| Feature | Covered by |
|---|---|
| §4 injection (`extension_data/set`, incl. merge on token rotation) | `set_session_extension_data_persists_and_merges_websocket_headers` |
| §4 forwarding (allow-listed headers reach the MCP server; others never do) | `allow_listed_session_headers_are_forwarded_to_the_mcp_server` |
| §1-3 tenant api_key/host, §6 `metadata.user_id`, §9 `thinking` params | `update_session_provider_applies_tenant_key_request_params_and_user_id` |
| §6 `ANTHROPIC_DISABLE_CACHE` | `anthropic_disable_cache_env_strips_cache_control` |
| §9 deepseek-v4 thinking default | `deepseek_v4_defaults_to_thinking_disabled_on_openai_format` |
| Branding (moocp/ComplianceCow prompts) | `agent_identity_is_rebranded_to_moocp` |
| §12 subagent tenant key + parent tenant-state inheritance | 4 tests in `summon.rs` |

Every one was **mutation-verified**: breaking the feature it covers makes it fail,
so a green run is meaningful rather than vacuous.

**Not covered** (needs live infra): the real cow-mcp server, a real LLM provider,
and CowGooseService driving goose over ACP end-to-end.

## Running locally (post-ACP: there is no more `goosed`)

```bash
source bin/activate-hermit
export SSL_CERT_FILE=$(python3 -m certifi)   # REQUIRED, see gotcha below
cargo build -p goose-cli                      # produces target/debug/goose

# ACP server (replaces the deleted goose-server REST API)
GOOSE_SERVER__SECRET_KEY=test ./target/debug/goose serve --host 127.0.0.1 --port 3284
# or: just run-server   (same thing on port 3000)

# interactive CLI instead of the server
./target/debug/goose session
```

Verify it is up:
```bash
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:3284/health          # 200
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:3284/acp             # 401 (auth enforced)
curl -s -o /dev/null -w '%{http_code}\n' -H 'x-secret-key: test' .../acp        # 406 (auth OK; needs WS upgrade)
```
ACP is JSON-RPC 2.0 over the **WebSocket at `/acp`**, authenticated with
**`x-secret-key`** — exactly what CowGooseService's `ACPConn` speaks.

### ⚠️ Build gotcha: `could not find native static library rusty_v8`
`goose` depends on v8 (via `pctx_code_mode` → `deno_core`). Its build script
downloads a 32 MB prebuilt `librusty_v8` from GitHub using **Python**, which on
macOS fails with `CERTIFICATE_VERIFY_FAILED` (no CA certs) — so the build fails
and, worse, **`goose-cli` silently never compiles**, which hides real errors from
`cargo check --all-targets`. Fix:
```bash
export SSL_CERT_FILE=$(python3 -m certifi)   # or run "/Applications/Python 3.12/Install Certificates.command"
cargo clean -p v8-goose -p v8 && cargo build -p goose-cli
```
(the `cargo clean` is needed once — `SSL_CERT_FILE` is not in the build script's
`rerun-if-env-changed`, so a cached failure would otherwise persist).
**Always build `-p goose-cli` before trusting a green check.**

## Build / verify
- **goose:** `source bin/activate-hermit && cargo build && cargo clippy --all-targets -- -D warnings && cargo fmt`
- After goose server-route changes: `just generate-openapi` (the sync added
  `api_key`/`host` to `UpdateProviderRequest`).
- **CowGooseService:** `/usr/local/go/bin/go build ./... && /usr/local/go/bin/go vet ./...`
