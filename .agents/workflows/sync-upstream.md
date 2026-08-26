---
description: Sync the ComplianceCow fork onto the latest upstream (block/goose) main without breaking local features
---
# Sync Upstream Workflow

Pulls the latest upstream `block/goose` `main` into this fork and re-applies our
commits on top, **preserving every ComplianceCow customization**.

> **Read `COMPLIANCECOW_FORK_FEATURES.md` first.** It is the checklist of what
> must survive the sync. Conflict resolutions and the final verification are all
> judged against it. If a feature isn't in that file, assume it's *not* protected —
> add it there before syncing.

## Prerequisites
- Clean working tree (commit or stash first).
- You've read `COMPLIANCECOW_FORK_FEATURES.md` end to end.
- Toolchain active: `source bin/activate-hermit`.

---

## 1. Safety net — clean tree + backup branch
The backup branch is the escape hatch: if anything goes wrong, `git reset --hard`
back to it.
```bash
git status                              # must be clean
git rev-parse --abbrev-ref HEAD         # note the branch you're syncing
git branch backup/pre-sync-20260707     # use today's date; keep until the sync is validated
```

## 2. Point `upstream` at block/goose and fetch
The canonical upstream is **`block/goose`** (GitHub redirects it to `aaif-goose/goose`
— both resolve to the same repo; `block/goose` is the name to use).
// turbo
```bash
git remote get-url upstream 2>/dev/null || git remote add upstream https://github.com/block/goose.git
git fetch upstream main
```

## 3. Assess the scope BEFORE rebasing
This decides whether it's a normal rebase or a major-refactor reconciliation.
```bash
git rev-list --left-right --count upstream/main...HEAD   # left = upstream-only, right = ours
git diff --stat "$(git merge-base upstream/main HEAD)" upstream/main | tail -1
# Are the provider files our patches touch still where we expect? (refactor signal)
git cat-file -e upstream/main:crates/goose-providers/src/anthropic.rs && echo "provider crates present"
```
- **Modest delta, our files unmoved** → normal rebase → **step 4a**.
- **Large delta and files we patch were moved/deleted** (like the 2026-07 crate
  refactor) → reconcile on a fresh branch → **step 4b**.

## 4a. Normal path — rebase
```bash
git rebase upstream/main
```
On each conflict:
- `git status` lists conflicted files; open each and find the `<<<<<<<` markers.
- The **`HEAD`/upstream** side is the newly fetched code; the **incoming** side
  (labeled with our commit subject) is our feature.
- Resolve so upstream's structural change is kept **and** the fork feature stays
  intact. Cross-reference that feature's file list in
  `COMPLIANCECOW_FORK_FEATURES.md` to know exactly which lines are ours.
- Stage and continue:
  ```bash
  git add <resolved_file>
  git rebase --continue
  ```
- Repeat until the rebase finishes. **If you can't tell which side is correct, stop
  and ask — do not guess.**

## 4b. Major-refactor path — reconcile on a fresh branch
When upstream relocated files our patches touch, a mechanical rebase fights every
commit. Instead, re-apply features into their new homes:
```bash
git checkout -b sync-upstream-20260707 upstream/main
```
Then, feature by feature from `COMPLIANCECOW_FORK_FEATURES.md`:
1. Check whether upstream already provides it (`git grep` its markers) — if so, skip.
2. Otherwise port it into its **new** location, compiling between features.

This is exactly what the 2026-07-05 sync did; that entry documents each feature's
new location.

---

## 5. Feature-preservation audit — the "don't break our features" gate
A clean compile does **not** prove features survived (a dropped feature still
compiles). Verify every fork customization is still present:

```bash
# Every distinctive fork identifier must still resolve in the tree.
for t in create_with_api_key from_api_key DynamicHeaderClient allowed_headers \
         websocket_headers update_extension_data anthropic_user_metadata \
         thinking_disable_model_patterns GOOSE_THINKING_DISABLE_MODELS \
         cache_control_disabled ANTHROPIC_DISABLE_CACHE ANTHROPIC_CACHE_TTL \
         GOOSE_DB_MAX_CONNECTIONS; do
  git grep -q "$t" -- '*.rs' && s=OK || s=MISSING
  printf "%-34s %s\n" "$t" "$s"
done
```
Any `MISSING` = that feature was dropped by the rebase → re-apply it from the
registry before proceeding.

Re-check the **branding** prompts — upstream owns these files and a rebase
reintroduces "goose":
```bash
grep -n "goose\|AAIF\|Agentic AI Foundation" \
  crates/goose/src/prompts/system.md \
  crates/goose/src/prompts/subagent_system.md \
  crates/goose/src/prompts/tiny_model_system.md
```
Any hit → re-apply the moocp / ComplianceCow rebrand.

## 6. Build & verify
```bash
source bin/activate-hermit
cargo fmt
cargo build
cargo clippy --all-targets -- -D warnings
cargo test -p goose-provider-types --lib format          # cache_control / thinking format tests
cargo test -p goose --test compliancecow_features_test   # the fork's own feature gate
```

`compliancecow_features_test.rs` is the gate that matters here: it is **fork-owned**,
so a rebase cannot quietly revert us the way it can with a test that lives inside an
upstream file. When a sync reverts fork behaviour whose upstream test asserts the
opposite, move our assertion into that file rather than editing upstream's test in
place — that is how the §13 session-naming regression got through once.

If any goose-server route or request type changed, regenerate the API spec:
```bash
just generate-openapi
```

### Full-stack validation
Unit tests do not catch wiring: a feature can compile, pass its tests, and still
never reach cow-mcp. With `goose serve` and CowGooseService both up:

```bash
export CCOW_SECURITY_CONTEXT='<the security-context JSON>'
python3 scripts/validate-compliancecow.py
```

It exercises the session-admin ACP methods, §4 header forwarding (with a negative
control that must fail), tool results actually reaching the browser, and §1-3 / §6 /
§9 / §12 / §13 as goose persisted them. Anything it cannot reach is reported SKIP,
never PASS, so a partial run can't be mistaken for a clean one — read the skip list
before calling a sync verified.

## 7. Verify the CowGooseService contract
The Go bridge depends on these goose-server endpoints — all must still exist:
`/agent/start`, `/agent/update_provider`, `/sessions/{id}`, `/sessions/{id}/reply`,
`/sessions/{id}/events`, `/sessions/{id}/cancel`, `/sessions/{id}/extension_data`,
`/sessions/{id}/fork`, `/sessions/{id}/name`.
```bash
git grep -hoE 'path = "(/agent/[a-z_]+|/sessions/\{session_id\}[a-z_/]*)"' \
  crates/goose-server/src/routes/ | sort -u
```
Also confirm the request types the bridge relies on are intact:
- `UpdateProviderRequest` still has `api_key` + `host` (§1–3).
- `StartAgentRequest` still has `extension_data` (§4 header injection).

## 8. Finalize
- Update `COMPLIANCECOW_FORK_FEATURES.md`: new sync date, branch name, and any
  feature whose location or upstream status changed.
- Keep the `backup/pre-sync-*` branch until the sync is validated **end to end**,
  including a live CowGooseService run confirming §4 header forwarding to cow-mcp.
- Adopt the synced branch when satisfied:
  ```bash
  git branch -f <your-working-branch> <synced-branch>
  ```

> **Golden rule:** compile-green is necessary but not sufficient. A sync is only
> done when steps 5–7 pass — the fork identifiers are all present, branding is
> reapplied, the build/clippy/tests are clean, and the CowGooseService endpoints +
> request fields are intact.
