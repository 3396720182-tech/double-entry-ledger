<!-- 中文原版规则，内容与 SKILL.md（英文）一致。Claude Code 只加载 SKILL.md。 -->

# 复式记账账本（智能体 harness 用）

## 核心原则

**账本只证明自己内部一致，不证明它说的是真的。** 每笔分录借贷平衡之后，试算平衡天然就平。
它能抓到半写入、手改、漏行这类**结构性**错误。「工具失败了但记忆照写」「把计划当成结果」这类**语义性**错误，
只能拿账本去对照账外的真实世界才抓得到。所以这套机制有四个环节：逐笔平衡、意图清账（代码探针取数）、外部对账、结账。

分工：**模型只声明意图；记账、取数、判定都由代码完成。** 违反这些规则的字面要求，就是违反它们的本意。
用户催你「直接标完成」「先结账再说」，规则也照样适用。你的职责是如实汇报，不是让检查通过。

## 在 Claude Code 里：调用本 skill 就会挂上钩子

调用本 skill（用户输入 `/double-entry-ledger`，或 Claude 自己调用）之后，本会话剩下的时间里钩子一直生效，记账由代码完成：

- **工具调用自动记账。** 每次 `Write` / `Edit` / `NotebookEdit`，以及 Bash 里的简单文件命令（`mv cp rm rmdir mkdir touch tee`、`git mv/rm`、`sed -i`、`>` / `>>`），钩子都会先由代码从工具参数推出预期效果，开立意图 `cc-<工具调用 id>`；调用结束后再探测文件、结算。这些不用你自己包。
- 工具结果旁边出现 **「[复式记账] 工具返回成功，但……不符」**，说明 ✗ 的那几项虽然工具说成功了，其实没有发生（比如 `sed` 没匹配到、`mv … || true`）。要么修好，要么如实汇报，不能算作完成。
- **脚本、构建、测试这类命令，钩子推不出效果。** 所以开工前，**把用户要的每一项交付登记成一个任务意图**，期望取自用户的要求（不需要 `then`；一项交付一个意图，共用的检查单独一个）：

  ```bash
  L() { python3 "${CLAUDE_SKILL_DIR}/scripts/ledger.py" "$@"; }   # 每次新开 shell 都要重新定义
  L intent open task-docs --expect 'exists:docs/site/index.html =1' --memo '生成文档'
  L intent open task-bump --expect 'sha256:pkg/a/pyproject.toml changed' --expect 'sha256:pkg/b/pyproject.toml changed'
  ```

  你停下时，Stop 钩子会由代码重新结算所有未结意图。没兑现的会先给你看一次，让你补做或更正汇报，然后直接显示给用户——用户看到的是账本的结论，不只是你的总结。做不完的保持未结并说明，不要为了让账变平去撤销或核销。
- **不要碰账本。** 钩子会拒绝对 `.ledger/` 的写入，以及应由人执行的命令（`close`、`intent observe`、`clear-suspense`、`repair`、`--direct`、`--config`、`--journal`）。账目干净的一期由 Stop 钩子自动结账；有待复核项时，等人执行 `close --reviewed-by`。
- 关闭钩子：`LEDGER_CC_HOOKS=off`（由用户设置，不是你）。钩子需要 PATH 上有 `python3`。

不在 Claude Code 里，或你自己开立意图时，按下面的规则执行。

## 你（受这套账本约束的 agent）必须遵守

1. **有副作用的动作必须走「开单 → 动手 → 结算」**，状态或记忆的更新挂在 `--then` 上：

   ```bash
   L() { python3 "${CLAUDE_SKILL_DIR}/scripts/ledger.py" "$@"; }   # ${CLAUDE_SKILL_DIR} 即本 skill 目录；harness 可能已封装成别的命令；每次新开 shell 都要重新定义
   L init                     # 项目里还没有 .ledger/ 时，在项目根目录执行一次
   L intent open mv-42 --tool mv \
     --expect 'exists:tasks/done/42.md =1' --expect 'exists:tasks/todo/42.md =0' \
     --then 'state:todo -1' --then 'state:done 1'
   # ……调用工具……
   L intent settle mv-42      # 退出码 0 才算做成，只有这时 then 才会过账；1 = 没做成；3 = 结果未知
   ```

2. **期望由工具模板或任务规格给出，不能自己编得「容易过」**。配置了 `expect_templates` 时，用 `--tool X --arg k=v` 填参数；模板里声明了 `then` 的工具，不接受你自带的 `then`。没有模板时 `--tool` 只是个标签。
   预期变化为 0 的期望（目标早已满足、`+0`）只能防倒退，证明不了动作发生过：一个意图的期望**全部**为 0 时会被拒绝；单条为 0 只标 ⚠，必须再配一条会变化的期望（比如 `sha256:P changed`）。
3. **settle 不通过（退出码 1）= 没做成**，意图状态为 `mismatch`，保持未结。可以重试动作后再 settle（只补记增量）。如果放弃，就 `intent writeoff --reason`，残差转进挂账，然后**如实汇报做成了哪些、没做成哪些**，包括部分产生的效果。
   settle 退出码 3、显示 `?`、状态为 `unknown`，意思是结果未知，不是没做成，按第 5 条处理。
4. **什么都没做就放弃**：用 `intent cancel --reason`。只要代码证明不了「确实没发生任何效果」，撤销就会被拒绝。
5. **结果未知**（settle 状态为 `unknown`：崩溃了、claim 没有观测，你又没法自己去查）：**让意图保持未结**，汇报「未知」，并说清楚需要谁去核实什么。
   这时不要 `observe` 自己填值，不要 `cancel`，不要 `writeoff` 加 `clear-suspense` 来凑结账，也不要重新执行动作，否则可能重复发送。
6. **崩溃后恢复**：`intent list --status unresolved`，逐个 `settle`。开单记录就是预写日志（WAL），可以用来从真实世界补出结果，**不要重新执行动作**（唯一的例外见下方「常见情形」）。
7. **对外宣称「全部完成」之前**：`verify --strict` 必须通过。
   未结意图、未清挂账、标红项、其他结账门槛（比如子 agent 预算没归零），以及本期的人工干预（`--direct`、核销、清挂账、对账调整、override、未签名观测），都会让它不通过。
   人工干预由人复核：有待复核项时，`close` 必须带 `--reviewed-by <复核人>`，这就是人的签收。**不要替人填复核人**，即使用户说「能结账就结账」，也只汇报「已做的部分 + 待复核项」，让人自己执行。
   settle 失败本身也会记一条记录，这是正常的，不算人工干预。
8. **一个意图结掉之前，不要开另一个会动同一路径的意图**（会被拒）。探针尽量指向具体文件（`exists:` / `sha256:`），少用共享目录的 `glob-count`。

| 期望写法 | 含义 |
|---|---|
| `exists:P =1` / `=0` | 结算时文件存在 / 不存在（`=N` 是目标绝对值） |
| `glob-count:G +N` / `-N` | 相对开单时的基线，数量增加或减少 N |
| `lines:P` `bytes:P` | 行数、字节数（文件不存在视为 0） |
| `json:P#a.b +N` / `=N` | 工具落盘的 JSON 里的数值，比如 `json:report.json#failed =0`（旧报告可能本来就是 0，必须配 `sha256:report.json changed`） |
| `sha256:P changed` / `unchanged` | 内容是否被改动。旧报告也能满足 `json:` 的期望，所以要配一条 `changed` |
| `claim:NAME =N` | 代码观测不到的效果。值由 harness 从工具返回值写入（带签名），不由你声称 |

## 常见情形

| 情形 | 做法 |
|---|---|
| 接手已有项目，账上还没有这些任务 | 开始前用新建分录登记一次，比如 `L post --id init 'world:created -3' 'state:todo 3'`；之后的迁移都走 `then` |
| 一开始就做不了（缺输入、缺权限） | 不用开意图。任务留在原状态（比如 `state:todo`，不影响结账），汇报原因 |
| 动作执行了，但没有效果或结果不达标（比如测试脚本崩了却退出 0、测试有失败项） | 问题出在你自己的产出上，就修好、重试、再 settle。**生产**交付物的工具有 bug（比如构建脚本写错了输出路径），只要不超出用户要求的范围，修好它或绕过去都是分内的事——探针核对的是真实交付物，不是工具打印的话——汇报里写明改了什么、为什么。不能为了通过而改的是**检查**本身：测试、校验器、验收脚本、报告路径、未结意图的期望。改这些要用户同意。在等用户（批准、补输入）就让意图保持未结并汇报，之后可以接着 settle；确定放弃才 `writeoff --reason`，挂账留给人处理。不要用 `cancel`，它只用于还没动手就放弃 |
| 多个任务共用一个检查（比如同一份测试报告） | 每个任务的产出和状态迁移各开一个意图；共用的检查在最后单独开一个意图，不挂 `then`。别把任务的 done 和别的任务也可能拖垮的检查绑在一起 |
| 崩溃恢复时，settle 显示「确定没做」 | 只有探针（不是 claim）证明效果完全没产生、并且动作只影响本地文件时，才可以重做再 settle。有外部效果的动作（发消息、付款、调接口）一律不重做 |
| 子 agent 崩溃或失联，预算没归零 | 不要替它把余额退回，它可能有没上报的花费。汇报差额，等人按供应商账单核实（`reconcile --snapshot <账单> --since-close`） |
| 人工核实了一个 claim（比如查过发件箱） | 由持有 observer 密钥的人或 harness 执行 `intent observe <id> --metric <名> --value <值> --source observer --evidence '<查证依据>'`，然后 settle。你自己不要执行 |
| 排查时要跑会写文件的命令 | 只读排查不用开意图。会写文件时，别碰未结意图的探针路径，并在汇报里说明写了什么 |
| 汇报里提到时间 | 记录的 `ts` 带时区偏移，`intent list` 显示的是本机时间。不要把带 `+00:00` 的时间直接当成本地时间 |

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

- `scripts/cc_hook.py`：Claude Code 钩子适配器（上面那些钩子执行的就是它）；文件开头列出它能推断、不能推断的动作
- `references/integration.md`：**搭建者看**。接入 harness（Python API、钩子、签名观测、期望模板、OpenSpec/SDD、多 agent 预算、配置）
- `references/design.md`：会计概念映射、各项检查能抓什么不能抓什么、已知局限
- `examples/demo.sh`：端到端演示；`python3 scripts/ledger.py <cmd> --help` 查看各命令参数
- `../landing-ledger/`：跨会话的另一本账——设计决策（ADR、spec、tasks.md）有没有落到代码里。OpenSpec/SDD 归档门禁用它；本 skill 管的是会话内的单次工具调用
