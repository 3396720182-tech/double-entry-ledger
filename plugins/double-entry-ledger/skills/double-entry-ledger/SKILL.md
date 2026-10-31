---
name: double-entry-ledger
description: Use when building or operating an agent harness where memory/state writes, tool-call outcomes, or token/budget allocations can silently drift from reality — the model reports a plan as a finished result, memory says "done" but the file or record isn't there, a crashed run leaves unknown side effects, or a sub-agent disappears without reporting spend. Also for double-entry bookkeeping, trial balance, reconciliation, suspense accounts, period close, or self-checking invariants in agent memory or multi-agent budgets (复式记账 / 借贷平衡 / 试算平衡 / 对账 / 挂账 / 结账 / 不变量自校验).
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
   L() { python3 <this skill's directory>/scripts/ledger.py "$@"; }   # the harness may wrap this under another name
   L intent open mv-42 --tool mv \
     --expect 'exists:tasks/done/42.md =1' --expect 'exists:tasks/todo/42.md =0' \
     --then 'state:todo -1' --then 'state:done 1'
   # ... call the tool ...
   L intent settle mv-42      # only exit code 0 means it happened; only then are the `then` lines posted
   ```

2. **Expectations come from tool templates or the task spec — never invent ones that are "easy to pass".** When `expect_templates` is configured, fill parameters with `--tool X --arg k=v`; for tools whose template declares `then`, your own `then` is not accepted.
   An expectation whose expected change is zero (target already satisfied, `+0`) is rejected, because the check would be meaningless.
3. **Settle fails (exit 1) = it did not happen.** You may retry the action and settle again (only the delta is recorded). If you give up, run `intent writeoff --reason`; the residual moves to suspense. Then **report truthfully what succeeded and what didn't**, including any partial effects.
4. **Giving up before doing anything**: `intent cancel --reason`. Cancellation is refused unless code can prove that no effect occurred.
5. **Outcome unknown** (crash, a `claim` with no observation, and you can't check yourself): **leave the intent open**, report "unknown", and say who needs to verify what.
   Do not `observe` a value yourself, do not `cancel`, do not `writeoff` + `clear-suspense` to force a close, and do not re-run the action — it may send twice.
6. **Crash recovery**: `intent list --status unresolved`, then `settle` each one. The open record is a write-ahead log; use it to recover the outcome from the real world. **Do not re-run the action.**
7. **Before claiming "all done"**: `verify --strict` must pass.
   Open intents, uncleared suspense, red-flagged items, and any manual intervention in the period (`--direct`, write-offs, suspense clearing, reconciliation adjustments, overrides, unsigned observations) all make it fail.
   Manual interventions are reviewed by a human; a human running `close` is the sign-off. Until then you may only report "what was done + items awaiting review".
8. **Don't open a second intent touching the same path until the first is resolved** (it will be refused). Point probes at specific files (`exists:` / `sha256:`); avoid `glob-count` over shared directories.

| Expectation | Meaning |
|---|---|
| `exists:P =1` / `=0` | File exists / does not exist at settle time (`=N` is an absolute target) |
| `glob-count:G +N` / `-N` | Count increased / decreased by N relative to the baseline at open time |
| `lines:P` `bytes:P` | Line count, byte count (missing file counts as 0) |
| `json:P#a.b +N` / `=N` | A numeric field in JSON the tool wrote to disk, e.g. `json:report.json#failed =0` |
| `sha256:P changed` / `unchanged` | Whether content changed. A stale report can also satisfy a `json:` expectation, so pair it with `changed` |
| `claim:NAME =N` | An effect code can't observe. The value is written (signed) by the harness from the tool's return value — not asserted by you |

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
