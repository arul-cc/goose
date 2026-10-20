# Autonomous loop — gate rules (draft for review)

Status: **draft; implemented in the ComplianceCow repo but not wired in yet** (§8: the decision function and its 87 tests, nothing calls it). Written 2026-10-20. Companion to
[AUTONOMOUS_LOOP_DESIGN.md](AUTONOMOUS_LOOP_DESIGN.md) §5. This file says exactly what the bridge
does each time the agent finishes a prompt. Facts seen in the four phase 0 runs (sessions
`20261020_1` to `_4`, goose session store) are marked **(seen)**; everything else is a proposal.

## 1. What the gate is

After every prompt that ends (ACP stop reason `end_turn`), the bridge calls one pure function:

```
decide(run, round) -> Decision{ action, code, findings[], message_to_agent }
  action: ACCEPT | CONTINUE | STOP
```

- **ACCEPT** — the work is ready for the next stage (at autonomy level L1: ready for a human to review
  and publish; the gate never publishes).
- **CONTINUE** — send `message_to_agent` as the next prompt in the same session.
- **STOP** — the run ends. `code` says why, and the report goes to the user as-is.

The function takes only data the bridge already has: the final assistant text, the tool-call stream of
this run, the run's counters. It makes no model call, so the same input always gives the same decision
and it can be tested table-driven.

## 2. What the bridge can see

| Signal | Source | Seen in runs |
|---|---|---|
| Stop reason | ACP prompt result; goose only ever returns `end_turn`, cancelled or max-tokens | `end_turn` every time |
| Final assistant text | last assistant message | yes |
| Turn-limit sentence | goose's fixed message, "I've reached the maximum number of actions I can do without user input. Would you like me to continue?" | run 4, turn 12 |
| `run-result` block | last fenced `run-result` JSON in the final text | present in runs 1–3 and in run 4 after the follow-up; absent in run 4's first prompt |
| Tool calls and results | `tool_request` / `tool_response` stream the worker already forwards | yes (**assumption:** the worker holds the full response text, not a truncation; verify) |
| Policy and score verdicts | `tool_score` records by `tool_call_id` | all low, none blocked |

## 3. Decision order

Rules run top to bottom. The first rule that applies decides. Codes are stable strings for logs and UI.

### 3.1 Stops that do not depend on the agent's report

| # | Condition | Action | Code |
|---|---|---|---|
| S1 | The user cancelled | STOP | `CANCELLED` |
| S2 | Two prompts in a row failed with an infrastructure error (provider, vault, bridge, MCP outage) | STOP | `INFRA_FAILURE` |
| S3 | A tool call was blocked by the policy hook and the agent tried it again | STOP | `POLICY_VIOLATION` |

A single infrastructure error is not a gate decision: retry the same prompt with backoff, inside the
budget, without spending a round.

**Budgets are not in this table on purpose.** A budget (rounds, wall clock, tool calls; §6) stops a run
that would otherwise go round again. It never throws away a result that is already acceptable: if the
decision would be ACCEPT, the budgets are not consulted. If the decision would be CONTINUE and a budget
is spent, the decision is STOP with code `BUDGET_EXHAUSTED`. "Exceeded" means strictly more than the
limit; exactly at the limit still allows the round. (Found while writing the tests: the first draft put
the budget stop first, which would have discarded a good result that arrived on the last round.)

### 3.2 The agent's report

The report is the **last** `run-result` block in the final message. "Last" matters: the agent may quote
an earlier one.

| # | Condition | Action | Code |
|---|---|---|---|
| R1 | No `run-result` block, and the text contains the turn-limit sentence | CONTINUE | `INCOMPLETE_TURN_LIMIT` |
| R2 | No `run-result` block (agent asked a question, stopped early, or ended with prose) | CONTINUE | `INCOMPLETE_NO_REPORT` |
| R3 | Block present but not valid JSON, or a required field is missing or has the wrong type (§4) | CONTINUE once, then STOP | `REPORT_MALFORMED` |
| R4 | `status` is `blocked` | STOP | `BLOCKED` |
| R5 | `status` is `failed` | STOP | `AGENT_FAILED` |
| R6 | `status` is `completed` | go to §3.3 | n/a |

Keying R1 and R2 on the **absence of the report**, not on the turn-limit sentence, keeps the gate
working if goose rewords the sentence. The sentence only chooses the wording of the message to the agent
and the code in the log. There is a test that fails if the report check and the sentence disagree on the
recorded run-4 fixture.

`blocked` and `failed` are terminal on purpose. Run 3 showed the correct behaviour for `blocked`: it
searched, found nothing, created nothing, listed what is needed. Sending the agent back would only invite
it to invent what is missing. A person decides what to do with `needs`.

### 3.3 Verifying a `completed` claim

The agent saying `completed` is a claim. The bridge checks it against the tool stream of this run. Each
check below is cheap and deterministic. All must hold, or the decision is CONTINUE with the failed checks
listed.

| # | Check | Why | Code on failure |
|---|---|---|---|
| V1 | Every item in `checks` has `result: pass` and a non-empty `evidence` string | `completed` means every check passed with evidence | `CLAIM_CHECKS_NOT_PASSING` |
| V2 | A successful `execute_rule` exists such that every call that changed the rule had its **result** before that `execute_rule` was **issued**. Changing calls are `create_rule`, `update_rule`, `configure_rule_output_schema` when they succeeded, and `confirm_template_input` / `confirm_parameter_input` when their result says `rule_updated`. A call with no result yet counts as a change, because it may already have happened. | A rule changed after its last verification is unverified. Failed changes do not count. | `RULE_CHANGED_AFTER_VERIFY` |
| V3 | For that `execute_rule`'s `execution_id`, a `fetch_execution_progress` result shows `COMPLETED` and zero errored tasks (`task_stats.error`) | The run actually finished | `EXECUTION_NOT_CONFIRMED` |
| V4 | A `fetch_output_file` call was issued after V3's completing result arrived (only evaluated when V3 holds) | The agent looked at the output, not only the status | `OUTPUT_NOT_INSPECTED` |
| V5 | The last `create_rule` / `update_rule` result reports status `ACTIVE` | A DRAFT rule is not done (run 4 had DRAFT 85% mid-run) | `RULE_NOT_ACTIVE` |
| V6 | Every execution id in the report appears in a tool result of this run | Cheap guard against a fabricated id | `EVIDENCE_NOT_IN_RUN` |
| V7 | `created` is non-empty and names the rule | A completed run produced something | `NOTHING_CREATED` |
| V8 | `design notes` and `README` creation each returned success (when the requirement type needs them) | Part of "done" in the recipe | `DOCS_MISSING` |
| V9 | No application other than the requirement's named one, or `nocredapp`, was used in `execute_task` / `execute_rule` | The no-invented-credentials rule | `UNAUTHORIZED_APPLICATION` |

All failed checks are listed in `findings` and in the message to the agent. The decision's `code` is the
first failed check in table order (V1 first), so the code is stable when several checks fail at once.

If every check holds: **ACCEPT** with code `ACCEPTED_L1`.

What the checks do on the recorded runs (computed from the session store, not assumed):

| Run | V2 | V3 | V4 | V5 | V8 | Result |
|---|---|---|---|---|---|---|
| 1 (simple rule) | **fails** | holds | holds | holds | holds | CONTINUE `RULE_CHANGED_AFTER_VERIFY` |
| 2 (join) | holds | holds | holds | holds | holds | ACCEPT |
| 4, after the follow-up | holds | holds | holds | holds | holds | ACCEPT |
| 4, first prompt | holds | fails | fails | holds | fails | already CONTINUE by R1 |

Run 1 is the interesting one. Its only whole-rule execution happened *before* the final `create_rule`
that added the mandatory outputs and took the rule from DRAFT to ACTIVE. The agent reported `completed`
and "ACTIVE (100%)", but the ACTIVE version of the rule was never executed. V2 would have sent it back for
one more execute-and-check round. That is correct strictness, not a false alarm, and it is exactly the
"fix A breaks B" risk the design warns about. It also means the gate costs a round on a run that looked
fine.

### 3.4 Counting rounds and repeated failures

Every CONTINUE costs one round. The gate keeps the **failure signature** of each round: the sorted list of
failed check codes plus a short hash of the last tool error text. Then:

| Condition | Action |
|---|---|
| Signature differs from the previous round | CONTINUE normally |
| Same signature as the previous round | CONTINUE with the "change your approach" message (§5.3) |
| Same signature three rounds in a row | STOP, code `STUCK` |
| Round count would exceed the limit | STOP, code `BUDGET_EXHAUSTED` |

Within one prompt the gate cannot act (it only runs at the end), so a loop inside a prompt is bounded only
by `GOOSE_MAX_TURNS`. A tighter inside-prompt guard (cancel after the same failing call four times) is a
later improvement, not part of this spec.

## 4. The report schema the gate requires

```json
{
  "status": "completed | blocked | failed",
  "rule_name": "string, non-empty",
  "summary": "string",
  "checks": [{"name": "string", "result": "pass | fail | not_run", "evidence": "string"}],
  "created": ["string"],
  "needs": ["string"],
  "error": "string"
}
```

`status`, `rule_name`, `checks`, `created`, `needs` and `error` are required. Extra fields are ignored.
`blocked` requires a non-empty `needs`; `failed` requires a non-empty `error`; a `blocked` or `failed` report
with those empty is treated as malformed (R3). **Seen:** all four final reports parse against this schema.

## 5. Messages to the agent

Each message states facts the bridge established, never guesses, and always tells the agent not to redo
finished work. The wording below is the one that worked in the run-4 follow-up.

### 5.1 `INCOMPLETE_TURN_LIMIT` and `INCOMPLETE_NO_REPORT`

> Gate round {n} of {max}: the run ended before it was finished. No run-result block was produced.
> {state} Not done: {unfinished}. Continue from the current state. Do not recreate the rule or the sample
> inputs. Finish the verification and end with the run-result block.

`{state}` and `{unfinished}` are filled from the tool stream (what exists, what has no result yet), not
from the agent's text. For `INCOMPLETE_NO_REPORT` add: "Do not ask questions; decide and continue."

### 5.2 A failed claim check

> Gate round {n} of {max}: your report says completed, but these checks on the run do not hold:
> {list of code: plain-language reason}. Fix them, then end with a new run-result block. Do not recreate
> anything that already exists.

### 5.3 Repeated signature

> Gate round {n} of {max}: the same problem remains as last round ({signature}). Do not repeat the same
> approach. Change what you are doing: {the failing check or error}.

### 5.4 Malformed report

> Your last run-result block was not valid: {problem}. Reply with only a corrected run-result block, and
> nothing else.

## 6. Budgets (suggested starting values)

| Budget | Value | Basis |
|---|---|---|
| Turns per prompt | `GOOSE_MAX_TURNS=25` | goose config |
| Gate rounds per run | 5 | run 4 needed 2 at a limit of 12; runs 1–3 needed 1 |
| Wall clock per run | 30 min | runs took 40 s to 5 min |
| Tool calls per run | 300 | runs used 31, 31 and 15 |
| Same failure signature | change approach at 2, stop at 3 | §3.4 |
| Infra retries per prompt | 2, with backoff | S2 |

These are starting guesses, not measurements from enough runs. Phase 0 had four.

A budget left at zero allows nothing, so a caller that forgets to set one stops runs instead of letting
them go on for ever. "Exceeded" means strictly more than the limit.

## 7. What the gate cannot catch

Be explicit, so nobody reads ACCEPT as "correct":

- **Meaning, not mechanics.** Run 2's reasons passed every mechanical check while hiding the difference
  between a missing record and a disabled one. Only a reviewer who reads the requirement sees that.
- **Wrong but self-consistent work.** If the agent writes the sample data and the rule from the same
  misreading, every check passes.
- **Quality of the rule beyond one sample.** Outputs were verified on one agent-made sample each time.
- **Anything it cannot see in the tool stream.**

Hence: ACCEPT means "ready for human review", a person publishes, and two later additions are planned
(not part of this spec): held-out checks that the bridge runs itself against cow-mcp with data the agent
never saw, and an independent read-only reviewer.

## 8. Tests

The decision function is pure, so the tests are a table of (final text, tool stream, run state) →
decision. They were written first and failed against a stub; the function is now implemented and all 87
pass (also under `-race`).

- Code: `src/cowgooseservice/autonomous/` in the ComplianceCow repo, branch `cowgoose-autonomous-gate`
  (from `upstream/dev`), uncommitted. `gate.go` has the types and `Decide`; `report.go` parses the
  run-result block; `stream.go` reads the tool stream; `verify.go` holds V1 to V9; `message.go` builds the
  messages and the failure signature. **Nothing calls `Decide` yet**; wiring it into the bridge is the next
  step.
- The package imports only the standard library, so `go test ./autonomous/` runs without the vault the rest
  of the service needs at init.
- Fixtures in `testdata/*.json` are the four phase 0 sessions, extracted and redacted by
  `testdata/extract_fixtures.py` (hosts and tenant ids replaced; discovery results trimmed because the gate
  never reads them; everything the gate parses is kept as recorded).
- **The tests were mutation-checked:** 25 deliberate breaks of the implementation (a threshold changed, a
  check removed, first report block instead of last, and so on) were each caught. Two survived at first, both
  behaviours added during implementation (confirm calls and pending calls counting as changes); tests were
  added and they are caught now.

| Fixture | Expected |
|---|---|
| Run 1 final | CONTINUE `RULE_CHANGED_AFTER_VERIFY` (the rule was changed after its only whole-rule run) |
| Run 2 final | ACCEPT (the reason-text weakness is documented as uncatchable) |
| Run 3 final | STOP `BLOCKED`, carrying the parsed report |
| Run 4, first prompt | CONTINUE `INCOMPLETE_TURN_LIMIT` |
| Run 4, after the follow-up | ACCEPT |

Constructed cases are single-change mutations of run 2: each of V1 to V9 failing alone; two failing at once;
a change answered after `execute_rule` was issued; a failed change after it (must not count); progress of a
different execution; an evidence id the run never produced and one it did; docs optional by requirement
type; an application nobody authorised, one the requirement names (by id and by name), `nocredapp`, and one
passed through `execute_rule`; seven malformed-report shapes, each CONTINUE once then STOP; no report, a
question, the turn-limit sentence with and without a report, first and last of several report blocks;
cancel, two infra errors (one is not enough), a retried policy-blocked call; every budget on both sides of
its limit, and an acceptable result surviving spent budgets; repeat detection over three rounds and its
reset; message contents; purity and determinism.

## 9. Open questions and unverified assumptions

1. Does the worker hold full tool-response text? The V-checks parse it. If it truncates, the bridge needs
   the full text from goose's session store instead.
2. V8 depends on the requirement type. Which types need design notes and a README?
3. Should `AGENT_FAILED` get one more round when budget remains? Spec says no; the data is thin.
4. Are 5 rounds and 30 minutes right? Needs more than four runs.
5. ~~Parallel tool calls: order issued or order arrived?~~ Resolved from the recordings: results do arrive
   out of order (in run 2 `create_design_notes` was issued at seq 61 and answered at 64, after
   `create_rule_readme` at 62 to 63). V2 therefore compares each change's *result arrival* with the
   `execute_rule` *issue* order. The caller must record both counters (`Seq`, `ResultSeq`).
6. Resuming after a bridge restart mid-run is out of scope here (design doc §8).
