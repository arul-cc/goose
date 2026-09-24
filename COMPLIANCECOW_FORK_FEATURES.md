# ComplianceCow Fork — Feature Registry

This document tracks every ComplianceCow-specific customization layered on top of
upstream `goose`, plus the companion changes in **CowGooseService** (the Go
WebSocket↔SSE bridge). Use it as the single source of truth when rebasing onto a
new upstream, onboarding, or restoring a feature.

- **goose fork (Rust):** `/Users/Arul/Documents/rust/goose/` — branch
  `acp-migration`, rebased onto `block/goose` `main` **2026-09-24**
  (upstream `80c119758`, goose 1.52.0)
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

## Sync log

### 2026-09-24 — 24 upstream commits (1.51.0 → 1.52.0, includes Jev)

Rebased onto `upstream/main` at `80c119758`. **45 fork commits replayed, zero
conflicts** — despite nine files touched by both sides, against one last sync:
`Cargo.lock`, `Justfile`, `goose-cli/src/session/mod.rs`, both provider formats
(`anthropic.rs`, `openai.rs`), `goose/Cargo.toml`, `acp/server.rs`,
`extension_manager/mod.rs` and `extension_manager/streamable_http.rs`.

**Target was `main`, not the `v1.52.0` tag.** The tag is not an ancestor of
`main`: it sits on a release branch whose only extra commits are the
branch-open chore (`aa5b2fcc8`) and a cherry-pick (`302b60806`) of a change
already on `main` as `9fd1051bb`. `main` carries all of the release, is already
versioned 1.52.0, and has seven later fixes. Basing on the tag would have left
the next sync to unpick those duplicates.

With nine shared files the tree-diff check from the last entry is weaker — it
cannot see inside a file both sides edited. The check that carries weight here
is range-diff, which compares every fork patch before and after:

```bash
git range-diff <old-base>..backup/pre-sync-<date> upstream/main..HEAD
```

All 45 came back `=`: every hunk applied with identical content.

The stale-index halt from the last sync recurred, on the §13 commit
(`session_manager.rs`). Same fix — `git update-index --refresh`, then
`git rebase --continue`. Treat it as expected on this machine, not a surprise.

**Jev (TypeSafe) arrived, but nothing uses it yet.** `8a460f964` adds a
decisions provider — `goose-providers/src/decision.rs`, `typesafe.rs` (host
`api.typesafe.ai`, model `jev-latest`) and an OpenRouter equivalent — exported
through the GDK bindings. No part of the goose agent calls it: no routing, no
tool gating, and no tenant data sent to TypeSafe by default. Using it means
writing the integration ourselves.

**The lib test command changed.** `e678c3b64` (lean ACP-only binary) made
`goose`'s default features empty, so a bare `cargo test -p goose --lib` no
longer compiles — upstream's own test code references `Scheduler`, now behind
the `scheduler` feature. Upstream CI passes an explicit list; the verification
commands below now do too. `goose-cli`'s defaults still enable every feature, so
normal and deployed builds are unaffected. The new `goose-acp` binary
(`just build-lean`) is optional.

**The MCP redirect guard is safe for our setup.** `6a2c3dadf` sets
`redirect::Policy::none()` on the streamable-HTTP client — redirects are
surfaced, never followed. Probed against the live cow-mcp: `/mcp` returns 200,
`/mcp/` returns 307. All ten recipes use `/mcp`, so nothing breaks. **A recipe
URI ending in `/mcp/` will now fail to connect** instead of silently following
the redirect. The guard also means §4's forwarded tenant headers can no longer
reach a redirect target.

**Latent §9 gap.** `78ab4b12b` renames `deepseek-v4-flash` to `deepseek-flash`
in the declarative catalogue, which the §9 default pattern `deepseek-v4` does not
match. The platform references only `deepseek-v4-pro`, which still matches, so
nothing is affected today.

Verification after: 42/42 fork markers, 11/11 fork feature tests, clippy and
`cargo fmt --check` clean. `cargo test -p goose --lib` with CI's feature set:
2325 passed / 4 failed. With those features the four old gcpauth/JWT failures
pass, as does `test_all_platform_extensions`. The four remaining:
`state_machine::provider_lifecycle` (asserts upstream's branding wording) and
three `plugins::tests` that fail with `Bad CPU type in executable`.

That last one is environmental and outlives the tests. `/usr/local/bin/git` is
an Intel-only Homebrew git 2.33.0, and macOS 27 on this machine does not run
Intel binaries. zsh falls through to `/usr/bin/git` when exec fails, so git
works from the shell; Rust's `Command::new("git")` takes the first `PATH` match
and stops. Any goose feature that shells out to git fails the same way.

Follow-ups this sync raised, none actioned:
- Add `deepseek-flash` to the §9 default pattern.
- Remove the Intel git at `/usr/local/bin/git`.
- Rebuild the running goose to pick up these fixes. Jev stays inert either way.

### 2026-09-21 — 8 upstream commits (1.51.0, no version bump)

Four days of upstream, rebased onto `e629eea1d`. **44 fork commits replayed,
zero conflicts.**

Exactly one file was touched by both sides: `crates/goose-providers/src/anthropic.rs`.
Upstream's `8fd84be23` deleted the hardcoded `ANTHROPIC_KNOWN_MODELS` list in
favour of `known_models_from_registry()`; §6's `metadata` forwarding lives inside
`create_request`, a different function. Git merged them without asking.

The check worth repeating on every sync: after the rebase, the tree differed from
the pre-rebase tree in **exactly** the 23 files upstream touched, and nothing
else. That is stronger than a green test run — it proves no fork file was
altered, dropped, or reformatted in passing.

```bash
diff <(git diff --name-only <old-base> upstream/main | sort) \
     <(git diff --name-only backup/pre-sync-<date> HEAD | sort)
```

**Trap:** the rebase halted partway with `error: Your local changes to the
following files would be overwritten by merge`, naming `acp/server.rs` and
`providers/openai_def.rs` — while `git status` reported a clean tree and
`git diff` was empty. Stale index stat data, not real changes.
`git update-index --refresh` followed by `git rebase --continue` cleared it and
the rebase ran to completion. Do not resolve this one by stashing or resetting;
there is nothing to stash.

Verification after: 42/42 fork markers, 11/11 fork feature tests, clippy and
`cargo fmt --check` clean. `cargo test -p goose --lib` at 2303 passed / 6 failed
— the identical six as before the rebase (4 rustls `CryptoProvider` in the
gcpauth/JWT tests, `state_machine::provider_lifecycle` and
`test_all_platform_extensions`, both asserting upstream's branding wording).

Follow-ups this sync raised, neither actioned:
- `1e83e89f5` gave upstream's OpenAI provider a custom base URL. §1-3's
  `from_api_key(api_key, host_override)` may now be partly redundant there —
  worth checking whether fork code can be deleted rather than carried.
- `2090ad1c6` de-duplicates `@radix-ui` and moves `ui/pnpm-lock.yaml`; run
  `pnpm install` before building the desktop app.

### 2026-09-17 — 157 upstream commits (1.48.0 → 1.51.0) — reconstructed

**This sync was never logged; the entry below is inferred from git, not from a
record of the work.** It surfaced on 2026-09-21 because the header block still
named `caf59517c` / 1.48.0 while `Cargo.toml` read 1.51.0. `caf59517c` was
correct when written — the gap is this sync, which moved the merge base to
`d213a3b13` (2026-09-17, "feat: add Live voice conversations to the desktop
app", #12093) without updating the registry.

What can be established: `backup/pre-sync-20260917` still points at the
pre-rebase tip (`78a39736a`), and its merge base with upstream is `caf59517c`,
which brackets the sync. Its conflicts, resolutions and verification results are
not recoverable.

The lesson is the one this file exists to enforce: **a sync that isn't logged
here leaves the next sync guessing at its own starting point.** Write the entry
in the same commit that moves the branch.

### 2026-08-27 — 175 upstream commits (1.46.0 → 1.48.0)

Nine days of upstream, rebased onto `caf59517c`. **31 fork commits replayed, one
conflict.**

The conflict was `platform_extensions/summon.rs` (§12): upstream reworked
provider construction to fetch the registry entry first and pass a
`provider_default_model` into a now-5-arg `resolve_model_config`, while our
commit adds the tenant-key early return. Both survive — the early return moved
below the new `model_config` and above upstream's `match provider_entry`.

One follow-up edit was needed: upstream's new
`test_codex_rejects_socket_backed_streamable_http` builds
`ExtensionConfig::StreamableHttp` as a struct literal, so §4's `allowed_headers`
field broke the *test* target while the lib itself compiled — `cargo build` did
not catch it, `cargo test` did. Any sync adding a construction site will need the
same one-line edit.

What made this cheap: upstream touched 7 fork-critical files but **not one
fork-owned function**. Zero commits mention `DynamicHeaderClient`,
`filter_allowed_headers`, `allowed_headers`, `create_streamable_http_client`,
`maybe_update_name`, or the recipe-title logic §13 removed. The overlaps were
same-file, different-region.

Verification after: 26/26 fork markers, 9/9 fork feature tests, Go 7/7 including
the live ACP round-trip, and full-stack 16 passed / 0 failed (§1-3, §4 both
sides, §6, §9, §12, §13, recipe selection, 37,829 bytes of authorized tenant
data).

`cargo test -p goose --lib` sits at the same pre-sync baseline — 4 crypto/JWT
(environmental), 2 `state_machine`, and `test_all_platform_extensions` which
needs `--features code-mode`. One extra, `tool_dispatch_records_gen_ai_span_attributes`,
fails only under parallelism and passes single-threaded and in isolation: an
upstream test-isolation issue in `agent.rs`, a file the fork does not touch.

**Unexplained, watch for it:** the first live ACP round-trip after the bump
returned `-32603 Internal error` from `session/fork`. Eleven subsequent runs
passed, including immediately after a restart, and the server log from that run
was overwritten before it could be read. Not reproduced, not explained.

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
  - **Verified end-to-end through CowGooseService on 2026-08-26** — see the
    full-stack note below.
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

### §14 Session credentials held out of the database — added 2026-09-20

`websocket_headers.v0` carries three tenant credentials: the LLM provider key
(`x-api-key`), the platform auth token (`authorization`), and
`x-cow-security-context`, which embeds another token. Those were serialized into
`sessions.extension_data` — a plain `TEXT` column — so a stolen `sessions.db`, a
PVC snapshot or a backup yielded live credentials. They also travelled with
session export, session copy, and the ACP session-info response.

- **Process-local store.** `session/session_secrets.rs` keys credentials by
  session id and never serializes them. `split_secret_headers` pulls them out in
  `on_set_session_extension_data` before the rest is persisted.
- **Reads merge.** `merged_headers(session_id, extension_data)` layers the
  in-memory credentials over the persisted headers. The three consumers —
  `DynamicHeaderClient` (§4), `session_provider_credentials` (§12) and
  `anthropic_user_metadata` (§6) — go through it, so their behaviour is unchanged.
- **Subagents.** `session_secrets::copy` runs in `create_subagent_session`:
  `INHERITED_SUBAGENT_EXTENSION_STATES` copies header *names*, so without this the
  subagent inherits an empty key and silently falls back to global credentials.
- **Export redacts.** `session_secrets::redact` strips credentials from rows
  written before this change, which export otherwise hands out verbatim.
- **Cleanup.** `delete_session` drops the session's entry.

**The key already came from the vault** — `resolveProvider` on the Go side has
always fetched it. The defect was that goose then wrote it to disk. The host
re-pushes headers on every (re)connect (`connection_manager.go` — on resume, and
on re-create after a goose restart), so the store repopulates from the vault
rather than from disk.

**Migration.** Rows written before this keep their credentials; `merged_headers`
falls back to the persisted map so those sessions still work. Scrubbing them is
not done — the fallback must stay until it is.

⚠️ **The store is keyed by session id, which `create_session` allocates as
`YYYYMMDD_N` counting up within one database.** That is unique for a server, which
opens one database, but *not* across two `SessionManager`s. Anything that builds a
second one — tests, tooling — must `session_secrets::remove` what it stores, or it
hands those credentials to the next session that lands on the same id.

⚠️ **Provider restore still ignores the session key.** It reads `GOOGLE_API_KEY`
etc. from global config and never consulted the tenant key — before this change or
after. Pre-existing, but it matters more now that nothing repopulates from disk.
Open decision: fail loudly, or use the session's key.

### §15 Re-applying a recipe to an existing session — added 2026-09-20

goose applies a recipe only at `session/new`, via `_meta.recipeId`. A chat created
before the platform wired recipes in carries none of the recipe's extensions, so
resuming one leaves the model with no ComplianceCow tools. goose logs
*"No extensions found in session … This is unexpected."* and the chat reads as
broken for reasons the user cannot see.

- **`_goose/unstable/session/recipe/apply`** (`ApplySessionRecipeRequest`,
  `on_apply_session_recipe`) applies a recipe to an existing session, reusing
  `build_enabled_extensions_data`.
- **`load_recipe_by_id`** shares `resolve_recipe_path_by_id` with `session/new`, so
  a recipe id resolves to the same file whether applied at creation or later.
- **CowGooseService** calls it on every resume
  (`bridge/acp_wiring.go` — `applyRecipeToResumedSession`).

**Why not translate extensions on the Go side.** An extension has three shapes: the
recipe DTO (`type: streamable_http`, `uri`), goose's internal `ExtensionConfig`
(`StreamableHttp`), and the ACP wire type `GooseExtension` (`Mcp { server: McpServer }`).
`session/extensions/add` takes the third and rejects the first. `McpServer` is an
**untagged** enum owned by the external `agent-client-protocol` crate, so building
it outside goose means tracking an upstream SDK's wire format. Keeping the
conversion in goose is the point of this method.

**It replaces, it does not merge.** The session ends up with the extension set
`session/new` would have built — configured builtins plus the recipe's. An
extension added by any other route is dropped. That is correct only where the
recipe owns the session's extensions, which is the platform's case. Applying twice
changes nothing, which is what makes it safe on every resume.

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
`websocket_headers.v0` (consumed by goose §4 above). The credentials among them are
no longer persisted goose-side (§14); they are re-pushed on every (re)connect.
- `bridge/sse_ws_bridge.go` — `HeaderWhitelist`, `ExtractWhitelistedHeaders`.

### 3. Per-session provider resolution
Resolves provider/model/API-key per session; handshake headers first, then
vault/prompty fallback. Sends them in the `update_provider` request body.
- `main.go` — `resolveProvider`; `utils/llm_tools_vault.go`.

### 4. Session attribution (allow-listed, §14)
`cow_tenant.v0` records `email`, `username`, `org_id`, `group_id` and `role_name`
alongside the ids it already held, so a session still says who ran it now that the
security context is not persisted.
- `bridge/sse_ws_bridge.go` — `attributionFields`, `ExtractAttribution`,
  `BuildTenantState` (shared by the bridge and `headless/runner.go`).

These five are an **allow-list, deliberately not a deny-list.** The header is a
serialized `SecurityContext`, which embeds the whole `UserVO`: it carries
`AuthToken` and a populated `hash` today, and `Token`, `AzureToken` and `OTP` are
declared on the same struct. Naming what to drop means every field added upstream
lands in the session database unnoticed — which is how the credentials got there.

⚠️ `email` and `username` are PII, and `extension_data` travels with session export
and copy.

### 5. Recipe re-apply on resume (§15)
`bridge/acp_wiring.go` — `applyRecipeToResumedSession`. Fails soft: a missing or
unreadable recipe costs the tools, never the resume.

### 6. DeepSeek thinking control (request_params injection)
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
| goose env | `GOOSE_RECIPE_PATH` | Recipe search path; `session/recipe/apply` (§15) resolves ids against it |
| CowGooseService env | `DEEPSEEK_THINKING_MODE` | `disabled` (default) / `enabled` / `passthrough` |
| CowGooseService env | `DEEPSEEK_REASONING_EFFORT` | optional effort when enabled |

## Verifying the fork features

```bash
cargo test -p goose --test compliancecow_features_test   # 11 tests
# goose's default features are empty since 1.52.0, so the lib test target needs
# upstream CI's feature set or it does not compile:
F=rustls-tls,code-mode,tree-sitter,live-voice,scheduler,platform-apps,chat-recall,acp-http
cargo test -p goose --lib --no-default-features --features $F session_secrets   # 6 §14 tests
cargo test -p goose --lib --no-default-features --features $F summon            # incl. 4 §12 tests
```
Both run **offline** — no API keys, no ports, no network — and use
`GOOSE_PATH_ROOT` + tempdirs, so they never touch `~/.config/goose` or a real
session database. Run them after every upstream sync; they are the executable
half of the preservation gate in `.agents/workflows/sync-upstream.md`.

| Feature | Covered by |
|---|---|
| §4 injection (`extension_data/set`, incl. merge on token rotation) | `set_session_extension_data_persists_and_merges_websocket_headers` |
| §4 forwarding (allow-listed headers reach the MCP server; others never do) | `allow_listed_session_headers_are_forwarded_to_the_mcp_server` |
| §4 forwarding for an extension registered **over ACP** (not config.yaml) | `extension_registered_over_acp_forwards_allow_listed_headers` |
| §1-3 tenant api_key/host, §6 `metadata.user_id`, §9 `thinking` params | `update_session_provider_applies_tenant_key_request_params_and_user_id` |
| §6 `ANTHROPIC_DISABLE_CACHE` | `anthropic_disable_cache_env_strips_cache_control` |
| §9 deepseek-v4 thinking default | `deepseek_v4_defaults_to_thinking_disabled_on_openai_format` |
| Branding (moocp/ComplianceCow prompts) | `agent_identity_is_rebranded_to_moocp` |
| §12 subagent tenant key + parent tenant-state inheritance | 4 tests in `summon.rs` |

Every one was **mutation-verified**: breaking the feature it covers makes it fail,
so a green run is meaningful rather than vacuous.

> **Follow-up:** adding `allowed_headers` to the ACP wire type changed
> `crates/goose/acp-schema.json` (regenerated). The generated TypeScript SDK in
> `ui/sdk/src/generated/` was **not** regenerated — `just generate-acp-types`
> needs `npm install` in `ui/sdk` first. Harmless for us (our UI does not use
> goose's TS SDK), but `just check-acp-schema` will fail until it is run.

> **§4, §6, §9 and §1-3 verified end-to-end (2026-08-26)** against live goose +
> cow-mcp with real tenant credentials:
> - **§4** — with `websocket_headers.v0` planted the compliancecow tool returned
>   386 rules (103KB); the same call with no headers returned 401. Controlled
>   negative test, so the headers are demonstrably what made the difference.
> - **§1-3 / §6 / §9** — after `session/provider/update`, the persisted session
>   shows `provider_name=anthropic`, `request_params.thinking={"type":"disabled"}`
>   (§9) and `request_params.metadata.user_id` populated from the security
>   context's `ID` (§6).
>
> **Prerequisite:** the `compliancecow` extension in `config.yaml` needs
> `allowed_headers: [Authorization, X-Cow-Security-Context]`. It had none, so §4
> forwarded nothing and every call 401'd. **Production needs this too.**

> **Full-stack run (2026-08-26)** — browser WS → CowGooseService (`COWGOOSE_USE_ACP=1`)
> → ACP → `goose serve` → cow-mcp, with a live tenant security context:
> - Recipe selection over ACP works: goose stored `recipe_json.title =
>   "ComplianceCow Rules Specialist"` for session type `rules`.
> - **§4** — `websocket_headers.v0` carried `authorization` +
>   `x-cow-security-context`; tools returned 37,795 / 3,437 byte authorized
>   payloads. Negative control with no headers: 401 from cow-mcp.
> - **§12** — `cow_tenant.v0` written with `session_type` + `original_session_id`.
> - Streaming to the browser: 391 `response`, 3 `tool_request`, 3 `tool_response`,
>   1 `complete`.
>
> This run also exposed a CowGooseService bug (fixed there, not in goose): goose
> sends `session/update.content` as an object on message chunks but as a **list**
> on `tool_call_update`. The Go client modelled only the object form, so every
> tool result failed to decode and was silently discarded — the browser saw tool
> requests that never resolved. See `cowgooseservice/TRACK_B_ACP.md`.

> **Non-OSC full-stack run (2026-08-26, after tunnelling cowlangservice + Postgres)**
> — with `OSC_GOOSE=false` the whole provider-resolution chain ran: prompty config
> returned `DeepSeek` / `deepseek-v4-pro`, the vault produced the per-session key,
> and the bridge called `session/provider/update`. The persisted goose session shows
> every remaining feature at once:
> - **§1-3** — `provider_name = anthropic`, `model = deepseek-v4-pro` (DeepSeek→anthropic
>   mapping), ephemeral vault key, never persisted as config.
> - **§9** — `request_params.thinking = {"type": "disabled"}`.
> - **§6** — `request_params.metadata.user_id` populated from the security context `ID`.
> - **§12** — `cow_tenant.v0.domain_id` / `user_id` carry the real tenant ids.
>
> `cowauthservice` (:12300) was still unreachable, which turned out not to matter:
> `cowcommonlibs/util.GetSecurityContextHeader` trusts `X-Cow-Security-Context`
> directly when `Authorization` is absent, and `ExtractWhitelistedHeaders` recovers
> the token from the context's `AuthToken` — so §4 still forwarded a working token.

**Reading the session store:** goose writes `sessions.db` through SQLite **WAL**.
Copying the `.db` file alone misses recent sessions — open it read-only with
`file:<path>/sessions.db?mode=ro` instead.

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
