---
name: landing-ledger
description: Use when checking whether design decisions actually landed in code across sessions — before archiving an OpenSpec/SDD change, when sub-agents, weaker models or unattended runs say a feature is done, or when auditing for half-landed work (implemented and unit-tested but never called in production, a decision changed while the ADR/spec still demands the old behaviour, "[x]" in tasks.md with no wiring). Turns ADRs, specs and task lists into debits, makes the implementer submit a voucher per debit, checks the vouchers mechanically, and gates only on high-precision signals (落地对账 / 半落地 / 归档门禁 / spec 落地核对 / 决策没冲销). Not for checking individual tool calls inside one session (use double-entry-ledger) and not for accounting software.
---

# Landing Ledger (design decision → code)

## Core principle

**What works is the structure "one voucher per debit", not grep catching liars.** Every code-level commitment in the design docs becomes a debit; the implementing side must answer each one with a voucher (where it is defined, where production code makes it true, which test covers it) — or admit it did not land, or point to the decision that superseded it. Code checks the form of each voucher. Semantics are judged by a reviewer, never by the checker.

Measured on a replay of a real closed-source repo (`experiments/e3-landing-replay` in this repo): the signals the implementer gives about itself are precise (admitted unlanded 97 %, superseded-but-not-reversed 100 %), while mechanical rejection by grep was right only ~5 % of the time. So **gate only on the high-precision signals and send mechanical rejections to a review list.**

Three states, always: passed / failed / **not checked**. A check that did not run is never reported as passed.

## Roles (keep them separate)

| Role | Sees | Produces |
|---|---|---|
| Planner session | design docs only (no code) | debits — `references/prompts/planner.md` |
| Implementer session | code (+ docs in the snapshot) | one voucher per code-level debit — `references/prompts/voucher.md` |
| Checker | — | `scripts/landing.py reconcile` (code, no model) |
| Reviewer | code | verdicts on the review list — `references/prompts/adjudicator.md`, sample of credited items — `references/prompts/fn-checker.md` |
| Human | the gate output | signs off review items, decides on blocks |

The planner must not see code, otherwise debits get written to match what exists. You (the agent running this skill) orchestrate: dispatch the sessions as separate sub-agents, run the checker, and report. Do not write debits and vouchers in the same context.

## Procedure

```bash
LD() { python3 "${CLAUDE_SKILL_DIR}/scripts/landing.py" "$@"; }   # redefine in every new shell
# 0. (history replay only) read-only snapshots; never check out in the user's working tree
bash "${CLAUDE_SKILL_DIR}/scripts/snapshot.sh" <repo> <commit> <snap>/docs docs openspec
bash "${CLAUDE_SKILL_DIR}/scripts/snapshot.sh" <repo> <commit> <snap>/code
# 1. planner sub-agents → debits/*.json, then
LD lint --debits debits/*.json                    # fix every "wiring constraint too weak" warning before going on
# 2. implementer sub-agents → vouchers/*.json, then
LD lint --debits debits/*.json --vouchers vouchers/*.json
# 3. check + gate; say whether tests really ran this period
LD reconcile <code-root> --debits debits/*.json --vouchers vouchers/*.json --tests not-run --json result.json
# 4. reverse scan: things defined but never called in production (debits nobody wrote)
LD orphans <code-root> --json orphans.json
# 5. reviewer sub-agents on the review list (REJECTED, CREDITED_NO_TEST) + a random sample of CREDITED
```

Exit codes: `0` pass, `1` block, `3` not blocked but has review items or unchecked items, `2` usage error / malformed input / internal error (never a gate verdict). Try it end to end with `bash examples/demo.sh`.

## Reading the result

| Verdict | Gate | Meaning |
|---|---|---|
| `NO_VOUCHER`, `BAD_VOUCHER` | block | debit unanswered, or the voucher is invalid (unknown status, superseding reference doesn't exist) |
| `UNLANDED` | block | implementer admits it did not land |
| `DEVIATION` | block | claimed landed, but the voucher lists deviations from the doc |
| `SUPERSEDED_UNRECORDED` | block | the decision was changed elsewhere and the source doc still demands the old behaviour — reverse the doc or land it |
| `REJECTED` | review | voucher failed the mechanical check — mostly checker false positives (rules too literal, names drifted between doc and code); occasionally the only clue to a real gap |
| `CREDITED_NO_TEST` | review | wired, no test references it |
| `UNANCHORED` | review | the debit names nothing checkable (no symbols / call / literals) — only the voucher's own word |
| `CREDITED`, `SUPERSEDED_RECORDED` | pass | |

"⚠ 文档自称已完成" next to a blocked item means the doc itself says done (`[x]`, "shipped") — the most important kind to report.
Orphans in the "tested but never called in production" group are the classic half-landing shape (e.g. a signature-verification function with an implementation and a unit test that no webhook handler ever calls); report them even though no debit covers them.

## Rules for you

1. **Report the gate verdict as the tool printed it.** `3` is not "all passed"; say what is under review and what was not checked (tests not run, out-of-scope process debits).
2. **Never turn a REJECTED into a block or a pass yourself.** It goes to review; if you reviewed it, say so and give file:line evidence.
3. **Don't edit debits to make vouchers pass**, and don't let the implementer session edit debits. A wrong debit is reported and fixed by the planner/human, with the reason.
4. **Debits must carry wiring constraints** (`scope_hint` / `must_include`) for every "X must happen at entry Y" decision; a bare function name can be satisfied by any call anywhere.
5. **Self-reported deviations go into `deviations`**, not only `note` — that is what makes them gate.
6. **Read-only on the audited repo.** Snapshots via `snapshot.sh` (`git archive`); no checkout, stash, branch or worktree in the user's tree.
7. When the repo is not yours to publish, keep debits, vouchers and review outputs out of any public place; they quote internal paths and code.

## Where it helps (and where it doesn't)

- Helps: cross-session spec → code (OpenSpec/SDD archive gate), output of sub-agents or weaker models, unattended tasks, audits of "is this ADR really done".
- Doesn't add much: a strong model in a single session reviewing its own short task — it already checks itself; the cost is the extra sessions.
- Blind spots: commitments that never made it into a design doc (use `orphans`), branch-level dead code, runtime facts (compare counts in production data — e.g. how many times an operation ran vs. how many records it left — as a separate reconciliation).

## References

- `references/schema.md` — debit/voucher format, verdict rules, config. *(Chinese)*
- `references/method.md` — method, gate policy and the data behind it, OpenSpec integration, history replay, limits. *(Chinese)*
- `references/prompts/` — planner, voucher, adjudicator, fn-checker prompt templates. *(Chinese)*
- `examples/demo.sh` + `examples/demo/` — a fictional service with all five half-landing shapes.
- `SKILL.zh-CN.md` — Chinese version of this file.
