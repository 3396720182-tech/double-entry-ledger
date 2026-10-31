# double-entry-ledger

[中文说明](README.zh-CN.md)

Double-entry bookkeeping for LLM-agent harnesses, packaged as a Claude Code plugin. It makes three things **self-checking in code**, so errors are caught where they happen instead of relying on the model's judgment:

- memory and state changes
- tool calls: "planned" vs. "actually happened"
- token / budget allocation across multiple agents

> In one line: **a trial balance only proves the ledger is internally consistent; truth comes from reconciliation.** So beyond balanced entries there is intent clearing, external reconciliation, suspense accounts and period close.

The core is a single stdlib-only Python file (`ledger.py`, Python 3.8+): an append-only, hash-chained JSONL journal. Every state change is a balanced entry, every side-effecting tool call is an *intent* cleared against code-measured effects, and multi-agent budgets must net to zero at close.

## Install (Claude Code)

Claude Code 2.1.275 or later — one step:

```
/plugin install double-entry-ledger --marketplace yzx-labs/double-entry-ledger
```

Or the classic two steps:

```
/plugin marketplace add yzx-labs/double-entry-ledger
/plugin install double-entry-ledger@double-entry-ledger
```

Then run `/reload-plugins` (or restart Claude Code). The skill loads automatically when a task matches its description, or you can invoke it as `/double-entry-ledger:double-entry-ledger`.

Updating later: `/plugin marketplace update double-entry-ledger`.

### Without the plugin system

- **Personal skill**: copy `plugins/double-entry-ledger/skills/double-entry-ledger/` to `~/.claude/skills/double-entry-ledger/`.
- **Project skill**: copy it to `.claude/skills/double-entry-ledger/` in your repo.
- **Other hosts that follow the SKILL.md convention**: put the same directory in that host's skills folder.
- **Just the tool**: `scripts/ledger.py` has no other dependencies — drop it into your harness.

**Before wiring it into a harness, read the deployment checklist in `references/integration.md` §0.** Separation of duties has to be enforced by your deployment; installing the skill alone is not enough.

## Repository layout

```
.claude-plugin/marketplace.json          # marketplace catalog (one plugin)
plugins/double-entry-ledger/
├── .claude-plugin/plugin.json           # plugin manifest
└── skills/double-entry-ledger/
    ├── SKILL.md                         # rules for the agent bound by the ledger (English)
    ├── SKILL.zh-CN.md                   # same rules, original Chinese version
    ├── scripts/ledger.py                # single file, stdlib only (CLI + importable)
    ├── references/integration.md        # for harness builders: checklist, API, hooks, signing, templates, SDD, budgets
    ├── references/design.md             # accounting concept mapping, what is/isn't caught, known limits
    ├── examples/demo.sh                 # end-to-end demo (runs in a temp dir)
    └── tests/test_ledger.py             # 72 tests: concurrency, tampering, crash recovery, red-team regressions
```

## Self-test

```bash
cd plugins/double-entry-ledger/skills/double-entry-ledger
python3 -m unittest discover -s tests
bash examples/demo.sh
```

## Capabilities

| Capability | Command |
|---|---|
| Balanced posting (idempotent ids, account rules, protected accounts) | `post` / `transfer` |
| Reversing entries (history is never edited) | `reverse` |
| Intent → act → settle (effects measured by code probes; state updated only after verification; expectation templates) | `intent open / settle / observe / writeoff / cancel / list` |
| External reconciliation (directories, JSON, external snapshots, period activity); differences go to suspense | `reconcile [--snapshot] [--adjust] [--since-close]` |
| Suspense listing and clearing with a reason | `suspense` / `clear-suspense` |
| Period close (temporary accounts must be zero, red flags handled; snapshots balances and chain head hash; names the reviewer when there are review items) | `close [--reviewed-by NAME]` |
| Full verification (hash chain, balance, rules, close snapshots, external anchor, strict mode) | `verify [--anchor] [--strict]` |
| Trial balance / balances | `trial` / `balance` |

## Language note

`SKILL.md` and this README are in English. `references/*.md`, the demo comments and the CLI's messages are currently in Chinese; Claude reads them fine, and English translations are welcome.

## Version

0.3.0 — fixes from a field trial (fault injection through a harness, plus agents with and without the skill on the same tasks): settle now reports `unknown` (not `mismatch`, CLI exit code 3) when an outcome can't be measured; `verify --strict` also lists close blockers such as sub-agent budgets not back to zero; `close` requires `--reviewed-by` when the period has review items; timestamps carry the local UTC offset and `intent list` shows local time; SKILL.md gained a "Common situations" table and a corrected rule on zero-change expectations.

0.2.0 — went through two rounds of adversarial review (code red team, concept/loophole red team, under-pressure scenario runs); every finding is fixed and has a regression test.
Tested on macOS + Python 3.9. File locking on Windows is untested.

## License

MIT — see [LICENSE](LICENSE).
