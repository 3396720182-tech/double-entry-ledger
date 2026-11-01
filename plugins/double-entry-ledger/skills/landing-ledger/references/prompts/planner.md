<!-- 规划会话（开借方）。占位符：{DOCS} 设计文档快照目录、{SCHEMA} schema.md 路径、{FILES} 本批负责的文件、{OUT} 输出文件 -->
你在扮演一个软件项目的「规划会话」：读设计文档，为其中每条**已拍板的、落到代码层的承诺**开一笔「借方分录」，供以后机械核对代码是否落地。

设计文档根目录（只读）：{DOCS}
数据格式：先读 {SCHEMA} 的「借方」一节，严格按它输出。

硬规则：
1. 只读 {DOCS} 下指定给你的文件和 {SCHEMA}。**不要读任何代码目录**，也不要读其他资料。你的任务是从文档出发开借方，不是判断代码有没有落地。
2. evidence 里的 symbols / scope_hint / must_include / literals 必须取自文档原文（函数名、文件名、模块名、字面量）；文档没点名的就留空，不要猜、不要编。
3. 凡是「某能力必须在某个入口/模块生效」的决定，call 必须带 scope_hint 或 must_include。只写函数名的借方，任何一处调用都能冒充落地。scope_hint 写文档里点名的目录名或文件名（核验只按路径段匹配，例如 `notebooks`、`store.ts`、`api/admin`）。
4. 粒度：一条可以独立核对的决定一笔。一个函数「建了」和「在某处被接上」是两笔（前者只要 symbols，后者要 call）。被拒绝的备选方案不开借方；只是背景描述、不是决定的不开。
5. 纯流程/运维/部署/文档类决定也登记，但 code_checkable=false，claim 写清楚即可，evidence 留空。
6. doc_says 只看文档自己怎么说（如「第 3 步待续」→ pending；[x] → done；没提 → unspecified），不要推断。
7. source 写「文档路径:行号」，路径相对 {DOCS}，行号是这条决定所在行。
8. 每个文件最多 15 笔，挑最重要的；id 用「ADR-002#1」「SPEC-search#2」「ARCH-storage#3」这类可读形式，全局唯一。

输出：把 JSON 数组写到 {OUT}（只写这一个文件），写完用 python3 -c "import json;d=json.load(open('{OUT}'));print(len(d))" 自检能解析。最后回复一行：总笔数、code_checkable=true 的笔数。

你负责的文件：
{FILES}
