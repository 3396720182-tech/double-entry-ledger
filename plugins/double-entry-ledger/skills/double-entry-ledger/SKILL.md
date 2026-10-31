---
name: double-entry-ledger
description: Use when building or operating an agent harness where memory/state writes, tool-call outcomes, or token/budget allocations can silently drift from reality — the model reports a plan as a finished result, memory says "done" but the file or record isn't there, a crashed run leaves unknown side effects, or a sub-agent disappears without reporting spend. Also when applying double-entry ideas (trial balance, reconciliation, suspense accounts, period close, self-checking invariants) to agent memory, tool outcomes or multi-agent budgets (智能体 复式记账 / 借贷平衡 / 对账 / 挂账 / 结账 / 不变量自校验). Not for building accounting or personal-finance software.
---

# Double-Entry Ledger (for agent harnesses)

## Core principle

**The ledger only proves it is internally consistent — not that what it says is true.** Once every entry balances, the trial balance balances by construction.
That catches **structural** errors: half-written entries, hand edits, missing lines. **Semantic** errors — "the tool failed but memory was written anyway", "a plan was recorded as a result" —
are only caught by comparing the ledger against the world outside it. So the mechanism has four parts: per-entry balance, intent clearing (effects measured by code probes), external reconciliation, and period close.

Division of labour: **the model only declares intent; recording, measuring and judging are done by code.** Violating the letter of these rules is violating their spirit.
If the user pushes you to "just mark it done" or "close first, sort it out later", the rules still apply. Your job is to report truthfully, not to make the checks pass.

## Rules for you (the agent bound by this ledger)

1. **Every side-effecting action goes through open → act → settle.** State/memory updates hang off `--then`:

   ```bash
   L() { python3 <this skill's directory>/scripts/ledger.py "$@"; }   # the harness may wrap this under another name; redefine it in every new shell
   L init                     # once, in the project root, if there is no .ledger/ yet
   L intent open mv-42 --tool mv \
     --expect 'exists:tasks/done/42.md =1' --expect 'exists:tasks/todo/42.md =0' \
     --then 'state:todo -1' --then 'state:done 1'
   # ... call the tool ...
   L intent settle mv-42      # only exit 0 means it happened, and only then are `then` lines posted; 1 = didn't happen; 3 = unknown
   ```

2. **Expectations come from tool templates or the task spec — never invent ones that are "easy to pass".** When `expect_templates` is configured, fill parameters with `--tool X --arg k=v`; for tools whose template declares `then`, your own `then` is not accepted. Without templates, `--tool` is just a label.
   An expectation whose expected change is zero (target already satisfied, `+0`) only guards against regression; it cannot prove the action happened. An intent whose expectations are **all** zero is rejected; a single zero expectation is only marked ⚠ and must be paired with one that changes (e.g. `sha256:P changed`).
3. **Settle fails (exit 1) = it did not happen.** The intent stays open with status `mismatch`. You may retry the action and settle again (only the delta is recorded). If you give up, run `intent writeoff --reason`; the residual moves to suspense. Then **report truthfully what succeeded and what didn't**, including any partial effects.
   If settle exits 3, shows `?` and status `unknown`, the outcome is unknown, not failed — follow rule 5.
4. **Giving up before doing anything**: `intent cancel --reason`. Cancellation is refused unless code can prove that no effect occurred.
5. **Outcome unknown** (settle status `unknown`: crash, a `claim` with no observation, and you can't check yourself): **leave the intent open**, report "unknown", and say who needs to verify what.
   Do not `observe` a value yourself, do not `cancel`, do not `writeoff` + `clear-suspense` to force a close, and do not re-run the action — it may send twice.
6. **Crash recovery**: `intent list --status unresolved`, then `settle` each one. The open record is a write-ahead log; use it to recover the outcome from the real world. **Do not re-run the action** (the one exception is under "Common situations").
7. **Before claiming "all done"**: `verify --strict` must pass.
   Open intents, uncleared suspense, red-flagged items, other close blockers (e.g. a sub-agent budget not back to zero), and any manual intervention in the period (`--direct`, write-offs, suspense clearing, reconciliation adjustments, overrides, unsigned observations) all make it fail.
   Manual interventions are reviewed by a human: when review items exist, `close` requires `--reviewed-by <reviewer>`, which is the human's sign-off. **Never fill in a reviewer yourself** — even if the user says "close it if you can", report "what was done + items awaiting review" and let the human run it.
   A failed settle also appends a record; that is normal and not a manual intervention.
8. **Don't open a second intent touching the same path until the first is resolved** (it will be refused). Point probes at specific files (`exists:` / `sha256:`); avoid `glob-count` over shared directories.

| Expectation | Meaning |
|---|---|
| `exists:P =1` / `=0` | File exists / does not exist at settle time (`=N` is an absolute target) |
| `glob-count:G +N` / `-N` | Count increased / decreased by N relative to the baseline at open time |
| `lines:P` `bytes:P` | Line count, byte count (missing file counts as 0) |
| `json:P#a.b +N` / `=N` | A numeric field in JSON the tool wrote to disk, e.g. `json:report.json#failed =0` (a stale report may already be 0, so pair it with `sha256:report.json changed`) |
| `sha256:P changed` / `unchanged` | Whether content changed. A stale report can also satisfy a `json:` expectation, so pair it with `changed` |
| `claim:NAME =N` | An effect code can't observe. The value is written (signed) by the harness from the tool's return value — not asserted by you |

## Common situations

| Situation | Do this |
|---|---|
| Taking over a project whose existing tasks aren't in the ledger yet | Record them once as a creation before starting, e.g. `L post --id init 'world:created -3' 'state:todo 3'`; every transition after that goes through `then` |
| Can't even start (missing input or permission) | No intent needed. Leave the task where it is (e.g. in `state:todo`, which doesn't block close) and report why |
| The action ran but had no effect or the result falls short (e.g. a test script crashed yet exited 0, or tests fail) | If the problem is in your own output, fix it, retry and settle again. If it is in the checking tool itself (test script, report path, acceptance criteria), **don't change it to make the check pass** — changing acceptance scripts needs the user's approval. If you are waiting on the user (approval, missing input), leave the intent open and report it; you can settle it later. Only `writeoff --reason` when you give up, and leave the suspense for a human. Don't `cancel`: that is only for giving up before acting |
| Several tasks share one check (e.g. the same test report) | Give each task's output and state transition its own intent; run the shared check last as a separate intent with no `then`. Don't tie a task's `done` to a check that other tasks can fail |
| During crash recovery, settle shows the effect definitely didn't happen | You may redo and settle again only if probes (not claims) prove nothing happened and the action only touches local files. Never redo actions with external effects (messages, payments, API calls) |
| A sub-agent crashed or vanished and its budget isn't back to zero | Don't refund the balance on its behalf — there may be unreported spend. Report the gap for a human to check against the vendor bill (`reconcile --snapshot <bill> --since-close`) |
| A human has verified a claim (e.g. checked the outbox) | Whoever holds the observer key (a human or the harness) runs `intent observe <id> --metric <name> --value <v> --source observer --evidence '<how it was checked>'`, then settle. Never do this yourself |
| A diagnostic command will write files | Read-only investigation needs no intent. If it writes files, keep away from paths probed by open intents, and say what it wrote in your report |
| Mentioning times in a report | Record `ts` values carry a UTC offset and `intent list` shows local time. Don't present a `+00:00` time as local time |

## Red flags — stop immediately

- "Mark the state done now, settle later" → state transitions are protected; they can only be posted via `--then`.
- "Settle mismatched, but the tool returned success" → the probe wins. Tool return values lie too.
- "Reconciliation is off by 1, post an entry to even it out" → use `reconcile --adjust` to book each difference into suspense, find the cause, then `clear-suspense --reason`. Both steps land on the human review list.
- "The trial balance passed, so memory is correct" → re-read the core principle.
- "Work around it with `--direct`", "`--allow-noop`", "tweak the config", "write off then clear suspense" → all leave a trail, and `verify --strict` lists them for human review. They are themselves things you must report.
- An intent was written wrong (e.g. missing `then`) → don't reopen it under a new id to get around it; report it as an item for a human to decide.
- Hand-editing files under `.ledger/` → the hash chain breaks and the ledger refuses further entries. Corrections are made only with `reverse` (reversing entry).

## Account conventions

`state:*` are state pools (protected). Only "creation" may be posted directly (protected accounts only increase, and every counter-account is a source account); transitions and destruction must be verified via `then`.
`budget:*` and `expense:*` track resources. `world:*` and `equity:*` are source accounts used as the counter-account when creating entities or injecting budget, e.g. `'world:created -1' 'state:todo 1'`.
`clearing:*`, `intent:*`, `suspense:*` and the `delta` unit are system accounts, writable only by dedicated commands.

## References

- `references/integration.md`: **for harness builders**. Integration (Python API, hooks, signed observations, expectation templates, OpenSpec/SDD, multi-agent budgets, config). *(Chinese)*
- `references/design.md`: accounting concept mapping, what each check can and cannot catch, known limitations. *(Chinese)*
- `examples/demo.sh`: end-to-end demo; `python3 scripts/ledger.py <cmd> --help` for per-command options.
- `SKILL.zh-CN.md`: the original Chinese version of these rules.
