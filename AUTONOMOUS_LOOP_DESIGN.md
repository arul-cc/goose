# Autonomous agent loop — design (draft for review)

Status: **draft, partly built.** The autonomous recipe has been run four times by hand (phase 0, §12), and
the gate, its budgets and their wiring into the bridge's WebSocket path are built and have run live once
(§14). Run state, the headless path, the API and cleanup are not built. Written 2026-10-16, rewritten the
same day, updated 2026-10-20. Facts checked
in the code or by a test are marked **(verified)**; everything else is a proposal or an assumption
and is listed in §11.

## 1. Goal and non-goals

**Goal.** Give the system a requirement ("create a rule that checks X") and have the agent plan,
build, check its own work, fix what fails and re-iterate, with no human between start and end. The
run ends with a validated result, or an honest failure report when a budget runs out.

**Non-goals.**
- Replacing human review of production changes. Publishing by itself is a setting (§6); the first
  release does not.
- A new orchestration service, or a separate planner/builder/validator pipeline (kept as an option,
  §10).
- Making the model smarter. The design makes a fallible model's work *checked and bounded*.

## 2. Approach

**The agent runs the loop; a thin harness around it enforces what a model cannot be trusted to.**
This is the common industry shape: the model plans (todo list, optional subagents), acts, observes
and decides its next step, while the harness caps turns and budget, gates "done" on real checks and
keeps state outside the context.

Three harness parts, nothing else:

1. **Completion gate.** When the agent says it is finished, objective checks run. If they fail, the
   failures go back to the agent and it continues. If they pass, the run ends.
2. **Hard budgets.** Time, iterations and tool calls, enforced by code, not by the model.
3. **Persisted run state.** Requirement, status, gate results and created resources, so a run
   survives a crash, can be audited, and can clean up after itself.

**No new process.** The harness is a code path in the existing bridge (CowGooseService), extending
the headless runner and the session worker. Same connection, vault and security-context handling.

## 3. What exists today

| Need | What we have | Limits |
|---|---|---|
| Think, act, observe loop | goose's agent loop **(verified)** | A turn ends at `GOOSE_MAX_TURNS` with a fixed sentence and stop reason `end_turn` **(verified)** |
| Turn limit | `GOOSE_MAX_TURNS` (goose config or env) | A recipe's `settings.max_turns` is **ignored over ACP**; default 1000 **(verified)** |
| Plan tracking | `todo` extension: the list is saved in the session's extension data and injected into the agent's context **(verified)**; `cow-orchestrator` recipe already tells the model to plan, delegate and verify | Capped at `GOOSE_TODO_MAX_CHARS`; the model rewrites the whole list each time |
| Resumable state | goose persists the conversation as its state: effects are saved, and a fresh state machine rebuilds from the stored session **(article; matches `ops_stop_hook.rs`)** | A tool that succeeded but whose result was not saved before a crash is not safe to rerun (the article says so) |
| Gate on "done" | goose **Stop hook** (upstream) **(verified)**: a hook can block a turn from ending; goose adds a hidden message with the reason and the model continues. Capped at 8 consecutive blocks by default (`GOOSE_STOP_HOOK_BLOCK_CAP`), then goose overrides and ends the turn | Command hook, so it runs where goose runs. Whether its timeout allows slow checks, and whether it also fires on the turn-limit message, are **not verified** |
| Unattended runs | bridge headless runner: one prompt per run; statuses PENDING, RUNNING, COMPLETED, FAILED, TIMEOUT, CANCELLED **(verified)** | No gate, no budgets across prompts |
| Guarding actions | tool-scoring hook: shadow or enforce policy, allow lists, Jev score **(verified)** | Enforce needs `on_failure: block` |
| Audit | score and verdict records per tool call, linked by `tool_call_id` | Per call, not per run |

**The tool surface looks human-gated, but the gate is the prompt, not the tools (verified in
`cow-mcp/tools/rules/rules.py`).** `collect_*_input` only validates a value and returns a preview.
`confirm_*_input` stores the value (uploads the file for FILE inputs) and writes it into the rule.
Neither checks for a person; the "ask the user" part lives in the `cow-rules` recipe's instructions.
`execute_task` takes its inputs directly and does not require them to have been collected first. See §7.

**Candidate objective checks (names verified, behaviour not):** `execute_rule`, `execute_task`,
`fetch_execution_progress`, `fetch_output_file`, `check_rule_status`, `verify_control_automation`.
**Creation tools:** `create_rule`, `update_rule`, `configure_rule_output_schema`,
`create_design_notes`. **Side effects beyond a draft:** `publish_rule`, `publish_application`,
`attach_rule_to_control`, `schedule_asset_execution`, `add_check_to_asset`.

## 4. The loop

```
requirement ──► bridge: start run (record, budgets, autonomy level)
                  │
                  ▼
        prompt goose (autonomous recipe: plan with todo, build drafts, self-check)
                  │  agent works, calls tools (policy hook + scoring on every call)
                  ▼
              turn ends (end_turn)
                  │
        bridge runs the GATE ──pass──► DONE (draft ready) ──► human publishes (L1)
                  │fail
                  ▼
        budgets left? ──no──► stop: FAILED_BUDGET + report
                  │yes
        same failure as last time? ──yes──► nudge to change strategy (or stop after N)
                  │no
                  ▼
        send failures as the next prompt to the SAME session ─► loop
```

The agent sees only: the requirement, its own history, and the gate's failure report. It decides how
to fix things. The bridge decides when to stop.

## 5. The completion gate

The exact decision rules, message wording and tests are in
[AUTONOMOUS_GATE_RULES.md](AUTONOMOUS_GATE_RULES.md), written from the four phase 0 runs. This section
is the overview.

The gate is a list of **acceptance checks**, each machine-checkable. For the first release they come
from the requirement type (rule creation), not from the model, so the model cannot weaken them.

- **Run by the bridge, not by the model.** The bridge calls cow-mcp directly with the requester's
  security context, so no model sits between a check and its result.
- **Run after every `end_turn`.** Run the whole suite each time, not just the last change's check, so
  fixing A cannot silently break B.
- **Report, don't just reject.** A failing gate returns which check failed and the actual error,
  written as the next prompt.
- **Repeat a failing check once** before counting it, and classify environment errors (timeout,
  outage) separately from real failures.
- **Held-out checks.** Keep some checks out of every prompt and failure report; the gate runs them but
  the agent only learns "a hidden check failed" (and which requirement area). An agent that can see
  every check can write a fix for the check instead of for the requirement, which is the
  special-purpose-fix failure the goose team describes when agents are left to fix themselves. A pass
  on visible checks alone is therefore not the final verdict: a hidden-check failure sends the run
  back around, and the human at publish (L1) is the last line.
- **Optional reviewer pass.** If self-confirmation proves a real problem in testing, add an
  independent read-only reviewer (a subagent with fresh context) after the objective checks pass. Not
  in the first release.

### Where the gate runs: two options

| | A. In the bridge (recommended first) | B. goose Stop hook calling the bridge |
|---|---|---|
| How | Bridge sees `end_turn`, runs the gate, sends the next prompt | A Stop hook calls a bridge endpoint with the session id; bridge runs the gate and answers allow or block |
| Pros | No limit from goose's hook timeout or block cap; one place for budgets and state; works with any goose version | The agent loop continues inside one turn; goose handles the continuation message; upstream feature |
| Cons | Each retry is a new prompt | Capped at 8 consecutive blocks by default; slow checks may hit the hook timeout (**not verified**); unclear if it fires at the turn limit (**not verified**); hook needs a token to call the bridge |
| Fork impact | None | None |

Start with A. B is a later optimisation if the extra prompt per retry costs too much.

## 6. Budgets, autonomy and safety

**Hard budgets, enforced by the bridge (suggested starting values):**

| Budget | Value |
|---|---|
| Wall-clock per run | 30 min |
| Gate rounds (iterations) per run | 10 |
| Tool calls per run | 300 |
| Turns per prompt | `GOOSE_MAX_TURNS=25` (**verified** to work) |
| Same failure repeated | change strategy after 2, stop after 4 |
| Tokens or cost per run | only if the bridge's usage numbers prove reliable (not verified) |
| Concurrent runs per tenant, runs per day | set per tenant |

When a budget runs out the run stops and returns what it has plus a diagnosis. It never continues
silently.

**Autonomy level (per tenant and requirement type):**
- **L0** manual, as today.
- **L1** plan, build and validate by itself; a human publishes. **First release.**
- **L2** auto-publish when every check passes, no policy hit and risk is low.

**Layered safety:**
- **Draft-only tools.** The autonomous recipe lists only creation and read tools. Publish, attach and
  schedule are not available to the agent; the bridge performs them, and only at L2.
- **Policy hook in enforce mode** with `on_failure: block` and an `allow_tools` list that matches the
  recipe, so the limit holds even if the recipe is wrong.
- **Untrusted content is data.** The bridge never treats tool output as instructions.
- **Tenant isolation as today:** the run uses the requester's security context and vault key; no
  state crosses tenants.
- **Cleanup.** Failed or cancelled runs delete the drafts listed in the created-resource ledger.

## 7. The human-interaction tools

The server never blocks on a person (see §3), so autonomous mode needs **no tool changes and no
bridge answering**: the agent validates a value with `collect_*`, then calls `confirm_*` itself,
which is the call that actually saves the input into the rule. Dropping `confirm_*` would remove the
only way inputs get attached to the rule, so the autonomous recipe keeps all four tools and only
changes the instructions: decide and verify instead of asking. goose's own wait-for-a-person
mechanism (it records a request and yields; per the state-machine article) is not involved, because
the rules tools do not use it.

What autonomous mode cannot supply is **credentials**. For tasks whose appType is not `nocredapp`,
the recipe may use only an application the requirement names or the only matching one, otherwise
it ends `blocked` with the candidates listed. That is a deliberate stop, not a failure.

The autonomous recipe is `cow-rules-auto.yaml`
(`src/cowgooseservice/configs/recipes/` in the ComplianceCow repo, `dev` branch): a strict subset of `cow-rules`' tools (36 of 58; publish, attach, asset,
check, schedule and note tools removed), instructions rewritten for autonomy, and a required
`run-result` block (status `completed | blocked | failed`, per-check evidence, resources created)
that the gate reads. The bridge does not pass recipe parameters, so the requirement arrives as the
prompt, and the recipe is selected by session type (`cow-` + type, so `rules-auto`).

## 8. State kept by the bridge

| Record | Contents |
|---|---|
| Run | requirement, tenant, requester, autonomy level, status, budget ledger |
| Gate round | time, checks run, pass/fail per check, error text, normalised failure signature |
| Created resources | every draft the run created (for cleanup) |
| Tool calls | `tool_call_id`s linking to the score and verdict records |
| Final report | what was built, which checks passed, resources, budget used |

The agent's plan is its `todo` list, which goose already saves with the session, so the bridge does
not keep a second copy. A long run can still drift; the gate and budgets limit the damage. The bridge
stores the session id in the run record and resumes the same session after a restart.

**Resuming safely.** goose can rebuild from the stored session, but a tool call that succeeded just
before a crash may not have its result saved, and rerunning it is not safe. On resume the bridge must
therefore (1) read the created-resource ledger, (2) check which drafts exist, and (3) tell the agent
what already exists, instead of letting it call the create tools again. Create tools that are not
idempotent are listed in the run record as "maybe created" until confirmed.

## 9. API, UI and observability

- **API:** start, status, cancel for a run, built on the headless endpoints.
- **Events:** an `autonomous_event` message on the existing WebSocket (round started, gate result,
  budget status, finished) so the UI can show progress. Built for the WebSocket path; see §14.
- **Metrics:** success rate, gate rounds to success, cost, failure counts by check.
- **Failure review (a human in the improvement loop, not the run loop).** Keep every gate failure
  with its check id, error text and the tool calls around it. On a regular schedule a person reads the
  patterns, not the pass rate, decides what the general lesson is, and fixes it in the recipe, the
  tools or the checks. Letting the agent patch its own recipe from failures invites fixes that only
  satisfy the visible checks; the goose team reports the same and keeps a person generalising.

## 10. If the thin version is not enough

Only if testing shows a concrete failure the gate cannot catch (for example the agent keeps passing
checks while missing the requirement), add heavier parts, one at a time:

1. An independent read-only reviewer after the gate passes.
2. Separate planner and builder roles with their own tool allow-lists.
3. A persisted plan the bridge owns, with a step-by-step state machine.

This is the earlier, controller-centric design. It costs more to build and tune, so it needs evidence
first.

## 11. Failure modes

| Risk | Mitigation |
|---|---|
| Runaway loop or cost | Hard budgets; repeated-failure detection; per-prompt turn cap |
| Declares success without proof | Gate runs objective checks the model cannot weaken |
| Fits the checks, not the requirement | Held-out checks; human at publish; gate failures reviewed for patterns |
| Rerun after a crash duplicates a side effect | Resume reads the resource ledger and tells the agent what exists |
| Fix A breaks B | Whole suite on every round |
| Misread requirement | Final report lists the checks used; L1 keeps a human at publish |
| Partial state left behind | Created-resource ledger and cleanup |
| Prompt injection | Untrusted data never becomes instructions; enforce-mode policy; draft-only tools |
| Flaky checks | Repeat once; separate environment errors |
| Vault, LLM or cow-mcp outage | Retry with backoff inside the budget; mark the run resumable |
| Bridge crash | Run state is persisted; resume from the last gate round |

## 12. Phases and exit criteria

0. **Manual spike, no new code.** Run 5 real requirements by hand against the autonomous recipe with
   `GOOSE_MAX_TURNS=25`, run the checks yourself between rounds, and record iterations, failures and
   cost. This is where the real-model behaviour gets tested. Use a mid-tier model, not the strongest
   one, so enough failures show up to reveal missing capabilities (the goose team does this for its
   benchmarks).

   **Results so far (2026-10-20, 4 of 5 runs, all on the dev tenant, each checked against the tool
   results, not only the agent's report):**

   | Run | Requirement | Outcome | Turns |
   |---|---|---|---|
   | 1 | Simple rule, no credentials | completed; 5 failed `create_rule` calls, self-corrected | 25 |
   | 2 | Join two files | completed; statuses correct, reason text did not tell a missing record from a disabled one | 25 |
   | 3 | Needs a saved application that does not exist | `blocked`, nothing created, nothing invented | 7 |
   | 4 | Join two files, limit lowered to 12 | stopped at the limit with no report; a gate-style follow-up finished it | 12, then 6 |

   What it showed: the agent stops correctly when it cannot proceed; the turn-budget counter it sees
   does not make it wrap up (it was still starting long steps at 11 of 12); a mechanical check can pass
   while the intent is only partly met (run 2); and run 1's final rule version was never executed, which
   the gate rules catch (`AUTONOMOUS_GATE_RULES.md`). Also found in cow-mcp: `fetch_rules_suggestions`
   errors because it returns a list where a dict is required, and the rule-name validator rejects
   hyphens. Two recipe wording fixes are still to do: allow a minimal, reported rename when the name
   validator rejects the name, and make `blocked` reports say which application tags were searched.
1. **Bridge harness MVP.** Gate in the bridge (option A), budgets, run state, rule creation only, L1,
   drafts only. Exit: at least 80% of a fixed benchmark set passes unattended, **zero** side effects
   outside drafts, every run within budget.
   **Status 2026-10-20:** the gate, the budgets and the wiring are built (§14) and ran live once. Not
   done: run state that survives a bridge restart, the headless path, and the benchmark set, so the exit
   criteria are not yet measured.
2. **Service.** API, events, resumability, per-tenant limits, cleanup.
3. **Stop-hook gate (option B), L2 and other use cases**, only after phase 2 metrics hold.

The harness logic is deterministic and can be tested with the stand-in model and fake ACP server
already used for the bridge: budgets, repeat detection, resume after a crash, cancel.

## 13. Open questions and unverified assumptions

**Decisions needed**
1. What is "done" for rule creation: which checks are objective, and can `execute_rule` run against
   sample data without touching real systems? (Biggest gap; the gate is only as good as these.)
2. May the agent ever publish on its own (L2)? Recommended: not in the first release.
3. ~~What do the collect and confirm tools do server-side?~~ Answered in §3 and §7. Remaining: does
   a rule in status ACTIVE (complete, not published) stay invisible outside the creator until
   `publish_rule`? Phase 0 should confirm this before any run touches a shared tenant.
4. Budget values, and who pays for LLM usage per run.
5. What must the UI show during a run?

**Not verified**
- Stop-hook timeout, and whether it fires when the turn limit ends the turn.
- Whether the usage numbers the bridge forwards are reliable enough for a cost cap.
- How `execute_rule` behaves on failure, and how long it takes.
- Real-model behaviour of the whole loop. Phase 0 and one live gated run (§14) are the only evidence
  so far.

## 14. As built: the gate in the bridge (2026-10-20)

Code is in the ComplianceCow repo, `src/cowgooseservice/`: the decision function in `autonomous/`
(branch `cowgoose-autonomous-gate`) and the wiring in `bridge/autonomous_loop.go` and
`bridge/acp_session_worker.go`. Both are on the local `dev` at `693ffd365`, **not pushed**.
`AUTONOMOUS_GATE_RULES.md` has the decision rules; this section is how they are attached.

**When it applies.** Only to session types listed in `AUTONOMOUS_SESSION_TYPES` (default
`rules-auto`). Every other session takes the original path unchanged. An empty value turns it off.

**What happens to a message.**
1. The worker sends the requirement to goose as the first prompt.
2. When the prompt returns, the worker waits for goose's queued updates to be handled (goose sends a
   prompt's updates before its result, and the connection reader queues them in that order), then reads
   the run's tool calls and the agent's closing text.
3. `autonomous.Decide` says accept, continue or stop.
4. Continue: the gate's message goes to goose as the next prompt in the same session. Accept or stop:
   the run ends with the usual `complete`.

**What the browser receives.** Everything it received before, plus an `autonomous_event` after each
decision: `data.event` is `gate_accept`, `gate_continue` or `gate_stop`, with `code`, `round`,
`max_rounds`, `findings` (code and detail each), the `message_to_agent` on a continue, and the agent's
`report` (status, rule name, summary, created, needs, error) when it gave a valid one. Each decision is
also one line in the bridge log (`Autonomous gate: ... action=... code=...`). Whether the UI handles the
new message type was not checked; only Postman was used.

**Settings** (all optional; an invalid value gives the default, never zero):

| Variable | Default | Meaning |
|---|---|---|
| `AUTONOMOUS_SESSION_TYPES` | `rules-auto` | Session types that run through the gate |
| `AUTONOMOUS_MAX_ROUNDS` | 5 | Gate rounds after the first prompt |
| `AUTONOMOUS_MAX_MINUTES` | 30 | Wall clock for one run |
| `AUTONOMOUS_MAX_TOOL_CALLS` | 300 | Tool calls in one run |

**Behaviour worth knowing**
- One run at a time per session. A second message during a run is refused (`AUTONOMOUS_RUN_ACTIVE`).
- A cancel reaches the worker, so a cancelled run stops at its next decision instead of sending the agent
  round again.
- A prompt that fails is retried once after 2 seconds; two failures in a row stop the run
  (`INFRA_FAILURE`). A session that stays busy is reported as `SESSION_BUSY`, not as a failed round.
- An application counts as allowed only if the requirement names it by id or name; `nocredapp` is always
  allowed.
- A refusal from the model is reported as the error it is.

**Live result.** Session 20261020_8, the rules-auto recipe: round 1 the agent reported `completed` with the
rule still in DRAFT, and the gate answered `CONTINUE` on `RULE_NOT_ACTIVE`; round 2 it saved the rule as
ACTIVE, ran it again and opened the output, and the gate answered `ACCEPT`. The output was right for all
three sample users. Session 20261020_7 hit a 502 from the dev backend on 8 calls; the agent reported
`blocked` and the gate stopped the run, correctly.

**Tests.** 117 in the bridge package, which drive the real router, reader goroutine and flush barrier with
the four recorded runs, under `-race`. They were mutation-checked: of 21 deliberate breaks of the wiring,
all are caught (3 needed new tests first: the wall clock, a second message starting a fresh run, and an
old cancel not stopping the next run).

**Not built, and known limits**
- **Only the WebSocket path.** The headless runner still runs one prompt per execution, with no gate.
- **No run state.** Counters live in the worker. A bridge restart loses a run; there is no status or
  start API.
- **No cleanup.** A run that stops leaves its drafts. Session 20261020_7 left a half-built rule
  (`rule_2891`) on the dev tenant.
- **The gate checks mechanics, not meaning.** Run 2's reason text passed every check while hiding the
  difference between a missing record and a disabled one. ACCEPT means ready for a person to review.
- **One live gated run** is the only evidence the loop works end to end.
