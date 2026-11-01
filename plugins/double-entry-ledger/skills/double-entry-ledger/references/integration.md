# 搭建者指南：把账本嵌进 harness

SKILL.md 是写给**受约束的 agent** 看的；这份文档写给**搭 harness 的人**。只有部署方式对了，职务分离才是真的。

## 0. 部署检查表（缺一项，账本就可能被模型绕过）

- [ ] observe 和 settle 由 **harness 代码**执行（工具执行器或钩子），不交给模型调 CLI。
- [ ] harness 进程持有 `LEDGER_OBSERVER_KEY`，模型的执行环境**拿不到**它。配置里开 `reject_self_certified: true`。
- [ ] 模型对 `.ledger/` **没有任何写权限**，包括 Edit/Write 和 Bash 重定向。只读权限可以给。
- [ ] 模型不能传 `--config` / `--journal`，也不能改 `LEDGER_CONFIG` / `LEDGER_JOURNAL`。如果你把 CLI 暴露给模型，请包一层固定这些参数。
- [ ] 每种有副作用的工具都配了 `expect_templates`（包括它触发的 `then` 状态迁移），并开 `require_expect_template: true`。这样模型既不能选更弱的期望，也不能给任意动作挂上任意的状态迁移。
- [ ] 模型拿到的 `--snapshot` 只能来自你的观察脚本。对账调整受保护科目必须写 `--reason`，并会进入人工复核清单。
- [ ] 每次 `close` 输出的链头哈希写到账外（git commit message、OpenSpec archive、飞书文档），审计时用 `verify --anchor`。
- [ ] 明确由**人**执行 `close`。`verify --strict` 会列出本期的人工干预（`--direct`、核销、清挂账、对账调整、override、未签名观测）；有这些项时 `close` 必须带 `--reviewed-by <复核人>`，复核人和复核清单会写进 close 记录。最好让模型的执行环境调不到 `close`。
- [ ] 想清楚账本损坏时怎么处理：失败即关（停止执行，等人处理），还是失败即放（记告警、继续跑）。崩溃留下的不完整末行默认会自动截掉；其他损坏一律拒绝继续。

## 1. Python API

```python
import sys; sys.path.insert(0, "<skill>/scripts")
from ledger import Ledger, CheckFailed, LedgerError

led = Ledger(".ledger/journal.jsonl")          # 可选第二个参数：配置 dict 或配置文件路径

led.post("t-1", ["world:created -1", "state:todo 1"], memo="新任务 X", meta={"change": "add-login"})
led.transfer("t-2", "budget:pool", "budget:agent:A", 5000, unit="tokens")
led.intent_open("call-17", tool="mv", args={"src": "tasks/todo/x.md", "dst": "tasks/done/x.md"},
                then=["state:todo -1", "state:done 1"], meta={"tool_use_id": "toolu_..."})
r = led.intent_settle("call-17")               # r["status"]: settled / mismatch / unknown / then_rejected
v = led.verify(strict=True)                    # {"ok", "violations", "warnings", ...}
```

- 写操作返回记录 dict。重复提交时返回原记录，并带 `_duplicate: True`。
- `CheckFailed` 表示业务检查未通过（退出码 1），`.details` 里是逐条原因。其他 `LedgerError` 是用法或账本损坏（退出码 2）。CLI 的 `intent settle` 结果未知时退出码为 3。
- 每个进程实例化一次 `Ledger` 即可，所有操作都在文件锁内完成，多进程、多 agent 共用同一份日记账是安全的。

## 2. 挂在工具执行器上

```python
def run_tool(call):
    led.intent_open(call.id, tool=call.name, args=call.args,          # 期望由模板生成
                    then=state_effects(call), meta={"tool_use_id": call.id})
    result = execute(call)
    for name, value in extract_claims(call.name, result):            # 从结构化返回值取，不读模型叙述
        led.intent_observe(call.id, name, value, source="tool", evidence=result.request_id)
    r = led.intent_settle(call.id)
    if r["status"] == "unknown":                                     # 缺观测 / 探针出错：不是失败，别让模型重试
        return tool_error(f"结果未知，保持未结、不要重做：{r['results']}")
    if r["status"] != "settled":
        return tool_error(f"效果与计划不符：{r['results']}")         # 结构化错误还给模型
    return result
```

**期望模板**（`.ledger/config.json`）：值可以是期望列表，也可以是 `{expect, then}`。声明了 `then` 的工具不接受调用方自带的 `then`。

```json
"expect_templates": {
  "mv":         {"expect": ["exists:{dst} =1", "exists:{src} =0"],
                 "then":   ["state:todo -1 {task}", "state:done 1 {task}"]},
  "write_file": ["exists:{path} =1", "sha256:{path} changed"],
  "run_tests":  ["json:{report}#failed =0", "sha256:{report} changed"],
  "send_email": ["claim:sent =1"]
},
"require_expect_template": true
```

参数通过 `--arg k=v` 原样替换进模板，不会被再次解析成格式串。

宿主有钩子的话，PreToolUse 里做 open，PostToolUse 里做 observe 和 settle。Claude Code 已有现成的适配器，见 §2.1。

### 2.1 Claude Code 钩子适配器（`scripts/cc_hook.py`）

SKILL.md 前言里声明了 `PreToolUse` / `PostToolUse` / `PostToolUseFailure` / `Stop` 四个钩子。按 Claude Code 的规则，**skill 前言里的钩子在 skill 被调用时注册，并在本会话剩余时间里一直生效**，所以用户装好之后调用一次 `/double-entry-ledger` 就能用上。插件安装和直接复制 skill 目录两种方式都可以（钩子进程里的 `CLAUDE_PLUGIN_ROOT`：插件安装时是插件根目录，个人 skill 时是 skill 目录，命令两种都兼容）。

| 事件 | 做什么 |
|---|---|
| PreToolUse | 拦截对 `.ledger/` 的写入和应由人执行的账本命令（`close`、`observe`、`clear-suspense`、`repair`、`--direct`、`--config`、`--journal`）；由代码从工具参数推出期望，开立意图 `cc-<tool_use_id>`。互不相干的操作各开一笔，同一路径的多步操作并成一笔 |
| PostToolUse | 结算。不符时用 `decision: block` 把 ✗ 项贴在工具结果旁边（工具已经执行，block 只是让 Claude 一定看到） |
| PostToolUseFailure | 代码证明没有产生任何效果就自动撤销（工具如实报错、世界也没变，账实一致）；部分生效则保持未结，并告诉 Claude 哪些已经改动 |
| Stop | 重新结算全部未结意图（含 agent 自己开立的任务意图）；从没收到 PostToolUse 的工具意图（被拒或中断）在代码证明无效果后撤销；有未兑现的意图时用 `additionalContext` 让 Claude 再看一次（同一组问题只提醒一次），并用 `systemMessage` 把账面结论直接显示给用户；账目干净（无未结、无挂账/标红/结账门槛、无待复核项）且本期有发生额时自动 `close`，链头哈希追加到 `.ledger/anchors.txt` |

能推出期望的动作：`Write`（新文件 `exists =1`，已有文件 `sha256 changed`，内容相同则不开单）、`Edit` / `MultiEdit` / `NotebookEdit`（`sha256 changed`），以及 Bash 里的 `mv`、`cp`、`rm`、`rmdir`、`mkdir`、`touch`、`tee`、`git mv`、`git rm`、`sed -i`、`>` / `>>` 重定向。解析器的原则是**宁可不推断，也不误报**（误报会让 Claude 白跑一轮，还会让用户想关掉钩子）：
- 通配符按执行前的文件系统展开（与 shell 一致）；`cd` / `pushd` 会跟着走；heredoc 正文当数据跳过，后面的行照常解析；`A || true` 这类吞错误的写法照样记 A 的期望（这正是最该抓的情形）。
- 不推断：含 `$变量`、命令替换、花括号展开的段落；`A || 备选命令`；`[ 条件 ] && 操作`、`grep -q … && 操作`（操作本来就可能不执行）；后台 `&`；循环、条件、函数定义所在的整条命令；`cp -R dir/` 这类 GNU 与 BSD 语义不同的写法。
- 一条推不出效果、又可能写文件的命令（项目脚本、构建、测试）**之前**的操作不再核对，因为它可能又动了那些文件；`chmod`、`git add/commit` 这类不改文件存在与内容的命令不算。
- `sed -i` 只改一个文件时，没变化就是没生效，记账；改多个文件时，替换规则本来就可能只命中一部分，所以改为账外核对：全都没变才判「没生效」，部分没变只列出没变的文件作提示，不记成未兑现。
- 只改大小写的改名（`mv readme.md README.md`）不期望旧名消失（macOS 默认文件系统不区分大小写）。
- 工具调用的路径和 agent 的任务意图重叠时，账本不允许重叠的意图，钩子不单独开单，改为在账外记下基线、调用后照样核对并提示；任务意图在 Stop 时结算。
- 推不出效果的交付靠任务意图。账本里还没有任何任务意图时，第一次遇到推不出效果的命令会提醒 agent 登记；项目自己的脚本（`tools/x.sh`、`python3 scripts/y.py`）第一次被调用时各提醒一次——项目脚本「打印成功、其实没做」是对照实验里最难靠 agent 自觉发现的一类。

会话与并发：工具意图的 meta 里记着 `session_id` / `prompt_id`。Stop 只结算、撤销、提醒本会话的工具意图（agent 开立的任务意图在项目内共享）。只开了单、没收到 PostToolUse 的调用（被拒绝或中断），在下一个回合、或 agent 自己要开意图时，代码证明没效果就撤销；还在执行中的并行调用不会被当成「重试」撤掉。Stop 在 `stop_hook_active` 为真（这一轮本身就是钩子要求继续的）时只给用户看，不再要求 Claude 继续，避免无限续轮。

守卫只看**写入目标**：重定向目标、`cp/mv/install/rsync/ln` 的目标、`rm/touch/tee/chmod…` 的参数、`sed -i`、`dd of=`、带 `-delete/-exec` 的 `find`、`git rm/mv/checkout/restore/clean` 以及会连带删掉被忽略文件的 `git clean -x/-X`、`xargs` 配写命令、解释器内联脚本（`-c` / heredoc）里提到 `.ledger` 的。路径按不区分大小写比较并先展开通配符。`cat`、`jq`、`git log`、`sed -n`、复制出去做备份等只读用法不受影响。人工专属命令按 `ledger.py` 之后的子命令位置识别（`--memo observe` 这种文字不会误伤），子命令用命令替换拼出来的一律拦下。

其他约定：
- 账本固定在 `$CLAUDE_PROJECT_DIR/.ledger/`，第一次遇到可推断的动作时自动 `init`。钩子自己的状态在 `.ledger/cc-state.json`，出错日志在 `.ledger/cc-hook.log`。
- **失败即放**：钩子内部出错时放行工具调用并记日志；Stop 时出错会用 `systemMessage` 告诉用户「本轮未对账」。
- 环境变量：`LEDGER_CC_HOOKS=off` 关闭全部钩子；`LEDGER_CC_AUTOCLOSE=0` 不自动结账。
- 开销：每次被匹配的工具调用多两次 Python 冷启动，实测每次几十到一百多毫秒。

**局限（和 §0 检查表对照）**：在普通的 Claude Code 会话里，钩子进程和模型的 Bash 跑在同一个用户、同一个环境下，所以 observer 密钥藏不住，模型也能绕开钩子直接调 `ledger.py`。钩子拦截的是「顺手」的越权（改 `.ledger/`、自己结账、自己写观测），不是蓄意的对抗。要真正的职务分离，仍然要按 §0 把账本挂在你自己的 harness 进程里。

**签名观测**：harness 进程的环境里有 `LEDGER_OBSERVER_KEY` 时，`intent_observe` 会自动给 `(intent, metric, value)` 加 HMAC 签名；settle 时用同一把密钥验签。
没有签名的 `source=tool` 只是标签。模型也可以写 `--source tool`，所以严格模式下这类观测一律不算数。
有签名的观测一旦存在，之后未签名的观测不能覆盖它。

**人工核实**：claim 结果未知（settle 状态 `unknown`）时，由人查证（比如看发件箱），再在**持有密钥**的环境里写入观测，然后结算：

```bash
LEDGER_OBSERVER_KEY=... L intent observe notify-client --metric sent --value 1 --source observer --evidence '发件箱 11-01 06:29 已发'
L intent settle notify-client
```

## 3. 对应到 OpenSpec / SDD

| SDD 节点 | 记账动作 |
|---|---|
| proposal / tasks.md 定稿 | 每个任务 `post 'world:created -1 <task>' 'state:todo 1 <task>'`（按实体记账，见 §5） |
| 执行单个 task | `intent open`（模板）→ 实现 → `intent settle`；状态迁移挂在 `then` 上 |
| 验证 | `run_tests` 模板：`json:report#failed =0` + `sha256:report changed`，防止拿旧报告过关 |
| archive | `reconcile`（对照 tasks.md 勾选和文件）→ `verify --strict` → `close <change-id>` → 链头哈希写进 archive |

上表核对的是 change 执行过程中每一步的效果。「这次 change 的设计决策有没有全部落到代码里」是另一本账：归档前用同插件里的 `landing-ledger` skill（借方来自 proposal/spec/ADR，凭证由实现会话逐笔交，门禁只拦高精度信号）。

## 4. 多 agent 预算

```
equity:funding → budget:pool → budget:agent:<id> → expense:tokens:<id>
                      ↑________ 退回 ________|
```

- 花费由 **LLM 客户端包装层**按 `response.usage` 记账，不让子 agent 自报。id 用 `spend-<agent>-<request_id>`，网络重试是幂等的。
- **超支照记、标红**：`budget:*` 的默认规则是 `min 0, mode flag`，钱已经花掉了，账上就不能拒记。超支项会出现在 `verify` 的提示里，并阻止 `close`，直到补批或冲减。
- 需要**事前拦截**时，用预留（encumbrance）：
  1. 在配置里加规则 `{"match": "budget:agent:*", "min": 0, "mode": "reject"}`。
  2. 调用前，把 `max_tokens` 从 `budget:agent:A` 转到 `budget:hold:A`。余额不够时，这一步会被拒，调用也就不必发出。
  3. 调用后，把实际用量从 hold 转到 `expense`，剩余部分退回 `budget:agent:A`。
  因为实际用量不会超过 `max_tokens`，花费只会从 hold 里扣。
- 子 agent 结束时退回余额。`budget:agent:*` 默认 `zero_at_close`，失联或漏报的 agent 在结账时会暴露。
- 供应商账单往往按期出：`reconcile --snapshot usage.json --since-close` 比较的是**本期发生额**，不是累计余额。
- `reconcile --adjust` 会把差额逐个目标记进 `suspense:reconcile:<科目>`。多个目标的差额不会相互抵消。

## 5. 按实体记账

把实体 id 当计量单位，余额就表示它当前在哪个池子里：

```bash
L() { python3 <skill>/scripts/ledger.py "$@"; }   # 用函数而不是 L="python3 ..."：zsh 不会拆分 $L
L post --id new-42 'world:created -1 task42' 'state:todo 1 task42'
L intent open mv-42 --tool mv --arg src=tasks/todo/42.md --arg dst=tasks/done/42.md \
   --then 'state:todo -1 task42' --then 'state:done 1 task42'
```

要保证这件实体**只被创建一次**，需要加一条规则（`unit` 支持通配）：

```json
{"match": "world:created", "unit": "task*", "min": -1}
```

有了这条规则，又因为每笔分录都守恒、`state:*` 有 `min 0`、迁移必须经 `then`，这件实体只会出现在一个状态池里（金额请用整数）。
外部对账写法：`{"account": "state:done", "unit": "task42", "probe": "exists:tasks/done/42.md"}`。

## 6. 配置参考（`.ledger/config.json`，`init` 会生成）

| 字段 | 默认 | 说明 |
|---|---|---|
| `default_unit` | `count` | 省略单位时使用 |
| `rules` | `[]` | **追加**在内置规则之后：`{"match", "unit"?, "min"?, "max"?, "mode": "reject"\|"flag", "zero_at_close"?}`；`match` 和 `unit` 都支持 fnmatch 通配 |
| `replace_default_rules` | `false` | 为 true 才会去掉内置规则（state 非负、budget 超支标红、clearing/suspense/子预算结账归零） |
| `guarded` | `["state:*"]` | 受保护科目。只有新建（受保护行全部流入、对方全是 `world:`/`equity:`）可以直接记；迁移和销毁只能经 `then`，或者用会留痕的 `--direct` |
| `reconcile` | `[]` | `{"account", "unit"?, "probe"}`，只接受数值型探针 |
| `expect_templates` / `require_expect_template` | `{}` / `false` | 见 §2；值为列表或 `{expect, then}` |
| `reject_self_certified` / `observer_key_env` | `false` / `LEDGER_OBSERVER_KEY` | 见 §2 |
| `require_reconcile_before_close` | `false` | 最后一笔过账之后必须有一次全部相符的对账 |
| `auto_repair_torn_tail` | `true` | 崩溃留下的不完整末行从未被确认，追加时先备份再截掉 |
| `root` / `allow_outside_root` | 日记账目录的上一级 / `false` | 探针根目录；默认禁止读取根目录之外的路径 |

- 配置里出现未知字段会直接报错，防止拼写错误让规则悄悄失效。
- 每条记录都带配置指纹 `cfg`。如果配置在一个结账期内被改过，`verify` 会提示。

## 7. 性能与上下文

- 实测（macOS，Python 3.9，2 万条记录，7.6 MB）：
  - 常驻进程用 Python API 时，增量校验每次追加约 5 ms。每次仍会对全文做一次字节摘要，所以对旧记录的任何改动都能当场发现。
  - CLI 每次冷启动需要全量重放，约 0.3 s；`verify` 约 0.6 s。
  - 更大规模时，阶段结束先 `close`，再换一份新日记账（`--journal`），旧账只读归档。
- 给模型看的只需要 `verify --quiet` 或 settle 的一行结论。不要让模型 `cat journal.jsonl`；需要细节时用 `--json` 加 `jq` 取相关字段。
