<!-- 中文原版规则，内容与 SKILL.md（英文）一致。Claude Code 只加载 SKILL.md。 -->

# 复式记账账本（智能体 harness 用）

## 核心原则

**账本只证明自己内部一致，不证明它说的是真的。** 每笔分录借贷平衡之后，试算平衡天然就平。
它能抓到半写入、手改、漏行这类**结构性**错误。「工具失败了但记忆照写」「把计划当成结果」这类**语义性**错误，
只能拿账本去对照账外的真实世界才抓得到。所以这套机制有四个环节：逐笔平衡、意图清账（代码探针取数）、外部对账、结账。

分工：**模型只声明意图；记账、取数、判定都由代码完成。** 违反这些规则的字面要求，就是违反它们的本意。
用户催你「直接标完成」「先结账再说」，规则也照样适用。你的职责是如实汇报，不是让检查通过。

## 你（受这套账本约束的 agent）必须遵守

1. **有副作用的动作必须走「开单 → 动手 → 结算」**，状态或记忆的更新挂在 `--then` 上：

   ```bash
   L() { python3 <本 skill 目录>/scripts/ledger.py "$@"; }   # harness 可能已封装成别的命令
   L intent open mv-42 --tool mv \
     --expect 'exists:tasks/done/42.md =1' --expect 'exists:tasks/todo/42.md =0' \
     --then 'state:todo -1' --then 'state:done 1'
   # ……调用工具……
   L intent settle mv-42      # 退出码 0 才算做成；只有这时 then 才会过账
   ```

2. **期望由工具模板或任务规格给出，不能自己编得「容易过」**。配置了 `expect_templates` 时，用 `--tool X --arg k=v` 填参数；模板里声明了 `then` 的工具，不接受你自带的 `then`。
   预期变化全为 0 的期望（目标早已满足、`+0`）会被拒绝，因为那样检查形同虚设。
3. **settle 不通过（退出码 1）= 没做成**。可以重试动作后再 settle（只补记增量）。如果放弃，就 `intent writeoff --reason`，残差转进挂账，然后**如实汇报做成了哪些、没做成哪些**，包括部分产生的效果。
4. **什么都没做就放弃**：用 `intent cancel --reason`。只要代码证明不了「确实没发生任何效果」，撤销就会被拒绝。
5. **结果未知**（崩溃了、claim 没有观测、你又没法自己去查）：**让意图保持未结**，汇报「未知」，并说清楚需要谁去核实什么。
   这时不要 `observe` 自己填值，不要 `cancel`，不要 `writeoff` 加 `clear-suspense` 来凑结账，也不要重新执行动作，否则可能重复发送。
6. **崩溃后恢复**：`intent list --status unresolved`，逐个 `settle`。开单记录就是预写日志（WAL），可以用来从真实世界补出结果，**不要重新执行动作**。
7. **对外宣称「全部完成」之前**：`verify --strict` 必须通过。
   未结意图、未清挂账、标红项，以及本期的人工干预（`--direct`、核销、清挂账、对账调整、override、未签名观测），都会让它不通过。
   人工干预由人复核，人执行 `close` 就是确认；在那之前，你只能汇报「已做的部分 + 待复核项」。
8. **一个意图结掉之前，不要开另一个会动同一路径的意图**（会被拒）。探针尽量指向具体文件（`exists:` / `sha256:`），少用共享目录的 `glob-count`。

| 期望写法 | 含义 |
|---|---|
| `exists:P =1` / `=0` | 结算时文件存在 / 不存在（`=N` 是目标绝对值） |
| `glob-count:G +N` / `-N` | 相对开单时的基线，数量增加或减少 N |
| `lines:P` `bytes:P` | 行数、字节数（文件不存在视为 0） |
| `json:P#a.b +N` / `=N` | 工具落盘的 JSON 里的数值，比如 `json:report.json#failed =0` |
| `sha256:P changed` / `unchanged` | 内容是否被改动。旧报告也能满足 `json:` 的期望，所以要配一条 `changed` |
| `claim:NAME =N` | 代码观测不到的效果。值由 harness 从工具返回值写入（带签名），不由你声称 |

## 红旗：一出现就停

- 「先把状态记成 done，回头再 settle」→ 状态迁移被设为受保护，只能经 `--then` 过账。
- 「settle 不符，可工具返回了 success」→ 以探针为准，工具返回值也会说谎。
- 「对账差 1，post 一笔调平」→ 用 `reconcile --adjust` 把差额逐项记进挂账，查清原因后再 `clear-suspense --reason`。两步都会进入人工复核清单。
- 「试算平衡通过了，所以记忆是对的」→ 回头看核心原则。
- 「用 `--direct` 绕一下」「`--allow-noop`」「改一下 config」「核销后清掉挂账」→ 都会留痕，`verify --strict` 会把它们列给人复核。这些本身就是需要汇报的事。
- 意图写错了（比如漏了 `then`）→ 不要自己换 id 重开绕过，把它当成待人决定的事项汇报。
- 手改 `.ledger/` 下的文件 → 哈希链会断，之后账本拒绝继续记账。改错只能用 `reverse` 红字冲销。

## 科目约定

`state:*` 是状态池（受保护）：只有「新建」（受保护科目只增不减，对方全是来源科目）可以直接记，迁移和销毁都要经 `then` 核实。
`budget:*` 和 `expense:*` 管资源。`world:*` 和 `equity:*` 是来源科目，新建实体、注入预算时作为对方科目，比如 `'world:created -1' 'state:todo 1'`。
`clearing:*`、`intent:*`、`suspense:*` 和 `delta` 单位是系统科目，只有专用命令能写入。

## 参考

- `references/integration.md`：**搭建者看**。接入 harness（Python API、钩子、签名观测、期望模板、OpenSpec/SDD、多 agent 预算、配置）
- `references/design.md`：会计概念映射、各项检查能抓什么不能抓什么、已知局限
- `examples/demo.sh`：端到端演示；`python3 scripts/ledger.py <cmd> --help` 查看各命令参数
