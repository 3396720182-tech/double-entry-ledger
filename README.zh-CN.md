# double-entry-ledger

[English](README.md)

把会计里的复式记账用到智能体 harness 上，让三件事都能**用代码自校验**，错误在发生的地方就被发现，不需要模型参与判断：

- 记忆和状态变更
- 工具调用的「计划 vs 实际」
- 多 agent 之间 token / 预算的拨付

> 一句话：**试算平衡只证明账本内部一致；真相来自对账。** 所以除了借贷平衡，还有意图清账、外部对账、挂账和结账。

## 仓库结构

```
.claude-plugin/marketplace.json          # 插件市场目录（只含这一个插件）
plugins/double-entry-ledger/
├── .claude-plugin/plugin.json           # 插件清单
└── skills/double-entry-ledger/
    ├── SKILL.md                         # 给受约束的 agent 读：必须遵守的规则和红旗（英文）
    ├── SKILL.zh-CN.md                   # 同上，中文原版
    ├── scripts/ledger.py                # 单文件，只依赖标准库，Python 3.8+（CLI + 可 import）
    ├── scripts/cc_hook.py               # Claude Code 钩子适配器（SKILL.md 前言注册，调用 skill 即生效）
    ├── references/integration.md        # 给搭建者读：部署检查表、API、钩子、签名、模板、SDD、预算
    ├── references/design.md             # 会计概念映射、能抓什么不能抓什么、已知局限
    ├── examples/demo.sh                 # 端到端演示（在临时目录里运行）
    ├── tests/test_ledger.py             # 80 个用例：并发、篡改、崩溃恢复、两轮对抗性审查回归
    └── tests/test_cc_hook.py            # 钩子适配器：按真实钩子协议驱动，覆盖命令解析、结算、守卫、Stop
```

## 安装（Claude Code 插件）

Claude Code 2.1.275 及以上，一步装好：

```
/plugin install double-entry-ledger --marketplace yzx-labs/double-entry-ledger
```

或者传统的两步：

```
/plugin marketplace add yzx-labs/double-entry-ledger
/plugin install double-entry-ledger@double-entry-ledger
```

装完运行 `/reload-plugins`（或重启 Claude Code）。任务符合描述时 skill 会自动加载，也可以手动用 `/double-entry-ledger`（或带插件前缀的 `/double-entry-ledger:double-entry-ledger`）调用。以后更新：`/plugin marketplace update double-entry-ledger`。

### 在 Claude Code 里用：调用一次就挂上钩子

在会话里输入 `/double-entry-ledger <你的任务>`。skill 前言里声明的钩子会在调用时注册，本会话之后一直生效，不需要改任何 settings：

- **工具调用自动记账**：`Write` / `Edit` 和 Bash 里的简单文件命令（`mv cp rm mkdir touch tee`、`git mv/rm`、`sed -i`、重定向），由代码从参数推出预期效果，调用后探测文件结算。工具说成功但实际没生效（`sed` 没匹配到、`mv … || true`），会当场把 ✗ 项贴在工具结果旁边。
- **任务意图**：脚本、构建、测试推不出效果，skill 要求 agent 开工前把每项交付登记成意图。
- **Stop 时对账**：重新结算全部未结意图；没兑现的先让 Claude 再看一次，再直接显示给你（「账本：N 笔意图未兑现……」）；账目干净就自动结账，链头哈希写进 `.ledger/anchors.txt`。
- **守卫**：拒绝 agent 改 `.ledger/`，拒绝它自己执行 `close`、`observe` 等应由人做的命令。
- 账本在项目根的 `.ledger/`，钩子给过的每条提示都记在 `.ledger/cc-hook.log`。关闭：启动前设 `LEDGER_CC_HOOKS=off`。需要 PATH 上有 `python3`。

普通 Claude Code 会话里，钩子和模型的 Bash 跑在同一个环境，它防的是「顺手」的越权，不是蓄意对抗；要真正的职务分离仍需按 `references/integration.md` §0 部署。适配器能推断什么、不能推断什么，见 `references/integration.md` §2.1。

### 不用插件系统

- **个人级 skill**：把 `plugins/double-entry-ledger/skills/double-entry-ledger/` 复制到 `~/.claude/skills/double-entry-ledger/`。钩子同样随 skill 调用生效。
- **项目级 skill**：复制到项目里的 `.claude/skills/double-entry-ledger/`。
- **其他支持 SKILL.md 规范的宿主**：放到该宿主文档规定的 skills 目录。
- **只要工具**：`scripts/ledger.py` 不依赖任何其他文件，直接复制进你的 harness 就能用。

**接入前请读 `references/integration.md` §0 的部署检查表。** 职务分离要靠部署落实，只装 skill 是不够的。

## 自检

```bash
cd plugins/double-entry-ledger/skills/double-entry-ledger
python3 -m unittest discover -s tests
bash examples/demo.sh
```

## 能力一览

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

0.4.0：新增 Claude Code 钩子适配器 `scripts/cc_hook.py`，由 SKILL.md 前言注册，调用 skill 即生效（插件安装、直接复制 skill 目录两种方式都实测可用）。PreToolUse 由代码推出期望并开单，PostToolUse 结算并当场提示，PostToolUseFailure 处理「报错但部分生效」，Stop 重新结算全部意图、把账面结论显示给用户、账目干净时自动结账。SKILL 新增「在 Claude Code 里」一节（开工前登记任务意图），`L()` 改用 `${CLAUDE_SKILL_DIR}`；「常见情形」里区分「生产交付物的工具有 bug（可以修或绕过，并汇报）」和「检查本身（不能为了通过而改）」——0.3.0 的写法在对照实验里让 agent 连构建脚本的明显 bug 也不敢修，交付反而变少。

0.3.0：根据实测（经 harness 注入故障；带 / 不带 skill 的 agent 做同一批任务）修复：结果无法测量时 settle 状态为 `unknown`（CLI 退出码 3），不再混同于 `mismatch`；`verify --strict` 也列出结账门槛（如子 agent 预算没归零）；有待复核项时 `close` 必须写明复核人（`--reviewed-by`）；时间戳带本机时区偏移，`intent list` 显示本机时间；SKILL 新增「常见情形」，并更正了「预期变化为 0」那条规则。

0.2.0：经过两轮对抗性审查（代码红队、概念/漏洞红队、受压场景实测），审查发现均已修复并配有回归用例。
在 macOS + Python 3.9 上测试通过；Windows 文件锁未实测。

## 许可证

MIT，见 [LICENSE](LICENSE)。
