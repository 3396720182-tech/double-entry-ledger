# double-entry-ledger

[English](README.md)

把会计里的复式记账用到智能体 harness 上：agent 说「做完了」，要拿得出凭证，由代码对账，而不是信它的总结。

一个插件，两本账，对应两种时间尺度：

| skill | 核对什么 | 怎么核对 | 什么时候用 |
|---|---|---|---|
| `double-entry-ledger`（运行时账本） | 会话内：每次工具调用的效果、状态迁移、多 agent 预算 | 调用前开意图（借），调用后探针测效果（贷）；Claude Code 钩子自动做，Stop 时把账面结论直接给用户看 | 弱模型或子 agent、被催着「别检查」、无人值守、崩溃恢复、多 agent 预算 |
| `landing-ledger`（落地账本） | 跨会话：设计决策（ADR / spec / tasks.md）有没有落到代码里 | 只看文档的会话开借方，看代码的会话逐笔交凭证，代码机械核验；门禁只拦高精度信号，反向扫描补盲区 | OpenSpec/SDD 归档门禁、审计「半落地」、验收子 agent 的产出 |

> 一句话：**试算平衡只证明账本内部一致；真相来自对账。** 真正起作用的是「逐笔交凭证」这个结构，不是用 grep 抓谎。

**先读这两份：**
- [docs/architecture.md](docs/architecture.md)：整体架构、和 harness 五层（约束 / 路由 / 反馈 / 判定 / 反测）的对应、什么时候开哪个、落地路线。
- [experiments/](experiments/README.md)：三组实验的报告、脚本和数据。要点：

| 实验 | 结果 |
|---|---|
| E1 故障注入 + agent 自用 | harness 故障注入 19/19 符合预期；强模型带不带 skill 一样诚实，skill 只增加成本 |
| E2 钩子对照，98 次真实运行 | Opus 所有条件 0 虚报（钩子 +60%～120% 成本）；Haiku 被要求「别检查」时虚报 7/8 → 1/10；只给规则不配钩子反而交付变少 |
| E3 真实仓库历史回放（脱敏） | 人工审计的 5 处半落地抓到 4 处 + 1 处线索；可在归档时提前 24 天拦下；grep 机械驳回精度约 5%，「自认未落地」97%，「已取代未冲销」100% |

定位：**对会自己回头核对的强模型、单会话短任务，两本账都没有增量**；价值在模型不会回头看的地方（弱模型、被催、子 agent、无人值守）和人记不住的地方（跨会话的决定、崩溃留下的半截状态、多 agent 的花费）。

## 仓库结构

```
docs/architecture.md                     # 架构说明（从这里开始读）
experiments/                             # E1 / E2 / E3：报告、可复现脚本、数据
.claude-plugin/marketplace.json          # 插件市场目录（只含这一个插件）
plugins/double-entry-ledger/
├── .claude-plugin/plugin.json           # 插件清单
└── skills/
    ├── double-entry-ledger/             # 运行时账本
    │   ├── SKILL.md                     # 给受约束的 agent 读：必须遵守的规则和红旗（英文）
    │   ├── SKILL.zh-CN.md               # 同上，中文原版
    │   ├── scripts/ledger.py            # 单文件，只依赖标准库，Python 3.8+（CLI + 可 import）
    │   ├── scripts/cc_hook.py           # Claude Code 钩子适配器（SKILL.md 前言注册，调用 skill 即生效）
    │   ├── references/integration.md    # 给搭建者读：部署检查表、API、钩子、签名、模板、SDD、预算
    │   ├── references/design.md         # 会计概念映射、能抓什么不能抓什么、已知局限
    │   ├── examples/demo.sh             # 端到端演示（在临时目录里运行）
    │   ├── tests/test_ledger.py         # 80 个用例：并发、篡改、崩溃恢复、两轮对抗性审查回归
    │   └── tests/test_cc_hook.py        # 钩子适配器：按真实钩子协议驱动，覆盖命令解析、结算、守卫、Stop
    └── landing-ledger/                  # 落地账本
        ├── SKILL.md / SKILL.zh-CN.md    # 给编排对账流程的 agent 读
        ├── scripts/landing.py           # 单文件，只依赖标准库：lint / reconcile（门禁）/ orphans（反向扫描）
        ├── scripts/snapshot.sh          # 只读导出某个提交的快照，用于历史回放
        ├── references/schema.md         # 借方、凭证格式，判定规则，配置
        ├── references/method.md         # 方法、门禁策略和数据依据、OpenSpec 接入、历史回放、局限
        ├── references/prompts/          # 规划 / 实现 / 复核 / 漏报抽查四个子代理提示词模板
        ├── examples/demo.sh + demo/     # 虚构的小服务，五种半落地形态各一个样本
        └── tests/test_landing.py        # 演示项目逐笔结论、门禁三态、核验规则、反向扫描
```

## 安装（Claude Code 插件）

Claude Code 2.1.275 及以上，一步装好：

```
/plugin install double-entry-ledger --marketplace 3396720182-tech/double-entry-ledger
```

或者传统的两步：

```
/plugin marketplace add 3396720182-tech/double-entry-ledger
/plugin install double-entry-ledger@double-entry-ledger
```

装完运行 `/reload-plugins`（或重启 Claude Code）。插件里的两个 skill 在任务符合描述时会自动加载，也可以手动用 `/double-entry-ledger`、`/landing-ledger`（或带插件前缀的 `/double-entry-ledger:landing-ledger`）调用。以后更新：`/plugin marketplace update double-entry-ledger`。

### 运行时账本在 Claude Code 里：调用一次就挂上钩子

在会话里输入 `/double-entry-ledger <你的任务>`。skill 前言里声明的钩子会在调用时注册，本会话之后一直生效，不需要改任何 settings：

- **工具调用自动记账**：`Write` / `Edit` 和 Bash 里的简单文件命令（`mv cp rm mkdir touch tee`、`git mv/rm`、`sed -i`、重定向），由代码从参数推出预期效果，调用后探测文件结算。工具说成功但实际没生效（`sed` 没匹配到、`mv … || true`），会当场把 ✗ 项贴在工具结果旁边。
- **任务意图**：脚本、构建、测试推不出效果，skill 要求 agent 开工前把每项交付登记成意图。
- **Stop 时对账**：重新结算全部未结意图；没兑现的先让 Claude 再看一次，再直接显示给你（「账本：N 笔意图未兑现……」）；账目干净就自动结账，链头哈希写进 `.ledger/anchors.txt`。
- **守卫**：拒绝 agent 改 `.ledger/`，拒绝它自己执行 `close`、`observe` 等应由人做的命令。
- 账本在项目根的 `.ledger/`，钩子给过的每条提示都记在 `.ledger/cc-hook.log`。关闭：启动前设 `LEDGER_CC_HOOKS=off`。需要 PATH 上有 `python3`。

普通 Claude Code 会话里，钩子和模型的 Bash 跑在同一个环境，它防的是「顺手」的越权，不是蓄意对抗；要真正的职务分离仍需按 `references/integration.md` §0 部署。适配器能推断什么、不能推断什么，见 `references/integration.md` §2.1。

### 不用插件系统

- **个人级 skill**：把 `plugins/double-entry-ledger/skills/double-entry-ledger/` 复制到 `~/.claude/skills/double-entry-ledger/`，钩子同样随 skill 调用生效；`skills/landing-ledger/` 同理，复制到 `~/.claude/skills/landing-ledger/`。
- **项目级 skill**：复制到项目里的 `.claude/skills/double-entry-ledger/`。
- **其他支持 SKILL.md 规范的宿主**：放到该宿主文档规定的 skills 目录。
- **只要工具**：`scripts/ledger.py`、`landing-ledger/scripts/landing.py` 都不依赖任何其他文件，直接复制进你的 harness 或 CI 就能用。

**接入前请读 `references/integration.md` §0 的部署检查表。** 职务分离要靠部署落实，只装 skill 是不够的。

## 落地对账：设计决策有没有落到代码里

`/landing-ledger <要对账的范围>`，例如「对 openspec/changes/add-billing 做归档前的落地对账」。skill 会编排：只看文档的子代理开借方 → 看代码的子代理逐笔交凭证 → `landing.py reconcile` 机械核验并给出门禁结论 → `landing.py orphans` 反向扫描 → 复核子代理看复核清单。也可以不经 skill 直接用脚本：

```bash
LD() { python3 plugins/double-entry-ledger/skills/landing-ledger/scripts/landing.py "$@"; }
LD lint --debits debits.json --vouchers vouchers.json
LD reconcile <代码根> --debits debits.json --vouchers vouchers.json --tests not-run   # 0 通过 / 1 阻断 / 3 有待复核或没查
LD orphans <代码根>
```

| 门禁 | 结论 |
|---|---|
| 阻断 | 没凭证、凭证无效、自认未落地、自述偏差、决策已改但来源文档没冲销 |
| 复核（不阻断，`--strict` 时阻断） | 机械驳回（回放实测精度约 5%）、没有测试引用、借方没写任何可核对的名字 |
| 没查 | 没告诉它测试是否真跑过（`--tests`）——不算通过 |

借方和凭证的格式见 `landing-ledger/references/schema.md`，方法和数据依据见 `references/method.md`，演示：`bash plugins/double-entry-ledger/skills/landing-ledger/examples/demo.sh`。

## 自检

```bash
cd plugins/double-entry-ledger/skills/double-entry-ledger
python3 -m unittest discover -s tests          # 129 个用例
bash examples/demo.sh
cd ../landing-ledger
python3 -m unittest discover -s tests          # 40 个用例
bash examples/demo.sh
```

## 能力一览（ledger.py）

| 能力 | 命令 |
|---|---|
| 借贷平衡记账（幂等 id、科目规则、受保护科目） | `post` / `transfer` |
| 红字冲销（不改历史） | `reverse` |
| 意图 → 动手 → 结算（代码探针取数，核实后才更新状态，支持期望模板） | `intent open / settle / observe / writeoff / cancel / list` |
| 外部对账（目录、JSON、外部快照、按期发生额），差额进挂账 | `reconcile [--snapshot] [--adjust] [--since-close]` |
| 挂账清单与带原因清理 | `suspense` / `clear-suspense` |
| 结账（要求临时科目已归零、标红已处理；快照余额与链头哈希；有待复核项时写明复核人） | `close [--reviewed-by 姓名]` |
| 全量校验（哈希链、平衡、规则、结账快照、账外锚点、严格模式） | `verify [--anchor] [--strict]` |
| 试算平衡表 / 余额 | `trial` / `balance` |

## 版本

0.5.0：整理成一套可复用的架构。新增 `landing-ledger` skill（跨会话的「设计决策 → 代码」落地对账：借方/凭证格式、机械核验与门禁、反向扫描、只读快照、四个子代理提示词模板、虚构演示项目、40 个用例（含一轮对抗审查的回归）），由一次真实仓库历史回放的经验通用化而来：门禁只拦高精度信号，机械驳回进复核清单，凭证新增 `deviations` 字段，借方缺接线约束时 `lint` 提示。新增 `docs/architecture.md`（两本账的结构、与 harness 五层的对应、定位与落地路线）和 `experiments/`（E1～E3 的报告、脚本、数据；E3 为脱敏汇总）。`double-entry-ledger` skill 本身没有改动（`ledger.py` 仍报 0.4.0），只在参考里指向新 skill。

0.4.0：新增 Claude Code 钩子适配器 `scripts/cc_hook.py`，由 SKILL.md 前言注册，调用 skill 即生效（插件安装、直接复制 skill 目录两种方式都实测可用）。PreToolUse 由代码推出期望并开单，PostToolUse 结算并当场提示，PostToolUseFailure 处理「报错但部分生效」，Stop 重新结算全部意图、把账面结论显示给用户、账目干净时自动结账。SKILL 新增「在 Claude Code 里」一节（开工前登记任务意图），`L()` 改用 `${CLAUDE_SKILL_DIR}`；「常见情形」里区分「生产交付物的工具有 bug（可以修或绕过，并汇报）」和「检查本身（不能为了通过而改）」——0.3.0 的写法在对照实验里让 agent 连构建脚本的明显 bug 也不敢修，交付反而变少。

0.3.0：根据实测（经 harness 注入故障；带 / 不带 skill 的 agent 做同一批任务）修复：结果无法测量时 settle 状态为 `unknown`（CLI 退出码 3），不再混同于 `mismatch`；`verify --strict` 也列出结账门槛（如子 agent 预算没归零）；有待复核项时 `close` 必须写明复核人（`--reviewed-by`）；时间戳带本机时区偏移，`intent list` 显示本机时间；SKILL 新增「常见情形」，并更正了「预期变化为 0」那条规则。

0.2.0：经过两轮对抗性审查（代码红队、概念/漏洞红队、受压场景实测），审查发现均已修复并配有回归用例。
在 macOS + Python 3.9 上测试通过；Windows 文件锁未实测。

## 许可证

MIT，见 [LICENSE](LICENSE)。
