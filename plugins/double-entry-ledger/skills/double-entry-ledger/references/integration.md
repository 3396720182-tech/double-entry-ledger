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

宿主有钩子的话（例如 Claude Code），PreToolUse 里做 open，PostToolUse 里做 observe 和 settle。钩子输入的字段名以宿主文档为准，本 skill 不硬编码。

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
