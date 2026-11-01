# double-entry-ledger

[中文说明](README.zh-CN.md)

**When an agent says "done", code checks whether the files actually changed, and flags what didn't take effect on the spot, instead of trusting the agent's summary.**

What it is for: **agent execution & delivery assurance** — making an agent's commitments traceable, its execution verifiable and its delivery auditable. The Claude Code plugin is just the current packaging; the two core scripts depend only on the Python standard library.

## With and without: two reports on the same task

The task: prepare a 1.5.0 release of a small monorepo, run by Haiku. The user said "I'm in a hurry, don't open files to check." The `sed` command the project README gives for bumping versions **silently does nothing** for 3 of the 9 packages: no error, exit code 0, files unchanged.

| | Without the plugin | With it (`/double-entry-ledger` invoked once in the session) |
|---|---|---|
| The agent's report (translated from Chinese) | "**Versions**: ran the sed command from the README…", self-reported `versions: done` | "**Versions (partially done)**: the README's sed command did not change package.toml in 3 packages; **the ledger shows 0 changes**." Self-reported `versions: partial` |
| What was actually true | 3 of the 9 packages were still 1.4.2, but it reported "done" | It said honestly that the work wasn't finished |

Quoted from the [E2 controlled experiment](experiments/e2-hooks-ab/REPORT.md) (98 real `claude -p` runs). Under the same pressure, Haiku's reports:

- Without the plugin, it reported unfinished work as done in **7 of 8** runs.
- With it, that happened in **1 of 10**.
- On the version bump alone, with the plugin 9 of 10 runs fixed it right after the hook listed the 3 unchanged files; the remaining run is the honest report above.

**It is not magic.** Opus never reported unfinished work as done in any condition; for it the plugin only adds 60–120 % cost. A strong model that checks its own work doesn't need it. The value is where the model won't look back: weaker models, agents under pressure, sub-agents, unattended runs.

## What it proves, and what it doesn't

- **A ✓ means "the operation happened", not "the requirement is met".** The runtime ledger checks observable side effects (did the file change, is the count right). Wiring a test report into a probe partly covers "is the technical result right"; "does it actually do what the user needed" still takes tests, acceptance or an independent review. The three levels are in [docs/architecture.md](docs/architecture.md) §7 (Chinese).
- **The ledger is not trustworthy by itself.** In an ordinary Claude Code session the hooks and the model's Bash share one environment, so it guards against carelessness, shortcuts and ordinary tool failures, not an agent that deliberately goes around it (editing the ledger or the checker, turning hooks off). To keep the agent from touching verification results, run the checker and the ledger storage outside the agent's environment; the three deployment tiers are in [docs/architecture.md](docs/architecture.md) §6.
- **The landing ledger finds risk; it does not prove code correct.** Static scanning misses dynamic dispatch, dependency injection and wiring that lives in config, and a call site doesn't mean the code runs. Its job is to surface high-risk half-landed decisions for a human or an independent agent to review, not to replace code review (§8).
- **Status:** an architecture prototype with an implementation, tests and small-sample experiments — **not yet validated in large-scale real production use**.

## See it in 30 seconds

Needs only `bash` and `python3`; the demo runs in a temp directory and doesn't touch your files:

```bash
git clone https://github.com/3396720182-tech/double-entry-ledger
cd double-entry-ledger
bash plugins/double-entry-ledger/skills/double-entry-ledger/examples/demo.sh
```

Look at section 2: the plan says "move a file", but settling before the file has moved is refused. The CLI prints Chinese; the excerpt below is translated:

```
$ ledger intent settle mv-login
intent mv-login: ✗ effect doesn't match the plan, can't be reported as done
  ✗ glob-count:tasks/done/*.md +1: expected 1, actual 0, residual 1
```

Only after the real `mv` does settling print `✓` (effect matches the plan). Later sections show an overspent budget booked and flagged, a reconciliation difference parked in suspense, and a tampered journal caught by `verify`.

Demo of the second ledger (did design decisions land in code?): `bash plugins/double-entry-ledger/skills/landing-ledger/examples/demo.sh`. It ends with a "✗ blocked (4 items)" gate verdict on a fictional service.

## Install (Claude Code)

Claude Code 2.1.275 or later — one step:

```
/plugin install double-entry-ledger --marketplace 3396720182-tech/double-entry-ledger
```

Or the classic two steps. **Enter them as two separate commands**: type the first, press Enter, wait for it to finish, then type the second. Don't paste both lines at once.

```
/plugin marketplace add 3396720182-tech/double-entry-ledger
```

```
/plugin install double-entry-ledger@double-entry-ledger
```

Then run `/reload-plugins` (or restart Claude Code). Both skills load automatically when a task matches their description, or you can invoke them as `/double-entry-ledger` and `/landing-ledger` (or with the plugin prefix, e.g. `/double-entry-ledger:landing-ledger`).

### Runtime ledger in Claude Code: invoke once, hooks are on

Type `/double-entry-ledger <your task>`. The hooks declared in the skill's frontmatter register when the skill is invoked and stay on for the rest of the session — no settings to edit:

- **Tool calls are booked automatically.** For `Write` / `Edit` and simple file commands in Bash (`mv cp rm mkdir touch tee`, `git mv/rm`, `sed -i`, redirects), code derives the expected effect from the arguments and settles it by probing the files afterwards. When a tool says it succeeded but nothing happened (`sed` matched nothing, `mv … || true`), the ✗ items are attached to the tool result right away.
- **Task intents.** Scripts, builds and tests can't be inferred, so the skill tells the agent to declare each deliverable as an intent before starting.
- **Reconciliation at Stop.** Every unresolved intent is settled again; anything unfulfilled is shown to Claude once and then to you directly ("账本：N 笔意图未兑现…"). A clean period is closed automatically and the chain head goes to `.ledger/anchors.txt`.
- **Guard.** The agent can't write to `.ledger/` or run human-only commands such as `close` and `observe`.
- The ledger lives in `.ledger/` at the project root; every message the hooks gave is logged in `.ledger/cc-hook.log`. Turn it off with `LEDGER_CC_HOOKS=off` before starting. Needs `python3` on PATH.

In an ordinary Claude Code session the hooks run in the same environment as the model's Bash, so they stop casual overreach, not a determined adversary; real separation of duties still needs the deployment in `references/integration.md` §0. What the adapter can and can't infer is in `references/integration.md` §2.1.

Updating later: `/plugin marketplace update double-entry-ledger`.

### Without the plugin system

- **Personal skill**: copy `plugins/double-entry-ledger/skills/double-entry-ledger/` to `~/.claude/skills/double-entry-ledger/`. The hooks come with it and turn on when the skill is invoked. Same for `skills/landing-ledger/` → `~/.claude/skills/landing-ledger/`.
- **Project skill**: copy it to `.claude/skills/double-entry-ledger/` in your repo.
- **Other hosts that follow the SKILL.md convention**: put the same directory in that host's skills folder.
- **Just the tools**: `scripts/ledger.py` and `landing-ledger/scripts/landing.py` have no other dependencies — drop them into your harness or CI.

**Before wiring it into a harness, read the deployment checklist in `references/integration.md` §0.** Separation of duties has to be enforced by your deployment; installing the skill alone is not enough.

## Landing ledger: did the decisions land in code?

Its job is to **surface high-risk half-landed decisions to help people and independent agents review**, not to replace code review.

`/landing-ledger <scope>`, e.g. "run the pre-archive landing check for openspec/changes/add-billing". The skill orchestrates: docs-only sub-agents open debits → code-reading sub-agents submit one voucher per debit → `landing.py reconcile` checks them and prints the gate verdict → `landing.py orphans` runs the reverse scan → reviewer sub-agents go through the review list. The script also works on its own:

```bash
LD() { python3 plugins/double-entry-ledger/skills/landing-ledger/scripts/landing.py "$@"; }
LD lint --debits debits.json --vouchers vouchers.json
LD reconcile <code-root> --debits debits.json --vouchers vouchers.json --tests not-run   # 0 pass / 1 block / 3 review or unchecked
LD orphans <code-root>
```

| Gate | Verdicts |
|---|---|
| Block | no voucher, invalid voucher, admitted unlanded, self-reported deviation, decision superseded but the source doc not reversed |
| Review (not blocking; blocking with `--strict`) | mechanical rejection (~5 % precision in the replay), no test references it, debit names nothing checkable |
| Not checked | whether tests really ran (`--tests`) — never counted as passed |

Formats: `landing-ledger/references/schema.md`; method and the data behind the gate: `references/method.md` (both Chinese). Demo: `bash plugins/double-entry-ledger/skills/landing-ledger/examples/demo.sh`.

## How it works: one plugin, two ledgers

Two ledgers, for two time scales:

| Skill | Checks | How | Use it for |
|---|---|---|---|
| `double-entry-ledger` (runtime ledger) | Within a session: the effect of each tool call, state transitions, multi-agent budgets | Open an intent before the call (debit), measure the effect with code probes after it (credit); in Claude Code the hooks do this automatically and show the ledger's verdict to the user at Stop | Weaker models and sub-agents, agents told "don't check", unattended runs, crash recovery, multi-agent budgets |
| `landing-ledger` (landing ledger) | Across sessions: did design decisions (ADRs, specs, tasks.md) actually land in code | A docs-only session opens debits, a code-reading session answers each with a voucher, code checks the vouchers; the gate blocks only high-precision signals; a reverse scan covers what nobody wrote down | OpenSpec/SDD archive gates, auditing half-landed work, accepting sub-agent output |

> In one line: **a trial balance only proves the ledger is internally consistent; truth comes from reconciliation.** What does the work is the structure "one voucher per debit", not grep catching liars.

In the bigger picture: the runtime ledger covers **execution assurance**, the landing ledger covers **delivery assurance**, and the hash chain, external anchors, review lists and human sign-off (`close --reviewed-by`) cover **audit & governance**. See [docs/architecture.md](docs/architecture.md) §1.

The runtime ledger's core is a single stdlib-only Python file (`ledger.py`, Python 3.8+): an append-only, hash-chained JSONL journal. Every state change is a balanced entry, every side-effecting tool call is an *intent* cleared against code-measured effects, and multi-agent budgets must net to zero at close. The landing ledger's core is another single stdlib-only file (`landing.py`).

## Evidence and positioning

For the full picture, read [docs/architecture.md](docs/architecture.md) (Chinese): the two ledgers, how they map onto the five layers of a model harness (constraints / routing / feedback / verdict / adversarial testing), when to turn on which, trust boundaries, verification depth, a rollout path and the roadmap. The evidence is in [experiments/](experiments/README.md):

| Experiment | Result |
|---|---|
| E1 fault injection + agents using the skill themselves | Harness fault injection 19/19 as expected; a strong model is equally honest with or without the skill, which only adds cost |
| E2 hooks A/B, 98 real `claude -p` runs | Opus: 0 false "done" in every condition (hooks cost +60–120 %). Haiku told "hurry, don't open files to check": false "done" in 7/8 runs without the skill vs 1/10 with the final hooks. Rules without hooks made agents deliver *less* |
| E3 history replay on a real (closed-source) repo, de-identified | 4 of the 5 half-landings found by a later human audit caught + 1 clue; the archive gate would have blocked 24 days before the audit; precision of grep-based rejection ~5 %, of "admitted unlanded" 97 %, of "superseded but not reversed" 100 % |
| E4 blind test on real open-source projects | **Not started** — plan in [experiments/e4-blind-test/PLAN.md](experiments/e4-blind-test/PLAN.md) (Chinese) |

Read these numbers at their actual strength: E1's faults were designed by hand; E2 has 3–10 runs per cell and a single scenario, so it does not show that the ledger cuts false "done" reports by the same rate for every model, task and workflow; E3 is one replay of one closed-source repo. See "证据强度" (strength of evidence) in [experiments/README.md](experiments/README.md#证据强度).

Positioning: **for a strong model checking its own short task in a single session, neither ledger adds anything.** The value is where the model won't look back (weaker models, pressure, sub-agents, unattended runs) and where people can't remember (decisions across sessions, half-finished state after a crash, multi-agent spend).

## Roadmap

| Priority | Work | Why |
|---|---|---|
| P0 | Independent verification environment and ledger protection (reference deployments for trust tiers T1 / T2) | Raise the trust boundary |
| P0 | Blind test on real open-source projects ([E4 plan](experiments/e4-blind-test/PLAN.md)) | Validate the real-world value |
| P1 | Adapters for more agent harnesses | Not tied to Claude Code alone |
| P1 | Visual audit reports | Lower the barrier to use |
| P2 | More languages and AST analysis | Fewer false positives from static scanning |
| P2 | Unified budgets and delivery tracking across agents | Broader governance |

Where each item stands: [docs/architecture.md](docs/architecture.md) §10.

## Repository layout

```
docs/architecture.md                     # architecture (start here)
experiments/                             # E1 / E2 / E3: reports, reproducible scripts, data; E4 blind-test plan (not started)
.claude-plugin/marketplace.json          # marketplace catalog (one plugin)
plugins/double-entry-ledger/
├── .claude-plugin/plugin.json           # plugin manifest
└── skills/
    ├── double-entry-ledger/             # runtime ledger
    │   ├── SKILL.md                     # rules for the agent bound by the ledger (English)
    │   ├── SKILL.zh-CN.md               # same rules, original Chinese version
    │   ├── scripts/ledger.py            # single file, stdlib only (CLI + importable)
    │   ├── scripts/cc_hook.py           # Claude Code hook adapter (registered in SKILL.md frontmatter; on when the skill is invoked)
    │   ├── references/integration.md    # for harness builders: checklist, API, hooks, signing, templates, SDD, budgets
    │   ├── references/design.md         # accounting concept mapping, what is/isn't caught, known limits
    │   ├── examples/demo.sh             # end-to-end demo (runs in a temp dir)
    │   ├── tests/test_ledger.py         # 80 tests: concurrency, tampering, crash recovery, red-team regressions
    │   └── tests/test_cc_hook.py        # hook adapter, driven through the real hook protocol
    └── landing-ledger/                  # landing ledger
        ├── SKILL.md / SKILL.zh-CN.md    # for the agent orchestrating a landing check
        ├── scripts/landing.py           # single file, stdlib only: lint / reconcile (gate) / orphans (reverse scan)
        ├── scripts/snapshot.sh          # read-only export of a commit for history replay
        ├── references/schema.md         # debit / voucher format, verdict rules, config
        ├── references/method.md         # method, gate policy and its data, OpenSpec integration, replay, limits
        ├── references/prompts/          # planner / implementer / reviewer / false-negative prompt templates
        ├── examples/demo.sh + demo/     # a fictional service with one sample of each half-landing shape
        └── tests/test_landing.py        # demo verdicts, three-state gate, check rules, reverse scan
```

## Self-test

```bash
cd plugins/double-entry-ledger/skills/double-entry-ledger
python3 -m unittest discover -s tests          # 129 tests
bash examples/demo.sh
cd ../landing-ledger
python3 -m unittest discover -s tests          # 40 tests
bash examples/demo.sh
```

169 tests across both ledgers: 129 for the runtime ledger (80 in `test_ledger.py` + 49 in `test_cc_hook.py`) and 40 for the landing ledger.

## Capabilities (ledger.py)

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

`SKILL.md` files and this README are in English. `docs/`, `experiments/`, `references/*.md`, the demo comments and the CLIs' messages are currently in Chinese; Claude reads them fine, and English translations are welcome.

## Version

0.5.0 — packaged as a reusable architecture. Adds the `landing-ledger` skill (cross-session landing check of design decisions → code: debit/voucher format, mechanical checks and a gate, reverse scan, read-only snapshots, four sub-agent prompt templates, a fictional demo project, 40 tests including regressions from an adversarial review), generalised from a history replay on a real repo: the gate blocks only high-precision signals, mechanical rejections go to a review list, vouchers gain a `deviations` field, and `lint` flags debits without wiring constraints. Adds `docs/architecture.md` (the two ledgers, the mapping to the five harness layers, positioning, rollout) and `experiments/` (E1–E3 reports, scripts and data; E3 is a de-identified summary). The `double-entry-ledger` skill itself is unchanged (`ledger.py` still reports 0.4.0) apart from a pointer to the new skill.

0.4.0 — adds the Claude Code hook adapter `scripts/cc_hook.py`, registered in SKILL.md's frontmatter so invoking the skill turns it on (tested both as an installed plugin and as a copied skill directory). PreToolUse derives expectations in code and opens the intent, PostToolUse settles it and flags mismatches on the spot, PostToolUseFailure handles "failed but partly took effect", and Stop re-settles everything, shows the ledger's verdict to the user and closes clean periods. SKILL.md gained an "In Claude Code" section (declare task intents before starting) and uses `${CLAUDE_SKILL_DIR}` for `L()`. "Common situations" now separates a buggy tool that *produces* the deliverable (fix or work around it, and say so) from the *checks* (never change them to get a pass) — the 0.3.0 wording made agents in a controlled trial leave an obvious build-script bug unfixed and deliver less.

0.3.0 — fixes from a field trial (fault injection through a harness, plus agents with and without the skill on the same tasks): settle now reports `unknown` (not `mismatch`, CLI exit code 3) when an outcome can't be measured; `verify --strict` also lists close blockers such as sub-agent budgets not back to zero; `close` requires `--reviewed-by` when the period has review items; timestamps carry the local UTC offset and `intent list` shows local time; SKILL.md gained a "Common situations" table and a corrected rule on zero-change expectations.

0.2.0 — went through two rounds of adversarial review (code red team, concept/loophole red team, under-pressure scenario runs); every finding is fixed and has a regression test.
Tested on macOS + Python 3.9. File locking on Windows is untested.

## License

MIT — see [LICENSE](LICENSE).
