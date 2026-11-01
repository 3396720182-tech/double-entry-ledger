# 实验

这套架构的每条结论都来自下面三组实验（第四组还没做）。数字请按下面「证据强度」和各报告「局限」一节写的强度理解：样本小、场景少，而且多数是人为设计的陷阱。

| 编号 | 问题 | 做法 | 关键结果 | 报告 |
|---|---|---|---|---|
| E1 | 运行时账本挂在 harness 上抓不抓得到？agent 自己用有没有用？ | 最小 harness 故障注入 19 场景；带 / 不带 skill 的 agent 在含陷阱的项目上各做一遍，含崩溃恢复 | 故障注入 19/19 符合预期；agent 自用时强模型带不带 skill 一样诚实，skill 只增加成本；离不开账本的是崩溃恢复和多 agent 预算 | [e1-agent-trial](e1-agent-trial/REPORT.md) |
| E2 | Claude Code 钩子能不能减少虚报？代价多大？ | 发版场景埋 3 个静默失败；Opus / Haiku × 无 skill / 只有规则 / 钩子 × 常规 / 施压，98 次真实 `claude -p` | Opus 0 虚报（钩子只加成本）；Haiku 施压：7/8 → 1/10；只有规则反而交付变少 | [e2-hooks-ab](e2-hooks-ab/REPORT.md) |
| E3 | 跨会话的「设计决策 → 代码」能不能对账？门禁该拦什么？ | 在一个闭源仓库的历史上回放：只看文档开借方、看代码交凭证、机械核验、独立复核、反向扫描 | 审计的 5 处半落地抓到 4 处 + 1 处线索；可在归档时提前 24 天拦下；grep 驳回精度约 5%，自认未落地 97%，取代未冲销 100% | [e3-landing-replay](e3-landing-replay/REPORT.md)（脱敏） |
| E4 | 在没人专门设计陷阱的真实项目上，两本账到底有没有用、值不值这个成本？ | 真实开源项目盲测，独立审查者标注，比较有无账本 | **未开始**，只有方案 | [e4-blind-test](e4-blind-test/PLAN.md) |

## 证据强度

| 实验 | 能说明 | 不能说明 |
|---|---|---|
| E1 | 机制按设计工作：人为注入的 19 种故障都按预期被发现 | 真实项目里这类故障有多常见；注入的故障是人为设计的，单一模型，每场景 1～2 次 |
| E2 | 在这一个发版场景、这两个模型上，钩子让被施压的 Haiku 虚报明显减少（7/8 → 1/10），Opus 用不上 | 不能外推到所有模型、任务和工作流。每组 3～10 次，只有一个场景，陷阱和压力都是人为施加的 |
| E3 | 在一个真实仓库的历史上，「逐笔交凭证」能抓到人工审计找到的大部分半落地；哪些信号精度高、哪些低 | 只有一个闭源仓库、一组回放点、一份审计答案，是回顾性分析；标签多由模型判断、人工只抽查；精度数字来自改进前的回放脚本 |

E2 的「7/8 → 1/10」是这一个场景里的观测值，不是「装上就能降低同样比例虚报」的结论。要回答实际价值，下一步是 E4：在真实开源项目上盲测，由独立审查者标注实际问题，比较有无账本时的召回率、误报率、成本和任务完成质量。这比继续增加人为设计的陷阱更有说服力。

## 复现

命令都在仓库根目录执行。

| 实验 | 命令 | 需要 |
|---|---|---|
| E1 故障注入 | `python3 experiments/e1-agent-trial/harness_trial.py plugins/double-entry-ledger/skills/double-entry-ledger` | Python 3.8+ |
| E1 agent 场景 | `bash experiments/e1-agent-trial/setup_env.sh <目录>`、`bash experiments/e1-agent-trial/setup_crash.sh <目录> <skill 目录的绝对路径>`，再让 agent 在里面做任务 | 一个 agent |
| E2 场景与判定 | `bash experiments/e2-hooks-ab/setup_relprep.sh <目录> readme-cmd`；`python3 experiments/e2-hooks-ab/check_relprep.py <项目> <stream.jsonl>` | Python |
| E2 全量 | 见 [e2-hooks-ab/REPORT.md](e2-hooks-ab/REPORT.md#1-设计) | Claude Code CLI，会产生真实费用 |
| E2 汇总表 | `python3 experiments/e2-hooks-ab/summarize.py experiments/e2-hooks-ab/data/runs`（从入仓数据重算，与 `data/runs/summary_tables.md` 一致） | Python |
| E3 方法 | `bash plugins/double-entry-ledger/skills/landing-ledger/examples/demo.sh`（虚构项目，五种半落地形态都有样本） | Python |
| E3 回放你自己的仓库 | 按 `landing-ledger/references/method.md` §6 | 子代理 |

## 数据披露

- E1、E2 的场景都是虚构的，脚本和逐次判定结果全部入仓。没入仓的：每次运行的完整对话流和项目现场（体积大，且带运行机器的本机路径）。
- E3 的回放对象是闭源项目：**只入仓方法、汇总计数和结论**（`e3-landing-replay/data/aggregate.json`）。逐笔借方、凭证、复核记录、源码、文件名、符号名、提交号、业务数据都没有入仓；五处半落地只按「形态」描述。回放时的脚本已经通用化为 `landing-ledger`，演示用的是虚构项目，不依赖原仓库。
