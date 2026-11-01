#!/usr/bin/env python3
"""
landing.py — 设计决策 → 代码的「落地对账」（仅依赖标准库，Python 3.8+）

把「这个决策到底做完了没」变成逐笔可查的账：
  借方（debit）   规划会话**只读设计文档**（ADR、spec、tasks.md、不变量……）开出的代码层承诺
  凭证（voucher） 实现会话**看代码**为每笔借方交的证据：定义处 / 生产代码落点 / 测试，
                  或者自认未落地、或者声明已被后来的决策取代
  贷方（credit）  本脚本按凭证做的**机械核验**：文件/行存在、是生产代码、附近有借方点名的标识符、
                  调用点落在文档点名的模块、必含字面量在场、测试文件引用。语义对不对，它不判。
  反向对账        orphans：有定义、生产代码零引用的导出符号（贷方无借方），补「承诺没进账」的盲区

门禁只拦高精度信号（实测见仓库 experiments/e3-landing-replay）：
  阻断  NO_VOUCHER / BAD_VOUCHER / UNLANDED（自认未落地）/ DEVIATION（自述偏差）/
        SUPERSEDED_UNRECORDED（决策已改、来源文档没冲销）
  复核  REJECTED（机械驳回，实测精度约 5%）/ CREDITED_NO_TEST / UNANCHORED —— 列清单，不阻断（--strict 时阻断）
  通过  CREDITED / SUPERSEDED_RECORDED
测试是否本期真跑过：由 --tests 告知；不告知就是「没查」，不算通过（跳过 ≠ 通过）。

核对时先做轻量词法处理：注释不算代码；字符串里的内容只给 must_include / literals 用，
不算调用；import / re-export 行不算调用；落点附近的定义行不算调用。

退出码：0 = 通过；1 = 阻断；3 = 不阻断，但有待复核或没查的项；2 = 用法错误 / 输入损坏 / 内部错误。

用法：
  landing.py lint      --debits D.json... [--vouchers V.json...]
  landing.py reconcile ROOT --debits D.json... --vouchers V.json... [--tests passed|failed|not-run]
                       [--strict] [--config C.json] [--json OUT]
  landing.py orphans   ROOT [--config C.json] [--json OUT]
"""
from __future__ import annotations

import argparse
import json
import os
import posixpath
import re
import sys
from collections import OrderedDict

VERSION = "0.5.0"

DEFAULT_CONFIG = {
    # 代码文件扩展名（同时决定词法：.py 用 # 和三引号，其余用 // /* */ 和反引号）
    "code_ext": [".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".py"],
    # 不是代码、但本身就是交付物的文件（决策常常就是「加一个依赖」）
    "deliverable_files": ["package.json", "pyproject.toml", "requirements.txt"],
    "ignore_dirs": ["node_modules", "dist", "build", ".git", "vendor", "__pycache__", ".venv", "venv",
                    "coverage", ".next", ".ledger"],
    # 命中任一正则（对 / 分隔的相对路径）即为测试文件
    "test_patterns": [r"(^|/)(tests?|__tests__|e2e|spec)/", r"\.(test|spec)\.[cm]?[jt]sx?$",
                      r"(^|/)test_[^/]*\.py$", r"_test\.py$", r"(^|/)conftest\.py$"],
    # 运维脚本：可以作为落点（脚本本身常是交付物）；反向扫描里「只被它们引用」单独归类
    "ops_prefixes": ["scripts/", "tools/", "bin/"],
    # 不当作代码扫描的目录（设计文档等）
    "doc_prefixes": ["docs/", "openspec/", "doc/"],
    # 反向扫描范围（相对路径前缀）；空 = 全部非测试代码
    "orphan_roots": [],
    # 反向扫描中按名字识别为测试钩子、单独计数不列出的符号
    "orphan_ignore": [r"^__", r"[Ff]or_?[Tt]ests?$", r"^_"],
    # 各类窗口（行数）
    "window": {"def": 3, "site": 2, "must": 15, "literal": 3, "body": 80},
    # 调用点允许经几层包装函数间接调到目标（0 = 只认直接调用）
    "wrap_hops": 2,
}

VERDICTS = ["CREDITED", "SUPERSEDED_RECORDED", "CREDITED_NO_TEST", "UNANCHORED", "REJECTED",
            "DEVIATION", "UNLANDED", "SUPERSEDED_UNRECORDED", "NO_VOUCHER", "BAD_VOUCHER"]
BLOCK = {"NO_VOUCHER", "BAD_VOUCHER", "UNLANDED", "DEVIATION", "SUPERSEDED_UNRECORDED"}
REVIEW = {"REJECTED", "CREDITED_NO_TEST", "UNANCHORED"}
VOUCHER_STATUS = ("claimed_landed", "admitted_unlanded", "superseded")
DOC_SAYS = ("done", "pending", "unspecified")
TESTS_STATES = ("passed", "failed", "not-run")
KEYWORDS = {"if", "for", "while", "switch", "catch", "return", "function", "typeof", "await", "new",
            "super", "import", "require", "print", "len", "isinstance", "def", "class", "with", "elif",
            "async", "yield", "throw", "delete", "void", "in", "of", "not", "and", "or", "lambda"}
# 来源文档里「这一条已被取代」的说法；取代依据指向来源文档时，那一行附近必须看得到这类字样
SUPERSEDE_MARK = re.compile(r"supersed|replaced by|deprecated|obsolete|no longer|取代|替代|代替|废弃|作废|不再|改为|改成|改由|撤销",
                            re.I)
REF_RE = re.compile(r"(.+):(\d+)", re.ASCII)


class UsageError(Exception):
    pass


# ---------------------------------------------------------------- 输入

def load_json_list(paths, what):
    out = []
    for p in paths:
        try:
            with open(p, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError) as e:
            raise UsageError(f"{what} 读不了：{p}（{e}）")
        if not isinstance(data, list):
            raise UsageError(f"{what} 必须是 JSON 数组：{p}")
        for i, item in enumerate(data):
            if not isinstance(item, dict):
                raise UsageError(f"{what} 第 {i} 项不是对象：{p}")
        out.extend(data)
    return out


def _str_list(v):
    return isinstance(v, list) and all(isinstance(x, str) for x in v)


def load_config(path):
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if path:
        try:
            with open(path, encoding="utf-8") as fh:
                user = json.load(fh)
        except (OSError, ValueError) as e:
            raise UsageError(f"配置读不了：{path}（{e}）")
        if not isinstance(user, dict):
            raise UsageError("配置必须是 JSON 对象")
        unknown = set(user) - set(cfg)
        if unknown:  # 拼写错误不能让规则悄悄失效
            raise UsageError(f"配置里有未知字段：{sorted(unknown)}")
        for k, v in user.items():
            if k == "window":
                if not isinstance(v, dict):
                    raise UsageError("window 必须是对象")
                bad = set(v) - set(cfg["window"])
                if bad:
                    raise UsageError(f"window 里有未知字段：{sorted(bad)}")
                cfg["window"].update(v)
            else:
                cfg[k] = v
    for k in ("code_ext", "deliverable_files", "ignore_dirs", "test_patterns", "ops_prefixes", "doc_prefixes",
              "orphan_roots", "orphan_ignore"):
        if not _str_list(cfg[k]):
            raise UsageError(f"配置 {k} 必须是字符串数组")
    for k, v in cfg["window"].items():
        if not (isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 10000):
            raise UsageError(f"配置 window.{k} 必须是 0～10000 的整数")
    if not (isinstance(cfg["wrap_hops"], int) and not isinstance(cfg["wrap_hops"], bool)
            and 0 <= cfg["wrap_hops"] <= 10):
        raise UsageError("配置 wrap_hops 必须是 0～10 的整数")
    for k in ("test_patterns", "orphan_ignore"):
        for p in cfg[k]:
            try:
                re.compile(p)
            except re.error as e:
                raise UsageError(f"配置 {k} 里的正则无效：{p!r}（{e}）")
    return cfg


def norm_rel(p):
    """凭证/借方里的路径统一成相对的 / 分隔形式；非字符串返回空串。"""
    if not isinstance(p, str) or not p.strip():
        return ""
    p = posixpath.normpath(p.strip().replace("\\", "/"))
    return "" if p == "." else p


def split_ref(ref):
    """'path:line' → (path, line|None)。"""
    if not isinstance(ref, str):
        return "", None
    m = REF_RE.fullmatch(ref.strip())
    if m:
        return norm_rel(m.group(1)), int(m.group(2))
    return norm_rel(ref), None


def validate(debits, vouchers):
    """格式与类型校验：errors 让 reconcile 拒绝运行（退出码 2），warns 只提示。"""
    errors, warns = [], []
    seen = set()

    def need(cond, msg):
        if not cond:
            errors.append(msg)
        return cond

    for i, d in enumerate(debits):
        did = d.get("id")
        where = f"借方[{i}] {did if isinstance(did, str) else ''}".strip()
        if need(isinstance(did, str) and did.strip(), f"{where}：缺 id 或不是字符串"):
            need(did not in seen, f"{where}：id 重复")
            seen.add(did)
        need(isinstance(d.get("claim"), str) and d.get("claim"), f"{where}：缺 claim")
        need(isinstance(d.get("code_checkable"), bool), f"{where}：code_checkable 必须是 true/false")
        need(d.get("doc_says") in (None,) + DOC_SAYS, f"{where}：doc_says 只能是 {'/'.join(DOC_SAYS)}")
        if d.get("source") is None:
            warns.append(f"{where}：没写 source（文档路径:行号），取代判定会失准")
        else:
            need(isinstance(d.get("source"), str), f"{where}：source 必须是字符串")
        ev = d.get("evidence")
        if ev is None:
            ev = {}
        if not need(isinstance(ev, dict), f"{where}：evidence 必须是对象"):
            continue
        need(ev.get("symbols") is None or _str_list(ev.get("symbols")), f"{where}：evidence.symbols 必须是字符串数组")
        call = ev.get("call")
        if call is not None and need(isinstance(call, dict), f"{where}：evidence.call 必须是对象"):
            need(call.get("symbol") is None or isinstance(call.get("symbol"), str), f"{where}：call.symbol 必须是字符串")
            need(call.get("scope_hint") is None or isinstance(call.get("scope_hint"), str),
                 f"{where}：call.scope_hint 必须是字符串")
            need(call.get("must_include") is None or _str_list(call.get("must_include")),
                 f"{where}：call.must_include 必须是字符串数组")
        else:
            call = {}
        lits = ev.get("literals")
        if lits is not None and need(isinstance(lits, list), f"{where}：evidence.literals 必须是数组"):
            for x in lits:
                if need(isinstance(x, dict) and isinstance(x.get("text"), str) and x.get("text"),
                        f"{where}：literals 每项必须是 {{\"text\": 字符串, \"role\"?}}"):
                    need(x.get("role") in (None, "read", "write"), f"{where}：literals.role 只能是 read/write")
        if d.get("code_checkable") is True and isinstance(call, dict):
            if not (ev.get("symbols") or call.get("symbol") or ev.get("literals")):
                warns.append(f"{where}：没有可核对的锚点（symbols / call / literals 全空），最多判 UNANCHORED")
            if call.get("symbol") and not (call.get("scope_hint") or call.get("must_include")):
                warns.append(f"{where}：接线约束过弱——只写了 call.symbol，没有 scope_hint / must_include；"
                             "任何地方的一次调用都能冒充落地（对抗凭证实测能骗过）")

    vseen = set()
    for i, v in enumerate(vouchers):
        k = v.get("debit_id")
        where = f"凭证[{i}] {k if isinstance(k, str) else ''}".strip()
        if need(isinstance(k, str) and k.strip(), f"{where}：缺 debit_id 或不是字符串"):
            need(k not in vseen, f"{where}：同一笔借方有多张凭证")
            vseen.add(k)
        st = v.get("status")
        need(st in VOUCHER_STATUS, f"{where}：status 只能是 {'/'.join(VOUCHER_STATUS)}")
        for key in ("defs", "sites", "tests"):
            locs = v.get(key)
            if locs is None:
                continue
            if not need(isinstance(locs, list), f"{where}：{key} 必须是数组"):
                continue
            for x in locs:
                if not need(isinstance(x, dict), f"{where}：{key} 每项必须是对象"):
                    continue
                need(isinstance(x.get("file"), str), f"{where}：{key}.file 必须是字符串")
                if key != "tests":
                    ln = x.get("line")
                    need(isinstance(ln, int) and not isinstance(ln, bool), f"{where}：{key}.line 必须是整数")
                need(x.get("symbol") is None or isinstance(x.get("symbol"), str), f"{where}：{key}.symbol 必须是字符串")
        need(v.get("deviations") is None or _str_list(v.get("deviations")), f"{where}：deviations 必须是字符串数组")
        need(v.get("note") is None or isinstance(v.get("note"), str), f"{where}：note 必须是字符串")
        if st == "superseded":
            ref = v.get("supersede_ref")
            if need(isinstance(ref, str) and ref.strip(), f"{where}：superseded 必须给 supersede_ref（文档路径:行号）"):
                if split_ref(ref)[1] is None:
                    warns.append(f"{where}：supersede_ref 没写行号，会被判 BAD_VOUCHER")
        if st == "claimed_landed" and not v.get("sites"):
            warns.append(f"{where}：claimed_landed 却没有 sites，会被驳回")
        if st == "admitted_unlanded" and not v.get("note"):
            warns.append(f"{where}：自认未落地最好在 note 里说明缺什么")
    if debits and vouchers:
        for k in vseen:
            if k not in seen:
                warns.append(f"凭证 {k}：对不上任何借方")
        missing = [d.get("id") for d in debits if d.get("code_checkable") is True and d.get("id") not in vseen]
        if missing:
            warns.append(f"{len(missing)} 笔代码层借方没有凭证：{missing[:8]}{' …' if len(missing) > 8 else ''}")
    return errors, warns


# ---------------------------------------------------------------- 词法

def lex(rel, text):
    """逐行给出两种视图：code（去掉注释，保留字符串）、bare（去掉注释，字符串内容清空）。"""
    py = rel.endswith(".py")
    code, bare, cl, bl = [], [], [], []
    i, n, state = 0, len(text), None
    while i < n:
        ch = text[i]
        if ch == "\n":
            if state == "lc" or state in ('"', "'"):  # 行注释结束；单引号字符串不能跨行
                state = None
            code.append("".join(cl))
            bare.append("".join(bl))
            cl, bl = [], []
            i += 1
            continue
        if state is None:
            if (py and ch == "#") or (not py and text.startswith("//", i)):
                state = "lc"
            elif not py and text.startswith("/*", i):
                state = "bc"
                i += 2
                continue
            elif py and text.startswith(('"""', "'''"), i):
                state = text[i:i + 3]
                cl.append(state)
                bl.append(state)
                i += 3
                continue
            elif ch in ('"', "'") or (ch == "`" and not py):
                state = ch
                cl.append(ch)
                bl.append(ch)
            else:
                cl.append(ch)
                bl.append(ch)
            i += 1
            continue
        if state == "lc":
            i += 1
            continue
        if state == "bc":
            if text.startswith("*/", i):
                state = None
                i += 2
            else:
                i += 1
            continue
        if ch == "\\" and i + 1 < n and text[i + 1] != "\n":
            cl.append(text[i:i + 2])
            i += 2
            continue
        if text.startswith(state, i):
            cl.append(state)
            bl.append(state)
            i += len(state)
            state = None
            continue
        cl.append(ch)
        i += 1
    code.append("".join(cl))
    bare.append("".join(bl))
    return code, bare


JS_IMPORT = re.compile(r"\s*(import\b|export\s*(type\s+)?(\*|\{[^}]*\})\s*(as\s+\w+\s*)?from\b|\}\s*from\b)")
PY_IMPORT = re.compile(r"\s*(from\s+\S+\s+)?import\b")


def import_lines(rel, bare):
    """import / re-export 行（含多行块）的行号集合。"""
    py = rel.endswith(".py")
    out, close = set(), None
    for i, s in enumerate(bare):
        if close:
            out.add(i)
            if close in s:
                close = None
            continue
        if py:
            if PY_IMPORT.match(s):
                out.add(i)
                if "(" in s and ")" not in s:
                    close = ")"
            elif s.startswith("__all__"):
                out.add(i)
                if "[" in s and "]" not in s:
                    close = "]"
        else:
            st = s.strip()
            if re.match(r"(import\b.*|export\s*(type\s+)?)\{", st) and "}" not in st:
                out.add(i)
                close = "}"
            elif JS_IMPORT.match(s):
                out.add(i)
    return out


class Source:
    __slots__ = ("raw", "code", "bare", "imports")

    def __init__(self, rel, text):
        self.raw = text.split("\n")
        self.code, self.bare = lex(rel, text)
        self.imports = import_lines(rel, self.bare)


# ---------------------------------------------------------------- 代码树

class Tree:
    """快照根目录下的代码文件：分类（生产 / 测试 / 运维）与按需读取（只读根目录之内的真实路径）。"""

    def __init__(self, root, cfg):
        if not os.path.isdir(root):
            raise UsageError(f"快照根目录不存在：{root}")
        self.root, self.cfg = os.path.realpath(root), cfg
        self._test_rx = [re.compile(p) for p in cfg["test_patterns"]]
        self._src = {}
        self._code = None

    def is_code(self, rel):
        return os.path.splitext(rel)[1] in self.cfg["code_ext"] and not rel.endswith(".d.ts")

    def is_test(self, rel):
        return any(rx.search(rel) for rx in self._test_rx)

    def is_ops(self, rel):
        return rel.startswith(tuple(self.cfg["ops_prefixes"]))

    def is_doc(self, rel):
        return rel.startswith(tuple(self.cfg["doc_prefixes"]))

    def is_ignored(self, rel):
        return any(seg in self.cfg["ignore_dirs"] for seg in rel.split("/")[:-1])

    def is_prod(self, rel):
        """生产代码或交付物：代码文件（含运维脚本）/ 依赖清单；不是测试、不在文档目录或忽略目录里。"""
        if not rel or self.is_test(rel) or self.is_doc(rel) or self.is_ignored(rel):
            return False
        return self.is_code(rel) or posixpath.basename(rel) in self.cfg["deliverable_files"]

    def path(self, rel):
        """根目录之内的真实路径；绝对路径、../、指向根外的符号链接一律返回 None。"""
        rel = norm_rel(rel)
        if not rel or rel.startswith("../") or rel == ".." or posixpath.isabs(rel):
            return None
        p = os.path.realpath(os.path.join(self.root, rel))
        return p if p.startswith(self.root + os.sep) else None

    def src(self, rel):
        rel = norm_rel(rel)
        if rel not in self._src:
            p = self.path(rel)
            s = None
            if p and os.path.isfile(p):
                with open(p, encoding="utf-8", errors="replace") as fh:
                    s = Source(rel, fh.read())
            self._src[rel] = s
        return self._src[rel]

    def lines(self, rel):
        s = self.src(rel)
        return s.raw if s else None

    def code_files(self):
        if self._code is None:
            out = []
            ignore = set(self.cfg["ignore_dirs"])
            for dp, dn, fn in os.walk(self.root):
                dn[:] = sorted(x for x in dn if x not in ignore)
                for f in sorted(fn):
                    rel = os.path.relpath(os.path.join(dp, f), self.root).replace(os.sep, "/")
                    if self.is_code(rel) and not self.is_doc(rel) and self.path(rel):
                        out.append(rel)
            self._code = out
        return self._code


def win(lines, ln, r, skip=()):
    """第 ln 行（1 起）上下各 r 行；skip 里的行号（0 起）不要。"""
    lo, hi = max(0, ln - 1 - r), min(len(lines), ln + r)
    return "\n".join(lines[i] for i in range(lo, hi) if i not in skip)


def word(sym):
    return re.compile(rf"(?<![\w$]){re.escape(sym)}(?![\w$])")


MODS = r"(?:(?:export|default|public|private|protected|static|readonly|async|override|abstract|get|set)\s+)*"


def def_re(sym):
    """宽松：窗口里出现 sym 的「定义」写法即可（函数、类、变量、解构、方法、接口成员、Python def/class）。"""
    s = re.escape(sym)
    return re.compile(
        rf"(function\s*\*?\s*{s}\b"
        rf"|\b(?:const|let|var|class|interface|type|enum|model|def)\s+{s}\b"
        rf"|\b(?:const|let|var)\s*[\[{{][^=]*\b{s}\b[^=]*[\]}}]\s*="
        rf"|^\s*{MODS}{s}\s*[(<]"
        rf"|^\s*{MODS}{s}\??\s*:"
        rf"|^\s*(?:export\s+)?{s}\s*=)", re.M)


def def_line_re(sym):
    """严格：判断某一行是 sym 的定义行（而不是对它的调用）。"""
    s = re.escape(sym)
    return re.compile(
        rf"(function\s*\*?\s*{s}\b"
        rf"|\b(?:const|let|var|class)\s+{s}\b"
        rf"|^\s*(?:async\s+)?def\s+{s}\b"
        rf"|^\s*{MODS}{s}\s*\([^;]*\)\s*(?::\s*[^;=]+)?\{{\s*$)")


def body_range(rel, bare, i, limit):
    """第 i 行（0 起）定义的函数体所在行范围 [lo, hi)：TS/JS 按花括号配平，Python 按缩进。"""
    if rel.endswith(".py"):
        ind = len(bare[i]) - len(bare[i].lstrip())
        j = i + 1
        while j < len(bare) and j - i <= limit and (not bare[j].strip()
                                                    or len(bare[j]) - len(bare[j].lstrip()) > ind):
            j += 1
        return i, j
    depth, started = 0, False
    for j in range(i, min(len(bare), i + limit)):
        for ch in bare[j]:
            if ch == "{":
                depth += 1
                started = True
            elif ch == "}":
                depth -= 1
                if started and depth <= 0:
                    return i, j + 1
        if not started and j - i >= 2:  # 没有花括号的单行箭头函数等
            return i, j + 1
    return i, min(len(bare), i + limit)


# ---------------------------------------------------------------- 正向对账

class Checker:
    def __init__(self, tree):
        self.t = tree
        self.w = tree.cfg["window"]
        self._defs = {}
        self._calls = {}

    def find_defs(self, sym):
        """全仓（生产代码）找 sym 的定义行：[(rel, idx)]。"""
        if sym not in self._defs:
            rx, out = def_line_re(sym), []
            for rel in self.t.code_files():
                if not self.t.is_prod(rel):
                    continue
                s = self.t.src(rel)
                for i, l in enumerate(s.bare if s else []):
                    if sym in l and rx.search(l):
                        out.append((rel, i))
            self._defs[sym] = out
        return self._defs[sym]

    def callees(self, sym):
        """sym 函数体里调用到的名字。"""
        if sym not in self._calls:
            names = set()
            for rel, i in self.find_defs(sym):
                s = self.t.src(rel)
                lo, hi = body_range(rel, s.bare, i, self.w["body"])
                body = "\n".join(s.bare[lo:hi])
                body = body.split(sym, 1)[1] if lo == i and sym in body else body
                names |= {m for m in re.findall(r"(?<![\w$.])([A-Za-z_$][\w$]*)\s*[(<]", body)
                          if m not in KEYWORDS}
                names |= {m for m in re.findall(r"\.([A-Za-z_$][\w$]*)\s*\(", body)}
            self._calls[sym] = names - {sym}
        return self._calls[sym]

    def reaches(self, wrapper, targets, hops):
        """wrapper 的函数体在 hops 层以内调到 targets 之一。"""
        targets = set(targets)
        seen, frontier = {wrapper}, [wrapper]
        for _ in range(hops):
            nxt = []
            for f in frontier:
                c = self.callees(f)
                if c & targets:
                    return True
                nxt += [x for x in c if x not in seen]
                seen |= c
            frontier = nxt
        return False

    def check(self, d, v):
        if v is None:
            return "NO_VOUCHER", ["实现会话没有交凭证"]
        st = v.get("status")
        note = v.get("note") or ""
        if st == "admitted_unlanded":
            return "UNLANDED", ["实现会话自认未落地" + (f"：{note}" if note else "")]
        if st == "superseded":
            return self.check_superseded(d, v)
        devs = [x for x in (v.get("deviations") or []) if x]
        reasons, anchored = self.mechanical(d, v)
        if devs:  # 自述偏差是高精度信号，优先于机械结论
            return "DEVIATION", ["自述偏差：" + x for x in devs] + [f"（机械核验：{r}）" for r in reasons[:2]]
        if reasons:
            return "REJECTED", reasons
        if not anchored:
            return "UNANCHORED", ["借方没有可核对的锚点（symbols / call / literals 全空），只能看凭证自述的位置"]
        if self.test_ok(d, v):
            return "CREDITED", []
        return "CREDITED_NO_TEST", ["没有引用该能力的测试文件（测试是否真跑过另算）"]

    def check_superseded(self, d, v):
        ref = v.get("supersede_ref") or ""
        f, ln = split_ref(ref)
        note = v.get("note") or ""
        if ln is None:
            return "BAD_VOUCHER", [f"取代依据要写到行号（文档路径:行号）：{ref or '（空）'}"]
        lines = self.t.lines(f)
        if not lines:
            return "BAD_VOUCHER", [f"声称已被取代，但取代依据不存在：{ref}"]
        if not (1 <= ln <= len(lines)):
            return "BAD_VOUCHER", [f"取代依据行号越界：{ref}"]
        src_doc, src_ln = split_ref(d.get("source") or "")
        tail = f"；{note}" if note else ""
        if f != src_doc:
            return "SUPERSEDED_UNRECORDED", [f"依据 {ref}，但来源文档 {src_doc or '（未知）'} 没有冲销{tail}"]
        if src_ln is not None and ln == src_ln:
            return "SUPERSEDED_UNRECORDED", [f"取代依据就是借方的出处 {ref}，原文仍然要求旧做法{tail}"]
        if not SUPERSEDE_MARK.search(win(lines, ln, 1)):
            return "SUPERSEDED_UNRECORDED", [f"取代依据 {ref} 附近看不到「已取代 / 不再 / 改为」之类的说明{tail}"]
        return "SUPERSEDED_RECORDED", [f"来源文档内已注明：{ref}{tail}"]

    def mechanical(self, d, v):
        """返回 (驳回理由, 借方是否有锚点)。"""
        t, w = self.t, self.w
        ev = d.get("evidence") or {}
        syms = [s for s in (ev.get("symbols") or []) if s]
        call = ev.get("call") or {}
        cs = call.get("symbol") or ""
        lits = [x for x in (ev.get("literals") or []) if x.get("text")]
        idents = set(syms) | ({cs} if cs else set())
        anchored = bool(idents or lits)
        reasons = []

        # 1) 文档点名的符号，凭证给的定义处必须真有定义
        for s in syms:
            ok = False
            for dd in v.get("defs") or []:
                src = t.src(dd.get("file", "")) if t.is_prod(norm_rel(dd.get("file"))) else None
                ln = dd.get("line") or 0
                if src and 1 <= ln <= len(src.bare) and def_re(s).search(win(src.bare, ln, w["def"])):
                    ok = True
            if not ok:
                reasons.append(f"凭证给的定义处（生产代码）找不到符号定义：{s}")

        # 2) 生产代码落点：附近要有借方点名的标识符（或经包装函数调到它）
        valid = []
        for site in v.get("sites") or []:
            f, ln = norm_rel(site.get("file")), site.get("line")
            src = t.src(f)
            ssym = site.get("symbol") or ""
            if src is None:
                reasons.append(f"落点文件不存在：{f}")
                continue
            if not t.is_prod(f):
                reasons.append(f"落点不是生产代码：{f}:{ln}")
                continue
            if not (1 <= ln <= len(src.raw)):
                reasons.append(f"落点行号越界：{f}:{ln}")
                continue
            if not src.code[ln - 1].strip():
                reasons.append(f"落点是注释或空行：{f}:{ln}")
                continue
            if (ln - 1) in src.imports:
                reasons.append(f"落点是 import 行：{f}:{ln}")
                continue
            near_bare = win(src.bare, ln, w["site"], src.imports)
            near_code = win(src.code, ln, w["site"], src.imports)
            hit = any(word(x).search(near_bare) for x in idents) or any(x["text"] in near_code for x in lits)
            if not hit and ssym and idents:
                ws = ssym.split(".")[-1]
                hit = bool(re.search(rf"(?<![\w$]){re.escape(ws)}\s*\(", near_bare)
                           and self.reaches(ws, idents, t.cfg["wrap_hops"]))
            if not hit and not anchored and ssym:
                hit = bool(word(ssym).search(near_bare))
            if not hit:
                reasons.append(f"落点附近没有借方点名的标识符：{f}:{ln}")
                continue
            valid.append((f, ln, src, ssym))
        if not valid:
            reasons.append("没有有效的生产代码落点")

        # 3) 接线：调用点（不是定义行、import、注释、字符串）+ 文档点名的模块 + 必含字面量
        if cs:
            scope = (call.get("scope_hint") or "").strip().lower()
            must = [m for m in (call.get("must_include") or []) if m]
            ok = False
            for f, ln, src, ssym in valid:
                lo, hi = max(0, ln - 1 - w["site"]), min(len(src.bare), ln + w["site"])
                drx = def_line_re(cs)
                skip = src.imports | {i for i in range(lo, hi) if drx.search(src.bare[i])}
                near = win(src.bare, ln, w["site"], skip)
                direct = word(cs).search(near)
                if not direct:
                    ws = ssym.split(".")[-1]
                    if not (ws and ws != cs and re.search(rf"(?<![\w$]){re.escape(ws)}\s*\(", near)
                            and self.reaches(ws, {cs}, t.cfg["wrap_hops"])):
                        continue
                if scope and not scope_match(scope, f):
                    continue
                if any(m not in win(src.code, ln, w["must"], src.imports) for m in must):
                    continue
                ok = True
            if not ok:
                reasons.append(f"没有合格的调用点：{cs}" + (f" @ {scope}" if scope else "")
                               + (f" 含 {must}" if must else ""))

        # 4) 必须被写入的字面量：要以「键 / 赋值目标」的样子出现，读取不算
        for x in lits:
            if x.get("role") != "write":
                continue
            s = re.escape(x["text"])
            rx = re.compile(rf"(?<![\w$]){s}['\"]?\s*(?::|=(?!=))|\.{s}\s*=(?!=)|\[['\"]{s}['\"]\]\s*=(?!=)")
            if not any(rx.search(win(src.code, ln, w["literal"], src.imports)) for f, ln, src, _ in valid):
                reasons.append(f"没有写入侧落点：{x['text']}（要以键或赋值目标的形式出现）")
        return reasons, anchored

    def test_ok(self, d, v):
        """测试文件要按词引用借方点名的标识符（不用凭证自报的名字）。"""
        ev = d.get("evidence") or {}
        toks = set(ev.get("symbols") or []) | {(ev.get("call") or {}).get("symbol") or ""}
        toks = {x for x in toks if x} or {x.get("text") for x in (ev.get("literals") or []) if x.get("text")}
        for tt in v.get("tests") or []:
            f = norm_rel(tt.get("file"))
            src = self.t.src(f)
            if src and self.t.is_test(f) and any(word(x).search("\n".join(src.code)) for x in toks):
                return True
        return False


def scope_match(scope, rel):
    """scope_hint 只和路径比：某一段等于它、或以「它.」开头；含 / 的按路径片段匹配。"""
    rel = rel.lower()
    if "/" in scope:
        return ("/" + rel).find("/" + scope.strip("/")) >= 0
    return any(seg == scope or seg.startswith(scope + ".") for seg in rel.split("/"))


def reconcile(root, debits, vouchers, cfg, tests="not-run", strict=False):
    errors, _ = validate(debits, vouchers)
    if errors:
        raise UsageError("输入格式有误，先用 lint 修好：\n  " + "\n  ".join(errors[:20])
                         + (f"\n  …（共 {len(errors)} 条）" if len(errors) > 20 else ""))
    vmap = OrderedDict((v["debit_id"], v) for v in vouchers)
    chk = Checker(Tree(root, cfg))
    rows = []
    for d in debits:
        if not d.get("code_checkable"):
            continue
        v = vmap.get(d["id"])
        verdict, reasons = chk.check(d, v)
        rows.append({"id": d["id"], "source": d.get("source"), "doc_kind": d.get("doc_kind"),
                     "doc_says": d.get("doc_says"), "claim": d.get("claim"), "verdict": verdict,
                     "reasons": reasons, "voucher_status": (v or {}).get("status")})
    ids = {d["id"] for d in debits}
    block_set = BLOCK | (REVIEW if strict else set())
    review_set = set() if strict else REVIEW
    not_checked = []
    if not rows:
        not_checked.append("没有任何代码层借方：本次门禁什么也没核对")
    if tests == "not-run":
        not_checked.append("测试是否本期真跑过且通过：没查（--tests not-run 或没给；跳过 ≠ 通过）")
    if any(r["verdict"] in block_set for r in rows) or tests == "failed":
        gate = "block"
    elif any(r["verdict"] in review_set for r in rows) or not_checked:
        gate = "review"
    else:
        gate = "pass"
    return {
        "version": VERSION,
        "debits": len(debits), "code_checkable": len(rows), "out_of_scope": len(debits) - len(rows),
        "out_of_scope_ids": [d["id"] for d in debits if not d.get("code_checkable")],
        "counts": {k: sum(1 for r in rows if r["verdict"] == k) for k in VERDICTS},
        "tests": tests, "strict": strict, "not_checked": not_checked,
        "stray_vouchers": [k for k in vmap if k not in ids], "gate": gate, "rows": rows,
    }


def print_reconcile(res):
    c = res["counts"]
    print(f"落地对账 · 试算（landing.py {res['version']}）")
    print(f"借方 {res['debits']} 笔：代码层可核 {res['code_checkable']}，"
          f"流程/运维类 {res['out_of_scope']} 不在本账范围")
    print("  " + "  ".join(f"{k}={c[k]}" for k in VERDICTS))
    if res["out_of_scope_ids"]:
        ids = res["out_of_scope_ids"]
        print(f"不在本账范围（流程/运维类，本门禁不对它们负责）：{', '.join(map(str, ids[:10]))}{' …' if len(ids) > 10 else ''}")
    if res["tests"] == "passed":
        print("测试：本期已跑且通过（由调用方告知）")
    elif res["tests"] == "failed":
        print("测试：本期已跑，未通过 → 阻断")
    for x in res["not_checked"]:
        print(x)
    if res["stray_vouchers"]:
        print(f"⚠ 有凭证对不上任何借方（忽略）：{res['stray_vouchers']}")

    block_set = BLOCK | (REVIEW if res["strict"] else set())
    review_rows = [] if res["strict"] else [r for r in res["rows"] if r["verdict"] in REVIEW]
    groups = [("阻断", [r for r in res["rows"] if r["verdict"] in block_set]),
              ("待复核（不阻断；机械驳回在实测里多是核验器误报，请人或独立复核员逐条看）", review_rows)]
    for title, rows in groups:
        if not rows:
            continue
        print(f"\n{title}：{len(rows)} 笔")
        for r in rows:
            flag = "  ⚠ 文档自称已完成" if r["verdict"] in BLOCK and r.get("doc_says") == "done" else ""
            print(f"[{r['verdict']}] {r['id']}  {r['source'] or ''}{flag}")
            if r.get("claim"):
                print(f"    {r['claim']}")
            for x in r["reasons"][:4]:
                print(f"    - {x}")
    n_block = sum(1 for r in res["rows"] if r["verdict"] in block_set) + (res["tests"] == "failed")
    print()
    if res["gate"] == "block":
        print(f"门禁：✗ 阻断（{n_block} 项）")
    elif res["gate"] == "review":
        print(f"门禁：~ 不阻断，但不是「全部通过」：待复核 {len(review_rows)} 项，没查 {len(res['not_checked'])} 项")
    else:
        print("门禁：✓ 通过（代码层承诺全部有凭证且核验通过，测试已跑且通过）")


def lint(debits, vouchers):
    return validate(debits, vouchers)


# ---------------------------------------------------------------- 反向对账

TS_EXPORT = re.compile(r"^export\s+(?:default\s+)?(?:declare\s+)?(?:async\s+)?"
                       r"(?:function\s*\*?\s*|const\s+enum\s+|const\s+|let\s+|var\s+|class\s+|enum\s+|abstract\s+class\s+)"
                       r"([A-Za-z_$][\w$]*)")
TS_LOCAL_EXPORT = re.compile(r"^export\s*\{([^}]*)\}\s*;?\s*$")
TS_REEXPORT = re.compile(r"^export\s*(?:type\s+)?\{([^}]*)\}\s*from\b")
PY_DEF = re.compile(r"^(?:async\s+)?(?:def|class)\s+([A-Za-z_][\w]*)")
WORD = re.compile(r"[A-Za-z_$][\w$]*")


def _export_items(body):
    for item in body.split(","):
        parts = item.strip().split()
        if not parts:
            continue
        if parts[0] == "type" and len(parts) > 1:
            parts = parts[1:]
        if len(parts) == 3 and parts[1] == "as":
            yield parts[0], parts[2]
        else:
            yield parts[0], parts[0]


def orphans(root, cfg):
    t = Tree(root, cfg)
    roots = tuple(cfg["orphan_roots"])
    defs, alias = [], {}
    for rel in t.code_files():
        src = t.src(rel)
        py = rel.endswith(".py")
        for i, ln in enumerate(src.bare):
            if not py:
                m = TS_REEXPORT.match(ln)
                if m:  # export { impl as name } from './x'：别处用 name 也算用到了 impl
                    for orig, name in _export_items(m.group(1)):
                        if orig != name:
                            alias.setdefault(name, set()).add(orig)
        if t.is_test(rel) or t.is_ops(rel) or (roots and not rel.startswith(roots)):
            continue  # 测试和运维脚本里的定义不是「生产能力」
        local = {}
        for i, ln in enumerate(src.bare):
            if py:
                m = PY_DEF.match(ln)
                if m and not m.group(1).startswith("_"):
                    defs.append((rel, i, m.group(1)))
                continue
            m = TS_EXPORT.match(ln)
            if m:
                defs.append((rel, i, m.group(1)))
                continue
            m = re.match(r"^(?:async\s+)?(?:function\s*\*?\s*|const\s+|let\s+|var\s+|class\s+)([A-Za-z_$][\w$]*)", ln)
            if m:
                local[m.group(1)] = i
            m = TS_LOCAL_EXPORT.match(ln)
            if m:  # function x() {} ... export { x, y as z }
                for orig, name in _export_items(m.group(1)):
                    if orig in local:
                        defs.append((rel, local[orig], orig))
                        if orig != name:
                            alias.setdefault(name, set()).add(orig)
    index = {}
    for rel in t.code_files():
        kind = "test" if t.is_test(rel) else ("ops" if t.is_ops(rel) else "prod")
        src = t.src(rel)
        for i, ln in enumerate(src.bare):
            if i in src.imports or (not rel.endswith(".py") and TS_LOCAL_EXPORT.match(ln)):
                continue
            for w in set(WORD.findall(ln)):
                for target in {w} | alias.get(w, set()):
                    index.setdefault(target, []).append((rel, i, kind))
    ignore = [re.compile(p) for p in cfg["orphan_ignore"]]
    found, hooks, seen = [], 0, set()
    for rel, i, sym in defs:
        if (rel, sym) in seen:
            continue
        seen.add((rel, sym))
        lo, hi = body_range(rel, t.src(rel).bare, i, cfg["window"]["body"])
        refs = [(f, l, k) for f, l, k in index.get(sym, []) if not (f == rel and lo <= l < hi)]
        if any(k == "prod" for _, _, k in refs):
            continue
        if any(rx.search(sym) for rx in ignore):
            hooks += 1
            continue
        kinds = {k for _, _, k in refs}
        cat = "ops_only" if "ops" in kinds else ("tested_unwired" if "test" in kinds else "dead")
        found.append({"symbol": sym, "file": rel, "line": i + 1, "category": cat,
                      "test_refs": sum(1 for _, _, k in refs if k == "test"),
                      "ops_refs": sum(1 for _, _, k in refs if k == "ops")})
    order = {"tested_unwired": 0, "dead": 1, "ops_only": 2}
    found.sort(key=lambda x: (order[x["category"]], x["file"], x["line"]))
    return {"version": VERSION, "exported_defs": len(seen), "test_hooks_filtered": hooks, "orphans": found}


ORPHAN_TITLE = {
    "tested_unwired": "有实现、有测试、生产零调用（半落地的典型样子，优先看）",
    "dead": "没有任何引用",
    "ops_only": "只被运维脚本引用",
}


def print_orphans(res):
    print(f"反向对账（贷方无借方）· 导出定义 {res['exported_defs']} 个，生产零引用 "
          f"{len(res['orphans']) + res['test_hooks_filtered']} 个"
          f"（其中按名字识别为测试钩子 {res['test_hooks_filtered']} 个，不列出）")
    for cat in ("tested_unwired", "dead", "ops_only"):
        items = [o for o in res["orphans"] if o["category"] == cat]
        if items:
            print(f"\n{ORPHAN_TITLE[cat]}：{len(items)}")
            for o in items:
                print(f"  {o['symbol']:<32} {o['file']}:{o['line']}")
    print("\n局限：只看符号级引用，看不到分支级死代码；CommonJS 导出、一行声明多个名字、"
          "只被配置文件（如 pyproject 入口）引用的函数会误报或漏报。")


# ---------------------------------------------------------------- CLI

def build_parser():
    ap = argparse.ArgumentParser(prog="landing.py", description="设计决策 → 代码的落地对账。")
    ap.add_argument("--version", action="version", version=VERSION)
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("lint", help="检查借方/凭证格式，提示接线约束过弱的借方")
    p.add_argument("--debits", nargs="+", required=True)
    p.add_argument("--vouchers", nargs="+", default=[])
    p = sub.add_parser("reconcile", help="试算 + 门禁")
    p.add_argument("root", help="代码快照根目录（凭证里的路径相对于它）")
    p.add_argument("--debits", nargs="+", required=True)
    p.add_argument("--vouchers", nargs="+", required=True)
    p.add_argument("--tests", choices=TESTS_STATES, default="not-run",
                   help="本期测试是否真跑过且通过；不给就是没查")
    p.add_argument("--strict", action="store_true", help="待复核项也阻断")
    p.add_argument("--config")
    p.add_argument("--json", dest="json_out")
    p = sub.add_parser("orphans", help="反向对账：生产代码零引用的导出符号")
    p.add_argument("root")
    p.add_argument("--config")
    p.add_argument("--json", dest="json_out")
    return ap


def write_json(path, obj):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=1)


def run(a):
    if a.cmd == "lint":
        debits = load_json_list(a.debits, "借方")
        vouchers = load_json_list(a.vouchers, "凭证")
        errors, warns = lint(debits, vouchers)
        for e in errors:
            print("✗ " + e)
        for w in warns:
            print("⚠ " + w)
        n_cc = sum(1 for d in debits if d.get("code_checkable") is True)
        print(f"借方 {len(debits)} 笔（代码层 {n_cc}），凭证 {len(vouchers)} 张："
              f"{len(errors)} 个错误，{len(warns)} 个提示")
        return 1 if errors else 0
    cfg = load_config(a.config)
    if a.cmd == "reconcile":
        res = reconcile(a.root, load_json_list(a.debits, "借方"), load_json_list(a.vouchers, "凭证"),
                        cfg, tests=a.tests, strict=a.strict)
        print_reconcile(res)
        if a.json_out:
            write_json(a.json_out, res)
        return {"pass": 0, "review": 3, "block": 1}[res["gate"]]
    res = orphans(a.root, cfg)
    print_orphans(res)
    if a.json_out:
        write_json(a.json_out, res)
    return 0


def main(argv=None):
    ap = build_parser()
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 2
    try:
        return run(a)
    except UsageError as e:
        print(f"用法错误：{e}", file=sys.stderr)
        return 2
    except Exception as e:  # noqa: BLE001 —— 工具自己崩了不能和「阻断」同一个退出码
        print(f"内部错误（不是门禁结论）：{type(e).__name__}: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
