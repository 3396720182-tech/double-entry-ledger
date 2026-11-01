# 落地对账（设计决策 → 代码）

> 本文件是 SKILL.md 的中文版，内容一致；Claude Code 加载的是 SKILL.md。

## 核心原则

**真正起作用的是「逐笔交凭证」这个结构，不是用 grep 抓谎。** 设计文档里每条代码层承诺开一笔借方；实现方必须逐笔回应：定义在哪、生产代码里哪一行让它成立、哪个测试覆盖它——或者承认没落地，或者指出取代它的那条决策。代码只核对凭证的形式，语义由复核员判断，核验器从不判断语义。

在一个真实闭源仓库上的历史回放（本仓库 `experiments/e3-landing-replay`）实测：实现方关于自己的信号很准（自认未落地 97%、已取代但原文没冲销 100%），而 grep 机械驳回只有约 5% 是对的。所以**门禁只拦高精度信号，机械驳回进复核清单**。

永远三态：过 / 不过 / **没查**。没跑的检查不能报成通过。

## 角色（必须分开）

| 角色 | 能看到 | 产出 |
|---|---|---|
| 规划会话 | 只有设计文档，看不到代码 | 借方，`references/prompts/planner.md` |
| 实现会话 | 代码（快照里的文档也能看） | 每笔代码层借方一张凭证，`references/prompts/voucher.md` |
| 核验器 | — | `scripts/landing.py reconcile`（纯代码，不用模型） |
| 复核员 | 代码 | 复核清单的裁定 `references/prompts/adjudicator.md`；已入账抽样 `references/prompts/fn-checker.md` |
| 人 | 门禁输出 | 签收复核项，决定阻断项怎么办 |

规划会话不能看代码，否则借方会照着已有的代码去写。运行这个 skill 的你负责编排：把各会话作为独立子代理派出去，跑核验器，汇报。不要在同一个上下文里既写借方又写凭证。

## 步骤

```bash
LD() { python3 "${CLAUDE_SKILL_DIR}/scripts/landing.py" "$@"; }   # 每个新 shell 都要重新定义
# 0.（仅历史回放）只读导出快照，不在用户的工作区 checkout
bash "${CLAUDE_SKILL_DIR}/scripts/snapshot.sh" <仓库> <提交> <快照>/docs docs openspec
bash "${CLAUDE_SKILL_DIR}/scripts/snapshot.sh" <仓库> <提交> <快照>/code
# 1. 规划子代理 → debits/*.json，然后
LD lint --debits debits/*.json                    # 先把「接线约束过弱」的提示都处理掉
# 2. 实现子代理 → vouchers/*.json，然后
LD lint --debits debits/*.json --vouchers vouchers/*.json
# 3. 核验 + 门禁；告诉它本期测试是否真跑过
LD reconcile <代码根> --debits debits/*.json --vouchers vouchers/*.json --tests not-run --json result.json
# 4. 反向扫描：有定义、生产零调用（没人写借方的承诺）
LD orphans <代码根> --json orphans.json
# 5. 复核子代理看复核清单（REJECTED、CREDITED_NO_TEST），另随机抽一批 CREDITED 查漏报
```

退出码：`0` 通过，`1` 阻断，`3` 不阻断但有待复核或没查的项，`2` 用法错误 / 输入格式有误 / 内部错误（永远不是门禁结论）。端到端演示：`bash examples/demo.sh`。

## 怎么读结果

| 结论 | 门禁 | 含义 |
|---|---|---|
| `NO_VOUCHER`、`BAD_VOUCHER` | 阻断 | 借方没人回应，或凭证无效（状态不明、取代依据不存在） |
| `UNLANDED` | 阻断 | 实现方自认未落地 |
| `DEVIATION` | 阻断 | 声称已落地，但凭证里列了和文档的偏差 |
| `SUPERSEDED_UNRECORDED` | 阻断 | 决策在别处改了，来源文档仍要求旧做法：要么冲销原文，要么落地 |
| `REJECTED` | 复核 | 凭证没通过机械核验。多数是核验器误报（规则太死、文档名和代码名漂移），偶尔是真缺口的唯一线索 |
| `CREDITED_NO_TEST` | 复核 | 接上了，但没有测试引用它 |
| `UNANCHORED` | 复核 | 借方什么可核对的名字都没写（symbols / call / literals 全空），只能看凭证自述 |
| `CREDITED`、`SUPERSEDED_RECORDED` | 通过 | |

阻断项旁边的「⚠ 文档自称已完成」表示文档自己写着已完成（`[x]`、「已上线」），这类最该汇报。
反向扫描里「有实现、有测试、生产零调用」那一组是半落地的典型样子（比如签名校验函数有实现、有单测，webhook 入口从没调用）。没有借方覆盖也要汇报。

## 给你的规则

1. **门禁结论照工具输出汇报。** 退出码 3 不是「全部通过」，要说清哪些待复核、哪些没查（测试没跑、流程类借方不在范围）。
2. **不要自己把 REJECTED 改判成阻断或通过。** 它进复核；如果你复核过，就说明并给出 文件:行号 证据。
3. **不要为了让凭证通过去改借方**，也不要让实现会话改借方。借方写错了，报告出来，由规划方或人带着理由改。
4. **「X 必须在入口 Y 生效」这类决定，借方必须带接线约束**（`scope_hint` / `must_include`）。只有函数名的借方，任何一处调用都能满足它。
5. **自述偏差写进 `deviations`**，不能只写在 `note` 里，这样它才会进门禁。
6. **被审计的仓库只读。** 快照用 `snapshot.sh`（`git archive`）；不在用户的工作区 checkout、stash、建分支或 worktree。
7. 仓库不是你能公开的，借方、凭证、复核输出就不要放到任何公开的地方，它们引用了内部路径和代码。

## 适用范围

- 有用：跨会话的 spec → 代码（OpenSpec/SDD 归档门禁）、子代理或较弱模型的产出、无人值守任务、审计「这条 ADR 真的做完了吗」。
- 用处不大：强模型在单个会话里自查短任务，它本来就会回头核对；成本是多出来的几个会话。
- 盲区：从没写进设计文档的承诺（靠 `orphans` 补）、分支级死代码、运行时事实（用生产数据里的计数对账，如某个操作执行了多少次 vs 留下了多少条记录，作为独立的一项）。

## 参考

- `references/schema.md`：借方/凭证格式、判定规则、配置。
- `references/method.md`：方法、门禁策略及其数据依据、OpenSpec 接入、历史回放、局限。
- `references/prompts/`：规划、实现、复核、漏报抽查四个提示词模板。
- `examples/demo.sh` + `examples/demo/`：一个虚构服务，五种半落地形态都在里面。
