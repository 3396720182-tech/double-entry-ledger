# 数据格式：借方与凭证

两份都是 JSON 数组，可以拆成多个文件（`--debits a.json b.json`）。路径一律相对于**代码快照根目录**，行号从 1 开始。
`python3 scripts/landing.py lint --debits … --vouchers …` 检查格式。

## 借方（debit）：规划会话产出，只读设计文档，看不到代码

```json
{
  "id": "ADR-002#1",
  "source": "docs/decisions/ADR-002-audit-log.md:7",
  "doc_kind": "adr",
  "doc_status": "Accepted",
  "doc_says": "unspecified",
  "claim": "删除笔记本之前由 notebooks 模块调用 recordAudit，action 为 DELETE，写失败就不删",
  "code_checkable": true,
  "evidence": {
    "symbols": ["recordAudit"],
    "call": {"symbol": "recordAudit", "scope_hint": "notebooks", "must_include": ["DELETE"]},
    "literals": [{"text": "requestId", "role": "write"}],
    "test_hint": "写日志失败时不删除"
  }
}
```

| 字段 | 说明 |
|---|---|
| `id` | 全局唯一、人能读懂：`ADR-002#1`、`SPEC-search#3`、`TASKS-add-login#2.1` |
| `source` | `文档路径:行号`。判定「已取代但原文没冲销」时要用它，务必写准 |
| `doc_kind` | `adr` / `spec-main` / `spec-archived` / `change-active` / `invariant` / `architecture` / `sdd` / 其他，自由取值 |
| `doc_status` | 文档自己的状态字段原文（Accepted、已归档、进行中……） |
| `doc_says` | 文档自己对这一条怎么说：`done`（如 `[x]`、「已上线」）/ `pending`（明说待做）/ `unspecified`。**只看文档，不推断**。`done` 的条目如果没落地，输出会标「⚠ 文档自称已完成」 |
| `claim` | 一句话：代码里应当成立的事 |
| `code_checkable` | 代码层承诺（函数、接线、守卫、字段写入、路由挂载、依赖……）为 `true`；纯流程、运维、文档类为 `false`，这类不进本账，计入「不在本账范围」 |
| `evidence.symbols` | 文档**点名**的、代码里必须有定义的标识符。没点名就留空，不要编 |
| `evidence.call` | 该能力必须在哪里生效：`symbol` 被调用的符号；`scope_hint` 调用点所在的目录名或文件名（只按路径段匹配）；`must_include` 调用点附近必须出现的字面量。都取自文档原文 |
| `evidence.literals` | 必须出现的字面量；`role: "write"` 表示必须有写入侧落点 |
| `evidence.test_hint` | 文档要求的验证点（给人看，不参与核验） |

**接线约束是借方最重要的部分。** 凡是「某能力必须在某入口生效」的决定，`call` 必须带 `scope_hint` 或 `must_include`。
只写 `call.symbol` 时，仓库里任何一处调用都能冒充落地：回放实验里，一张拿别处调用冒充「入口已接」的对抗凭证，就这样被机器入了账（`examples/demo/adversarial/` 复现了这件事）。`lint` 会对这类借方给出「接线约束过弱」提示。

粒度：一条可以独立核对的决定一笔。一个函数「建了」和「在某处接上了」是两笔（前者只要 `symbols`，后者要 `call`）。被拒绝的备选方案、背景描述不开借方。

## 凭证（voucher）：实现会话产出，看得到代码

```json
{
  "debit_id": "ADR-004#1",
  "status": "claimed_landed",
  "defs":  [{"file": "src/share/links.ts", "line": 6, "symbol": "assertShareAlive"}],
  "sites": [{"file": "src/share/links.ts", "line": 23, "symbol": "assertShareAlive"}],
  "tests": [{"file": "tests/share.test.ts", "symbol": "assertShareAlive"}],
  "deviations": ["openShareLink 不检查过期，过期链接仍能打开"],
  "supersede_ref": "docs/specs/audit/spec.md:6",
  "note": "一句话"
}
```

| 字段 | 说明 |
|---|---|
| `status` | `claimed_landed`（已落地）/ `admitted_unlanded`（自认未落地）/ `superseded`（已被后来的决策取代） |
| `defs` | 能力的定义处 |
| `sites` | **生产代码**里让承诺成立的位置：调用点、接线点、守卫所在行。测试文件、注释、import 行、忽略目录（`dist/` 等）里的文件都不算；借方有 `call` 时，定义行也不算调用点（只要求「函数建了」的借方，落点可以就是定义处）。最多给 5 个最有力的 |
| `tests` | 覆盖它的测试文件 |
| `deviations` | 声称已落地、但和文档有出入的地方，一条一句。**有偏差就写在这里，不要只写在 note 里**：这是门禁的高精度信号之一（回放里「note 里自述偏差、机器照样放行」的漏报都是这种） |
| `supersede_ref` | `superseded` 时必填，必须写到行号：后来那条决策的 `文档路径:行号`。如果来源文档自己已注明被取代，就指向来源文档里**写着「已被……取代 / 不再 / 改为」的那一行**（不能是这条决定本身那一行） |
| `note` | 补充说明。`admitted_unlanded` 时写清缺什么 |

每笔代码层借方一张凭证；同一笔借方两张凭证会被拒绝（防止后一张悄悄覆盖前一张）。

## 判定（`reconcile` 的输出）

| 结论 | 条件 | 门禁 | 回放实测精度 |
|---|---|---|---|
| `NO_VOUCHER` | 没交凭证 | 阻断 | — |
| `BAD_VOUCHER` | `superseded` 的依据没写行号、文件不存在或行号越界 | 阻断 | — |
| `UNLANDED` | 实现会话自认未落地 | 阻断 | 29/30 为真（97%） |
| `DEVIATION` | 声称已落地，但 `deviations` 非空 | 阻断 | 回放时还没有这个字段；已入账抽样里 note 自述偏差的 2 笔都是真缺口 |
| `SUPERSEDED_UNRECORDED` | `supersede_ref` 不在来源文档里；或指向这条决定本身；或那一行附近看不到「取代 / 不再 / 改为 / superseded / deprecated」之类的说明：决策改了，原文没冲销 | 阻断 | 4/4 为真 |
| `REJECTED` | 凭证没通过机械核验（见下） | 复核，不阻断 | **2/44 为真（约 5%）** |
| `CREDITED_NO_TEST` | 核验通过，但没有测试文件按词引用借方点名的标识符 | 复核，不阻断 | — |
| `UNANCHORED` | 借方没有任何锚点（symbols / call / literals 全空），只能看凭证自述的位置 | 复核，不阻断 | — |
| `SUPERSEDED_RECORDED` | 依据指向来源文档里另一行，且那一行附近写明了被取代 | 通过 | — |
| `CREDITED` | 核验通过，且有测试文件引用 | 通过 | 抽样 22/25 确实落地 |

机械核验做的事（先做轻量词法处理：注释不算代码；字符串里的内容只给 `must_include` / `literals` 用，不算调用；import / re-export 行不算调用）：
- 凭证给的定义处（生产代码）附近确有借方点名的符号定义；
- 落点是存在的生产代码行，附近有借方点名的标识符（或经包装函数调到它）——只认借方给的名字，不认凭证自报的名字；
- `call`：附近有对 `call.symbol` 的引用，且不是它的定义行（可以经 `wrap_hops` 层包装函数间接调到，包装函数的范围按花括号 / 缩进截到函数体）；落点路径的某一段等于 `scope_hint` 或以「它.」开头（`scope_hint` 含 `/` 时按路径片段匹配；**不看注释和 import 里的文字**）；附近有 `must_include`；
- `role: write` 的字面量要以键或赋值目标的样子出现（`text: …`、`text = …`、`.text = …`），只是读取不算；
- 测试文件按词引用了借方点名的标识符（`recordAuditLogger` 不算引用 `recordAudit`）。
它**不判语义**，也不判测试是否真跑过：后者由 `--tests passed|failed|not-run` 告知，不告知就是「没查」，门禁结果最多是「不阻断」，不会是「通过」。

退出码：0 通过；1 阻断；3 不阻断，但有待复核或没查的项；2 用法错误、输入格式有误（字段类型不对等，`reconcile` 会先做 `lint` 的错误级校验）或工具内部错误——**2 永远不是门禁结论**。`--strict` 让待复核项也阻断。一笔代码层借方都没有时，门禁不会给「通过」。

## 配置（`--config cfg.json`，可选）

| 字段 | 默认 | 说明 |
|---|---|---|
| `code_ext` | `.ts .tsx .js .jsx .mjs .cjs .py` | 代码文件；`.py` 按 `#` 注释和三引号字符串处理，其余按 `//` `/* */` 和反引号处理 |
| `deliverable_files` | `package.json` `pyproject.toml` `requirements.txt` | 不是代码、但本身算交付物的文件，可以作为落点 |
| `ignore_dirs` | `node_modules dist build .git vendor …` | 不扫描，里面的文件也不能作为落点 |
| `test_patterns` | `tests/`、`__tests__/`、`*.test.ts`、`test_*.py` 等正则 | 命中即测试文件，不能作为落点 |
| `ops_prefixes` | `scripts/ tools/ bin/` | 运维脚本：可以作为落点；反向扫描里「只被它们引用」单列 |
| `doc_prefixes` | `docs/ openspec/ doc/` | 不当代码扫描 |
| `orphan_roots` | `[]`（全部） | 反向扫描只看这些前缀下的定义 |
| `orphan_ignore` | `^__`、`[Ff]or_?[Tt]ests?$`、`^_` | 按名字识别的测试钩子，只计数不列出 |
| `window` | `def 3, site 2, must 15, literal 3, body 80` | 各类窗口行数（`body` 是函数体最多截多少行） |
| `wrap_hops` | `2` | 调用点经几层包装函数间接调到目标也算 |

未知字段、类型不对、正则无效都直接报错（退出码 2），防止拼写错误让规则悄悄失效。

路径安全：凭证里的路径一律按快照根目录解析，绝对路径、`../`、指向根目录之外的符号链接都不读（报「文件不存在」）。
