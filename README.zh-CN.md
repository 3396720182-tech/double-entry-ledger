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
    ├── references/integration.md        # 给搭建者读：部署检查表、API、钩子、签名、模板、SDD、预算
    ├── references/design.md             # 会计概念映射、能抓什么不能抓什么、已知局限
    ├── examples/demo.sh                 # 端到端演示（在临时目录里运行）
    └── tests/test_ledger.py             # 72 个用例：并发、篡改、崩溃恢复、两轮对抗性审查回归
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

装完运行 `/reload-plugins`（或重启 Claude Code）。任务符合描述时 skill 会自动加载，也可以手动用 `/double-entry-ledger:double-entry-ledger` 调用。以后更新：`/plugin marketplace update double-entry-ledger`。

### 不用插件系统

- **个人级 skill**：把 `plugins/double-entry-ledger/skills/double-entry-ledger/` 复制到 `~/.claude/skills/double-entry-ledger/`。
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

0.3.0：根据实测（经 harness 注入故障；带 / 不带 skill 的 agent 做同一批任务）修复：结果无法测量时 settle 状态为 `unknown`（CLI 退出码 3），不再混同于 `mismatch`；`verify --strict` 也列出结账门槛（如子 agent 预算没归零）；有待复核项时 `close` 必须写明复核人（`--reviewed-by`）；时间戳带本机时区偏移，`intent list` 显示本机时间；SKILL 新增「常见情形」，并更正了「预期变化为 0」那条规则。

0.2.0：经过两轮对抗性审查（代码红队、概念/漏洞红队、受压场景实测），审查发现均已修复并配有回归用例。
在 macOS + Python 3.9 上测试通过；Windows 文件锁未实测。

## 许可证

MIT，见 [LICENSE](LICENSE)。
