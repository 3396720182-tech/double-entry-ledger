#!/usr/bin/env python3
"""
ledger.py — 面向智能体 harness 的复式记账账本（仅依赖标准库，Python 3.8+）

一份 append-only、哈希链式的 JSONL 日记账，承载三本账：
  1. 状态账   state:*       每次状态变更 = 一笔借贷平衡的分录（有借必有贷，借贷必相等）
  2. 意图账   clearing:*    计划(借) vs 实际(贷)；实际值由代码探针取数，残差即异常
  3. 资源账   budget:*      拨款 / 花费 / 退回；结账时子账户必须归零
以及：挂账 suspense:*、外部对账 reconcile、结账 close、红字冲销 reverse。

记账符号约定：金额带符号，正数 = 借（流入该科目），负数 = 贷（流出）。
每条记录内，同一计量单位（unit）的金额之和必须为 0。

退出码：0 = 通过；1 = 检查未通过（规则拒绝、对账不平、意图不符、结账被拒）；
        2 = 用法错误 / 日记账损坏 / IO 错误 / 内部错误。

运行 `python3 ledger.py --help` 或 `python3 ledger.py <命令> --help` 查看用法。
"""
from __future__ import annotations

import argparse
import contextlib
import copy
import datetime
import decimal
import fnmatch
import functools
import glob
import hashlib
import hmac
import json
import os
import re
import sys
from decimal import Decimal
from pathlib import Path

try:
    import fcntl  # POSIX
except ImportError:  # pragma: no cover
    fcntl = None
try:
    import msvcrt  # Windows
except ImportError:
    msvcrt = None

VERSION = "0.2.0"
SCHEMA = 1
GENESIS = "0" * 64
DELTA_UNIT = "delta"

# 系统科目：只能由 intent / reconcile / clear-suspense 等专用命令写入，post 不能碰
RESERVED_PREFIXES = ("clearing:", "intent:", "suspense:")
RESERVED_UNITS = (DELTA_UNIT,)
# 来源科目：新实体「凭空出现 / 消失」的对方科目（建任务、注资）
SOURCE_PREFIXES = ("world:", "equity:")

DEFAULT_RULES = [
    {"match": "state:*", "min": 0},
    # 预算超支：真实花费不能拒记（钱已经花了），记下并标红，结账前必须处理
    {"match": "budget:*", "min": 0, "mode": "flag"},
    {"match": "clearing:*", "zero_at_close": True},
    {"match": "suspense:*", "zero_at_close": True},
    {"match": "budget:agent:*", "zero_at_close": True},
]

DEFAULT_CONFIG = {
    "default_unit": "count",
    # 追加在 DEFAULT_RULES 之后；默认规则不能被悄悄去掉，除非显式 replace_default_rules
    "rules": [],
    "replace_default_rules": False,
    # 受保护科目：状态迁移只能经 intent --then（核实后）过账；直接 post 须 --direct 写明原因
    "guarded": ["state:*"],
    # 外部对账：{"account": "state:done", "unit": "count", "probe": "glob-count:tasks/done/*.md"}
    "reconcile": [],
    # 按工具名给出期望模板，如 {"mv": ["exists:{dst} =1", "exists:{src} =0"]}；模型只填参数
    "expect_templates": {},
    "require_expect_template": False,
    # true：claim 观测必须带有效 HMAC 签名（harness 持有密钥）才算数
    "reject_self_certified": False,
    "observer_key_env": "LEDGER_OBSERVER_KEY",
    "require_reconcile_before_close": False,
    # 崩溃留下的不完整末行（从未确认写入）在下次追加时自动截掉（先备份）
    "auto_repair_torn_tail": True,
    "root": None,
    "allow_outside_root": False,
}

OBS_SOURCES = ("tool", "observer", "agent")
FINAL_INTENT = ("settled", "written_off", "cancelled")
NUMERIC_PROBES = ("exists", "glob-count", "lines", "bytes", "json")
KNOWN_TYPES = ("txn", "intent_open", "intent_observe", "intent_settle", "intent_writeoff",
               "intent_cancel", "suspense_clear", "reconcile", "close")
UNIT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.\-]*$")
NUM_RE = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$", re.ASCII)

# 精确算术：输入位数受限 + 高精度上下文 + 舍入即报错，杜绝「差 1 却被舍入成平」
EXACT = decimal.Context(prec=200, Emax=999999, Emin=-999999,
                        traps=[decimal.Inexact, decimal.Overflow, decimal.InvalidOperation,
                               decimal.DivisionByZero])
MAX_INT_DIGITS, MAX_FRAC_DIGITS = 30, 18


class LedgerError(Exception):
    """用法错误 / 日记账损坏 / IO（退出码 2）"""
    exit_code = 2


class ProbeUnavailable(LedgerError):
    """探针目标暂不存在（如 json 报告尚未生成）"""


class CheckFailed(LedgerError):
    """业务检查未通过（退出码 1）"""
    exit_code = 1

    def __init__(self, message, details=None):
        super().__init__(message)
        self.details = details or []


def _exact(fn):
    @functools.wraps(fn)
    def wrapper(*a, **k):
        with decimal.localcontext(EXACT):
            return fn(*a, **k)
    return wrapper


# ---------------------------------------------------------------- 数值与校验

def to_dec(x) -> Decimal:
    if isinstance(x, float):
        raise LedgerError(f"金额不接受 float（会引入舍入误差），请传 int/str/Decimal：{x!r}")
    if isinstance(x, Decimal):
        d = x
    else:
        s = str(x).strip()
        if len(s) > 80 or not NUM_RE.match(s):
            raise LedgerError(f"无法解析的数值：{x!r}（仅接受 ASCII 十进制，如 12、-3.5、1E+3）")
        d = Decimal(s)
    if not d.is_finite():
        raise LedgerError(f"数值必须有限：{x!r}")
    if d != 0:
        if not -MAX_FRAC_DIGITS <= d.adjusted() < MAX_INT_DIGITS:
            raise LedgerError(f"数值超出范围：{x!r}（整数位 ≤ {MAX_INT_DIGITS}，小数位 ≤ {MAX_FRAC_DIGITS}）")
        n = d.normalize(context=decimal.Context(prec=400, Emax=999999, Emin=-999999))  # 去掉尾随 0 再量位数
        if n.as_tuple().exponent < -MAX_FRAC_DIGITS:
            raise LedgerError(f"数值超出范围：{x!r}（整数位 ≤ {MAX_INT_DIGITS}，小数位 ≤ {MAX_FRAC_DIGITS}）")
    return d


def dfmt(d: Decimal) -> str:
    s = format(d, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return "0" if s in ("-0", "", "-") else s


def check_account(name: str) -> str:
    if not isinstance(name, str) or not name or any(c.isspace() for c in name) or name.startswith("-"):
        raise LedgerError(f"非法科目名：{name!r}（不能为空、含空白或以 - 开头）")
    if any(c in name for c in "*?[]"):
        raise LedgerError(f"科目名不能含通配符：{name!r}")
    try:
        name.encode("utf-8")
    except UnicodeEncodeError:
        raise LedgerError(f"科目名含非法 Unicode：{name!r}")
    return name


def check_unit(unit: str) -> str:
    if not isinstance(unit, str) or not UNIT_RE.match(unit):
        raise LedgerError(f"非法计量单位：{unit!r}（字母/下划线开头，仅含字母数字 _ . -）")
    return unit


def check_id(i: str, what="id") -> str:
    if not isinstance(i, str) or not i or any(c.isspace() or c in "/*?[]" for c in i):
        raise LedgerError(f"非法 {what}：{i!r}（不能为空、含空白、/ 或通配符）")
    return i


def norm_posting(p, default_unit) -> dict:
    """接受 'ACCOUNT AMOUNT [UNIT]' 字符串或 dict，返回规范化分录行。"""
    if isinstance(p, str):
        parts = p.split()
        if len(parts) not in (2, 3):
            raise LedgerError(f"分录格式应为 'ACCOUNT AMOUNT [UNIT]'：{p!r}")
        p = {"account": parts[0], "amount": parts[1], "unit": parts[2] if len(parts) == 3 else default_unit}
    elif isinstance(p, dict):
        p = {"account": p.get("account"), "amount": p.get("amount"), "unit": p.get("unit") or default_unit}
    else:
        raise LedgerError(f"无法识别的分录：{p!r}")
    amt = to_dec(p["amount"])
    return {"account": check_account(p["account"]), "amount": dfmt(amt), "unit": check_unit(p["unit"])}


def is_reserved(p) -> bool:
    return p["account"].startswith(RESERVED_PREFIXES) or p["unit"] in RESERVED_UNITS


def check_balanced(postings, require_lines=True):
    """有借必有贷，借贷必相等：每个计量单位的金额之和为 0。"""
    if not isinstance(postings, list):
        raise LedgerError("postings 必须是列表")
    if require_lines and len(postings) < 2:
        raise CheckFailed("有借必有贷：一笔分录至少需要两行")
    sums = {}
    for p in postings:
        if not isinstance(p, dict):
            raise LedgerError(f"分录行必须是对象：{p!r}")
        amt = to_dec(p["amount"])
        if require_lines and amt == 0:
            raise CheckFailed(f"分录行金额不能为 0：{p['account']}")
        sums[p["unit"]] = sums.get(p["unit"], Decimal(0)) + amt
    bad = {u: dfmt(s) for u, s in sums.items() if s != 0}
    if bad:
        raise CheckFailed(f"借贷不平：各单位差额 {bad}")


def canon(obj) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def rec_hash(rec: dict) -> str:
    body = {k: v for k, v in rec.items() if k != "hash"}
    return hashlib.sha256(canon(body).encode("utf-8", "surrogatepass")).hexdigest()


def now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="milliseconds")


def postings_fingerprint(postings):
    return sorted((p["account"], p["unit"], dfmt(to_dec(p["amount"]))) for p in postings)


def bal_key(a, u):
    return f"{a} [{u}]"


def parse_bal_key(k):
    a, _, u = k.rpartition(" [")
    return a, u[:-1]


def _check_text(s, what):
    if s is None:
        return
    try:
        canon(s).encode("utf-8")
    except UnicodeEncodeError:
        raise LedgerError(f"{what} 含非法 Unicode（孤立代理字符）")
    except ValueError:
        raise LedgerError(f"{what} 含 NaN/Infinity，不是标准 JSON")


# ---------------------------------------------------------------- 回放状态

class State:
    """按顺序回放日记账得到的当前状态。"""

    def __init__(self):
        self.balances = {}          # (account, unit) -> Decimal
        self.txns = {}              # txn id -> record
        self.reversed = {}          # 被冲销的 txn id -> 冲销分录 id
        self.intents = {}           # intent id -> dict
        self.closes = []            # close records
        self.last_posting_seq = 0
        self.last_reconcile = None
        self.seq = 0
        self.head = GENESIS

    def bal(self, account, unit) -> Decimal:
        return self.balances.get((account, unit), Decimal(0))

    def apply(self, rec):
        self.seq = rec["seq"]
        self.head = rec["hash"]
        postings = rec.get("postings") or []
        for p in postings:
            k = (p["account"], p["unit"])
            v = self.balances.get(k, Decimal(0)) + to_dec(p["amount"])
            if v == 0:
                self.balances.pop(k, None)
            else:
                self.balances[k] = v
        if postings:
            self.last_posting_seq = rec["seq"]
        t = rec["type"]
        if t == "txn":
            self.txns[rec["id"]] = rec
            if rec.get("reverses"):
                self.reversed[rec["reverses"]] = rec["id"]
        elif t == "intent_open":
            self.intents[rec["intent"]] = {
                "open": rec, "status": "open", "observations": {},
                "credited": [Decimal(0)] * len(rec["expects"]), "last_settle": None,
            }
        elif t == "intent_observe":
            self.intents[rec["intent"]]["observations"].setdefault(rec["metric"], []).append(rec)
        elif t == "intent_settle":
            it = self.intents[rec["intent"]]
            for r in rec["results"]:
                if r.get("credited") is not None:
                    it["credited"][r["index"]] = to_dec(r["credited"])
            it["status"] = rec["status"]
            it["last_settle"] = rec
        elif t in ("intent_writeoff", "intent_cancel"):
            it = self.intents[rec["intent"]]
            it["status"] = "written_off" if t == "intent_writeoff" else "cancelled"
            it["final"] = rec
        elif t == "reconcile":
            self.last_reconcile = rec
        elif t == "close":
            self.closes.append(rec)


# ---------------------------------------------------------------- 探针

class Prober:
    """只读、确定性的取数器。真实世界的值由代码读取，不由模型声称。"""

    def __init__(self, root: Path, allow_outside_root=False):
        self.root = root.resolve()
        self.allow_outside = allow_outside_root

    def norm_target(self, arg) -> str:
        """探针目标的规范形式：相对根目录拼接、消去 . 与 ..、在大小写不敏感的文件系统上折叠大小写。"""
        p = os.path.normpath(os.path.join(str(self.root), arg))
        if self._case_insensitive():
            p = p.casefold()
        return p

    def _case_insensitive(self):
        if not hasattr(self, "_ci"):
            r = str(self.root)
            flipped = r.swapcase()
            self._ci = flipped != r and os.path.exists(flipped) and os.path.samefile(r, flipped)
        return self._ci

    def overlap(self, a, b) -> bool:
        ta, tb = probe_target(a), probe_target(b)
        if ta is None or tb is None:
            return False
        ta, tb = self.norm_target(ta), self.norm_target(tb)
        if glob.has_magic(ta) or glob.has_magic(tb):
            pa, pb = _static_prefix(ta), _static_prefix(tb)
            return (_is_parent(pa, pb) or _is_parent(pb, pa)
                    or fnmatch.fnmatchcase(ta, tb) or fnmatch.fnmatchcase(tb, ta))
        return _is_parent(ta, tb) or _is_parent(tb, ta)

    def _inside(self, p: Path) -> bool:
        rp = p.resolve()
        return rp == self.root or self.root in rp.parents

    def _path(self, arg) -> Path:
        if not arg:
            raise LedgerError("探针缺少路径参数")
        p = Path(arg)
        p = p if p.is_absolute() else self.root / p
        if not self.allow_outside and not self._inside(p):
            raise LedgerError(f"探针路径越出根目录 {self.root}：{arg}（如确需，请在配置中设 allow_outside_root）")
        return p

    def measure(self, spec: str):
        try:
            return self._measure(spec)
        except LedgerError:
            raise
        except (OSError, RuntimeError, ValueError) as e:
            raise LedgerError(f"探针 {spec} 执行失败：{e}")

    def _measure(self, spec):
        kind, sep, arg = spec.partition(":")
        if not sep:
            raise LedgerError(f"探针格式应为 KIND:ARG：{spec!r}")
        if kind == "exists":
            return Decimal(1 if self._path(arg).exists() else 0)
        if kind == "glob-count":
            hits = glob.glob(str(self._path(arg)), recursive=True)
            if not self.allow_outside:
                hits = [h for h in hits if self._inside(Path(h))]
            return Decimal(len(hits))
        if kind == "lines":
            p = self._path(arg)
            if not p.exists():
                return Decimal(0)
            with open(p, "rb") as f:
                return Decimal(sum(1 for _ in f))
        if kind == "bytes":
            p = self._path(arg)
            return Decimal(p.stat().st_size if p.exists() else 0)
        if kind == "json":
            path, _, keypath = arg.partition("#")
            p = self._path(path)
            if not p.exists():
                raise ProbeUnavailable(f"json 探针文件不存在：{path}")
            with open(p, "r", encoding="utf-8") as f:
                try:
                    data = json.load(f)
                except ValueError as e:
                    raise LedgerError(f"json 探针解析失败 {path}：{e}")
            for k in [k for k in keypath.split(".") if k]:
                if isinstance(data, list):
                    try:
                        data = data[int(k)]
                    except (ValueError, IndexError):
                        raise ProbeUnavailable(f"json 探针路径不存在：{arg}")
                elif isinstance(data, dict) and k in data:
                    data = data[k]
                else:
                    raise ProbeUnavailable(f"json 探针路径不存在：{arg}")
            if isinstance(data, bool) or not isinstance(data, (int, float, str)):
                raise LedgerError(f"json 探针取到的不是数值：{arg}")
            return to_dec(repr(data) if isinstance(data, float) else str(data))
        if kind == "sha256":
            p = self._path(arg)
            if not p.exists():
                return ""
            h = hashlib.sha256()
            with open(p, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 16), b""):
                    h.update(chunk)
            return h.hexdigest()
        raise LedgerError(f"未知探针类型：{kind}（可用：{', '.join(NUMERIC_PROBES)}, sha256, claim）")


def parse_expect(s: str) -> dict:
    """'PROBE OP'。OP：+N / -N（变化量）、=N（目标绝对值）；sha256 用 changed / unchanged。"""
    s = (s or "").strip()
    probe, _, op = s.rpartition(" ")
    probe = probe.strip()
    if not probe:
        raise LedgerError(f"期望格式应为 'PROBE OP'：{s!r}")
    kind = probe.partition(":")[0]
    if kind == "sha256":
        if op not in ("changed", "unchanged"):
            raise LedgerError(f"sha256 探针的 OP 只能是 changed/unchanged：{s!r}")
        return {"probe": probe, "op": op}
    if kind == "claim":
        name = probe.partition(":")[2]
        if not name or not UNIT_RE.match(name):
            raise LedgerError(f"claim 名称非法：{probe!r}")
    elif kind not in NUMERIC_PROBES:
        raise LedgerError(f"未知探针类型：{kind}")
    if not op or op[0] not in "+-=":
        raise LedgerError(f"OP 应为 +N / -N / =N：{s!r}")
    to_dec(op[1:] if op[0] in "+=" else op)
    return {"probe": probe, "op": op}


def probe_target(probe):
    kind, _, arg = probe.partition(":")
    if kind == "claim":
        return None
    return arg.partition("#")[0] if kind == "json" else arg


def _is_parent(a, b):
    return b == a or b.startswith(a.rstrip(os.sep) + os.sep)


def _static_prefix(path):
    parts = []
    for part in path.split(os.sep):
        if glob.has_magic(part):
            break
        parts.append(part)
    return os.sep.join(parts) or os.sep


def obs_sig(key: str, intent, metric, value) -> str:
    msg = "\x1f".join([intent, metric, value]).encode("utf-8")
    return hmac.new(key.encode("utf-8"), msg, hashlib.sha256).hexdigest()


# ---------------------------------------------------------------- 账本

class Ledger:
    def __init__(self, journal=None, config=None):
        self.journal = Path(journal or os.environ.get("LEDGER_JOURNAL") or ".ledger/journal.jsonl")
        self.config, cfg_dir = self._load_config(config)
        self.rules = ([] if self.config["replace_default_rules"] else copy.deepcopy(DEFAULT_RULES)) + self.config["rules"]
        root = self.config.get("root")
        if root:
            root = Path(root)
            self.root = root if root.is_absolute() else (cfg_dir / root)
        else:
            self.root = self.journal.resolve().parent.parent
        self.prober = Prober(self.root, bool(self.config.get("allow_outside_root")))
        self.default_unit = check_unit(self.config.get("default_unit") or "count")
        self.cfg_hash = hashlib.sha256(canon(self.config).encode("utf-8")).hexdigest()[:16]
        self.notices = []
        self._cache = None  # 进程内增量缓存：{"size", "tail", "records", "state"}

    # ---- 配置 / IO

    def _load_config(self, config):
        cfg = copy.deepcopy(DEFAULT_CONFIG)
        cfg_dir = self.journal.resolve().parent
        user = None
        if isinstance(config, dict):
            user, cfg_dir = config, Path.cwd()
        else:
            path = config or os.environ.get("LEDGER_CONFIG")
            if not path:
                cand = self.journal.parent / "config.json"
                path = cand if cand.exists() else None
            if path:
                path = Path(path)
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        user = json.load(f)
                except (OSError, ValueError) as e:
                    raise LedgerError(f"配置文件读取失败 {path}：{e}")
                cfg_dir = path.resolve().parent
        if user is not None:
            if not isinstance(user, dict):
                raise LedgerError("配置顶层必须是对象")
            unknown = set(user) - set(DEFAULT_CONFIG)
            if unknown:
                raise LedgerError(f"配置含未知字段：{sorted(unknown)}（拼写错误会让规则悄悄失效）")
            cfg.update(user)
        types = {"rules": list, "guarded": list, "reconcile": list, "expect_templates": dict}
        for k, t in types.items():
            if not isinstance(cfg[k], t):
                raise LedgerError(f"配置 {k} 必须是 {t.__name__}")
        if not all(isinstance(g, str) for g in cfg["guarded"]):
            raise LedgerError("配置 guarded 必须是字符串列表")
        for item in cfg["reconcile"]:
            if not isinstance(item, dict) or not {"account", "probe"} <= set(item):
                raise LedgerError(f"reconcile 项需要 account 与 probe：{item}")
        for tool, tpl in cfg["expect_templates"].items():
            if isinstance(tpl, dict):
                if set(tpl) - {"expect", "then"} or not isinstance(tpl.get("expect", []), list) \
                        or not isinstance(tpl.get("then", []), list):
                    raise LedgerError(f"expect_templates.{tool} 应为列表或 {{expect: [...], then: [...]}}")
            elif not isinstance(tpl, list):
                raise LedgerError(f"expect_templates.{tool} 应为列表或 {{expect: [...], then: [...]}}")
        for r in cfg["rules"]:
            if not isinstance(r, dict) or "match" not in r:
                raise LedgerError(f"规则缺少 match：{r}")
            if r.get("mode", "reject") not in ("reject", "flag"):
                raise LedgerError(f"规则 mode 只能是 reject/flag：{r}")
        return cfg, cfg_dir

    def _read(self):
        """返回 (records, problems)。"""
        self._last_read = b""
        if not self.journal.exists():
            return [], []
        data = self.journal.read_bytes()
        self._last_read = data
        lines = data.split(b"\n")
        records, problems = [], []
        for i, raw in enumerate(lines[:-1], start=1):
            try:
                rec = json.loads(raw.decode("utf-8"))
                if not isinstance(rec, dict):
                    raise ValueError("not an object")
                records.append(rec)
            except (ValueError, UnicodeDecodeError):
                problems.append({"kind": "corrupt_line", "line": i,
                                 "msg": f"第 {i} 行不是合法 JSON 记录（被手改或磁盘损坏）"})
        if lines[-1]:
            problems.append({"kind": "torn_tail", "line": len(lines),
                             "msg": "末行不完整（写入中途崩溃，该记录从未被确认）"})
        return records, problems

    @staticmethod
    def _chain_problems(records):
        out, prev = [], GENESIS
        for idx, r in enumerate(records, start=1):
            where = f"seq {r.get('seq', '?')}"
            if r.get("seq") != idx:
                out.append(f"{where}: 序号不连续（期望 {idx}）——记录被删除、插入或重排")
            if r.get("prev") != prev:
                out.append(f"{where}: prev 哈希断链——前面的记录被改动或删除")
            try:
                ok = r.get("hash") == rec_hash(r)
            except (TypeError, ValueError):
                ok = False
            if not ok:
                out.append(f"{where}: 哈希不符——该记录被改动")
            prev = r.get("hash")
        return out

    @contextlib.contextmanager
    def _locked(self):
        self.journal.parent.mkdir(parents=True, exist_ok=True)
        # 用真实路径加锁：经符号链接访问同一日记账也会争同一把锁
        fd = os.open(os.path.realpath(str(self.journal)) + ".lock", os.O_RDWR | os.O_CREAT, 0o644)
        try:
            if fcntl:
                fcntl.flock(fd, fcntl.LOCK_EX)
            elif msvcrt:  # pragma: no cover
                msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
            yield
        finally:
            if fcntl:
                fcntl.flock(fd, fcntl.LOCK_UN)
            elif msvcrt:  # pragma: no cover
                with contextlib.suppress(OSError):
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            os.close(fd)

    def _replay(self, records):
        st = State()
        for r in records:
            try:
                st.apply(r)
            except Exception as e:  # noqa: BLE001  任何结构异常都视为账本损坏
                raise LedgerError(f"seq {r.get('seq', '?')} 记录结构异常，日记账已损坏：{e!r}")
        return st

    def _from_cache(self):
        """已缓存部分的字节摘要不变（只追加、没被改）时，只解析、校验新增的记录；否则返回 None 走全量。
        字节摘要比逐条 JSON 解析 + 记录哈希便宜一个数量级，且同样能发现对旧记录的任何改动。"""
        c = self._cache
        if not c:
            return None
        try:
            data = self.journal.read_bytes()
        except OSError:
            return None
        if len(data) < c["size"] or hashlib.sha256(data[:c["size"]]).digest() != c["digest"]:
            return None
        new = data[c["size"]:]
        if not new:
            return c["records"], c["state"]
        if not new.endswith(b"\n"):
            return None  # 不完整末行：交给全量路径处理
        recs = []
        try:
            for raw in new.split(b"\n")[:-1]:
                r = json.loads(raw.decode("utf-8"))
                if not isinstance(r, dict):
                    return None
                recs.append(r)
        except (ValueError, UnicodeDecodeError):
            return None
        st = c["state"]
        prev, seq = st.head, st.seq
        for r in recs:
            seq += 1
            if r.get("seq") != seq or r.get("prev") != prev or r.get("hash") != rec_hash(r):
                return None
            prev = r["hash"]
        for r in recs:
            st.apply(r)
        c["records"].extend(recs)
        c["size"], c["digest"] = len(data), hashlib.sha256(data).digest()
        return c["records"], st

    def _load_state(self, for_append=False):
        try:
            hit = self._from_cache()
        except Exception:  # noqa: BLE001  缓存路径有任何异常都退回全量重放
            self._cache, hit = None, None
        if hit:
            return hit
        self._cache = None
        records, problems = self._read()
        if problems and for_append and self.config.get("auto_repair_torn_tail") \
                and {p["kind"] for p in problems} == {"torn_tail"}:
            info = self._drop_torn_tail()
            self.notices.append(f"已自动截掉崩溃留下的不完整末行 {info['dropped_bytes']} 字节，备份：{info['backup']}")
            records, problems = self._read()
        if problems:
            raise LedgerError("日记账已损坏，拒绝继续：" + "；".join(p["msg"] for p in problems)
                              + "（运行 verify 查看；仅末行不完整时可 repair --drop-torn-tail）")
        chain = self._chain_problems(records)
        if chain:
            raise LedgerError("日记账哈希链校验失败，拒绝继续：" + "；".join(chain[:5]))
        st = self._replay(records)
        data = self._last_read
        if records and data.endswith(b"\n"):
            self._cache = {"size": len(data), "digest": hashlib.sha256(data).digest(), "records": records, "state": st}
        return records, st

    def _append(self, build):
        """在锁内：读取→校验链→回放→build(state) 生成记录→校验→追加。"""
        with self._locked():
            records, st = self._load_state(for_append=True)
            rec = build(st)
            if rec is None or rec.get("_duplicate"):
                return rec
            postings = rec.get("postings") or []
            for p in postings:
                norm_posting(p, self.default_unit)  # 防御：系统生成的科目名也必须合法
            if postings:
                check_balanced(postings, require_lines=False)
                self._check_rules(st, postings)
            rec["v"] = SCHEMA
            rec["cfg"] = self.cfg_hash
            rec.setdefault("ts", now_iso())
            rec["seq"] = st.seq + 1
            rec["prev"] = st.head
            rec["hash"] = rec_hash(rec)
            line = (canon(rec) + "\n").encode("utf-8")
            fd = os.open(str(self.journal), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
            try:
                view = memoryview(line)
                while view:
                    n = os.write(fd, view)
                    view = view[n:]
                os.fsync(fd)
            finally:
                os.close(fd)
            # 缓存保持写入前的状态：下次读取时，自己刚写的记录和其他进程的写入走同一条增量校验路径
            return rec

    def _rules_for(self, account, unit):
        for r in self.rules:
            if fnmatch.fnmatchcase(account, r["match"]) and (not r.get("unit") or fnmatch.fnmatchcase(unit, r["unit"])):
                yield r

    def _rule_violations(self, items, mode=None):
        out = []
        for (acct, unit), bal in items:
            for r in self._rules_for(acct, unit):
                if mode and r.get("mode", "reject") != mode:
                    continue
                if "min" in r and bal < to_dec(r["min"]):
                    out.append(f"{acct} [{unit}] 余额 {dfmt(bal)} 低于下限 {r['min']}（规则 {r['match']}）")
                if "max" in r and bal > to_dec(r["max"]):
                    out.append(f"{acct} [{unit}] 余额 {dfmt(bal)} 高于上限 {r['max']}（规则 {r['match']}）")
        return out

    def _touched(self, st, postings):
        touched = {}
        for p in postings:
            k = (p["account"], p["unit"])
            touched[k] = touched.get(k, st.bal(*k)) + to_dec(p["amount"])
        return touched

    def _check_rules(self, st, postings):
        v = self._rule_violations(self._touched(st, postings).items(), mode="reject")
        if v:
            raise CheckFailed("违反科目规则，已拒绝记账：" + "；".join(v), v)

    def _is_guarded(self, account):
        return any(fnmatch.fnmatchcase(account, g) for g in self.config.get("guarded") or [])

    def _base(self, typ, actor, memo, meta):
        _check_text(memo, "memo")
        _check_text(meta, "meta")
        rec = {"type": typ, "actor": actor or os.environ.get("LEDGER_ACTOR") or ""}
        if memo:
            rec["memo"] = memo
        if meta:
            if not isinstance(meta, dict):
                raise LedgerError("meta 必须是对象")
            rec["meta"] = meta
        return rec

    # ---- 1. 记账

    @_exact
    def post(self, txn_id, postings, memo=None, actor=None, meta=None, direct=None, _reverses=None):
        check_id(txn_id, "txn id")
        lines = [norm_posting(p, self.default_unit) for p in postings]
        check_balanced(lines)
        bad = [p["account"] for p in lines if is_reserved(p)]
        if bad:
            raise LedgerError(f"系统科目/单位不能直接记账：{bad}（clearing:/intent:/suspense: 与 delta 单位只能由 "
                              "intent、reconcile、clear-suspense 写入）")
        guarded = [p for p in lines if self._is_guarded(p["account"])]
        others = [p for p in lines if not self._is_guarded(p["account"])]
        # 新建/销毁实体（对方科目全是 world:/equity:）属于声明；在受保护科目之间迁移属于事实，须经核实
        # 新建 = 受保护行全部流入、其余全是来源科目。任何受保护行流出（迁移、销毁）都要经核实或 --direct
        is_creation = (bool(others) and all(p["account"].startswith(SOURCE_PREFIXES) for p in others)
                       and all(to_dec(p["amount"]) > 0 for p in guarded))
        needs_direct = bool(guarded) and not _reverses and not is_creation
        if needs_direct and not direct:
            raise LedgerError(f"受保护科目 {[p['account'] for p in guarded]} 的迁移只能经 intent --then 核实后过账；"
                              "确需直接更正请加 --direct '<原因>'（会在 verify 中标出）")
        _check_text(direct, "direct")

        def build(st):
            if txn_id in st.txns:
                old = st.txns[txn_id]
                if (postings_fingerprint(old["postings"]) == postings_fingerprint(lines)
                        and old.get("reverses") == _reverses):
                    return dict(copy.deepcopy(old), _duplicate=True)  # 幂等：重试同一笔不会重复记账
                raise CheckFailed(f"txn id {txn_id} 已存在且内容不同（拒绝覆盖；更正请用 reverse）")
            if _reverses:
                if _reverses not in st.txns:
                    raise LedgerError(f"找不到要冲销的 txn：{_reverses}")
                if _reverses in st.reversed:
                    raise CheckFailed(f"txn {_reverses} 已被 {st.reversed[_reverses]} 冲销过")
            rec = self._base("txn", actor, memo, meta)
            rec.update({"id": txn_id, "postings": lines})
            if _reverses:
                rec["reverses"] = _reverses
            if needs_direct:
                rec["direct"] = direct
            return rec

        return self._append(build)

    def transfer(self, txn_id, src, dst, amount, unit=None, memo=None, actor=None, meta=None, direct=None):
        with decimal.localcontext(EXACT):
            amt = to_dec(amount)
            if amt <= 0:
                raise LedgerError("transfer 金额必须为正")
            u = unit or self.default_unit
            return self.post(txn_id, [{"account": src, "amount": dfmt(-amt), "unit": u},
                                      {"account": dst, "amount": dfmt(amt), "unit": u}], memo, actor, meta, direct)

    @_exact
    def reverse(self, txn_id, of, reason, actor=None):
        """红字冲销：不改历史，追加一笔方向相反的分录。"""
        if not reason:
            raise LedgerError("冲销必须写明原因（--reason）")
        _, st = self._snapshot()
        orig = st.txns.get(of)
        if not orig:
            raise LedgerError(f"找不到要冲销的 txn：{of}")
        if orig.get("reverses"):
            raise LedgerError("不能冲销一笔冲销分录；如需恢复请重新记一笔")
        neg = [dict(p, amount=dfmt(-to_dec(p["amount"]))) for p in orig["postings"]]
        return self.post(txn_id, neg, memo=f"冲销 {of}：{reason}", actor=actor, _reverses=of)

    # ---- 查询

    def _snapshot(self):
        with self._locked():
            return self._load_state()

    @_exact
    def balances(self, pattern="*"):
        _, st = self._snapshot()
        return {bal_key(a, u): dfmt(v) for (a, u), v in sorted(st.balances.items())
                if fnmatch.fnmatchcase(a, pattern)}

    @_exact
    def trial(self):
        """试算平衡表：各单位借方合计、贷方合计、差额。"""
        records, st = self._snapshot()
        dr, cr = {}, {}
        for (a, u), v in st.balances.items():
            if v > 0:
                dr[u] = dr.get(u, Decimal(0)) + v
            else:
                cr[u] = cr.get(u, Decimal(0)) + v
        units = sorted(set(dr) | set(cr))
        rows = [{"unit": u, "debit": dfmt(dr.get(u, Decimal(0))), "credit": dfmt(-cr.get(u, Decimal(0))),
                 "diff": dfmt(dr.get(u, Decimal(0)) + cr.get(u, Decimal(0)))} for u in units]
        return {"balanced": all(r["diff"] == "0" for r in rows), "units": rows, "records": len(records)}

    # ---- 2. 意图-效果对账（清账科目）

    def _expected_delta(self, e, baseline):
        op = e["op"]
        if op == "changed":
            return Decimal(1)
        if op == "unchanged":
            return Decimal(0)
        n = to_dec(op[1:]) if op[0] in "+=" else to_dec(op)
        if op[0] == "=" and not e["probe"].startswith("claim:"):
            return n - to_dec(baseline)
        return n

    def _trust(self, o):
        """observation 的可信度：verified（有效签名）/ labelled（仅标签）/ self（agent 自报）/ forged（签名无效）"""
        if o["source"] == "agent":
            return "self"
        key = os.environ.get(self.config.get("observer_key_env") or "")
        if o.get("sig") and key:
            ok = hmac.compare_digest(o["sig"], obs_sig(key, o["intent"], o["metric"], o["value"]))
            return "verified" if ok else "forged"
        return "labelled"

    def _claim_trust(self, it, e):
        obs = it["observations"].get(e["probe"][6:]) or []
        if not obs:
            return "missing"
        ts = [self._trust(o) for o in obs]
        return "verified" if "verified" in ts else ts[-1]

    def _actual_delta(self, it, e):
        """返回 (actual, note)。actual=None 表示无法确定（缺观测、探针失败、未通过可信度要求）。"""
        probe = e["probe"]
        if probe.startswith("claim:"):
            obs = it["observations"].get(probe[6:])
            if not obs:
                return None, "缺少观测（claim 尚未 observe）"
            trusts = [(o, self._trust(o)) for o in obs]
            verified = [o for o, t in trusts if t == "verified"]
            chosen = verified[-1] if verified else obs[-1]  # 有签名观测时，后来的未签名观测不能覆盖它
            t = "verified" if verified else self._trust(chosen)
            if self.config.get("reject_self_certified") and t != "verified":
                return None, f"未经签名核实的观测（{t}），严格模式拒绝"
            note = None if t == "verified" else {"self": "自证：由 agent 自报",
                                                  "labelled": "未签名：source 仅为标签",
                                                  "forged": "签名无效"}[t]
            return to_dec(chosen["value"]), note
        try:
            now = self.prober.measure(probe)
        except LedgerError as ex:
            return None, f"探针失败：{ex}"
        if probe.startswith("sha256:"):
            return Decimal(1 if now != e["baseline"] else 0), None
        return now - to_dec(e["baseline"]), None

    def _render_template(self, tool, args):
        """返回 (期望列表, then 列表或 None)。then 为 None 表示模板未声明 then。"""
        tpl = self.config["expect_templates"].get(tool) if tool else None
        if tpl is None:
            if self.config.get("require_expect_template"):
                raise LedgerError(f"工具 {tool or '(未指定)'} 没有期望模板（require_expect_template=true）")
            if args:
                raise LedgerError(f"工具 {tool} 没有期望模板，--arg 无处使用")
            return [], None
        if isinstance(tpl, list):
            tpl = {"expect": tpl}

        class Strict(dict):
            def __missing__(self, k):
                raise LedgerError(f"期望模板需要参数 --arg {k}=...")
        fill = Strict({k: str(v) for k, v in (args or {}).items()})
        then = [t.format_map(fill) for t in tpl["then"]] if "then" in tpl else None
        return [t.format_map(fill) for t in tpl.get("expect", [])], then

    @_exact
    def intent_open(self, intent_id, expects=(), then=(), tool=None, args=None, memo=None, actor=None,
                    meta=None, allow_noop=False, allow_shared_probe=False):
        """开立意图：登记预期效果并抓取基线（预写日志 / WAL）。之后再去调用工具。"""
        check_id(intent_id, "intent id")
        tpl_expects, tpl_then = self._render_template(tool, args)
        if then and (tpl_then is not None or self.config.get("require_expect_template")):
            raise LedgerError(f"工具 {tool} 的状态迁移由模板决定（或要求必须由模板声明），不接受自带的 then")
        if tpl_then is not None:
            then = tpl_then
        raw = tpl_expects + [s if isinstance(s, str) else f"{s['probe']} {s['op']}" for s in expects]
        parsed = [parse_expect(s) for s in raw]
        if not parsed:
            raise LedgerError("意图至少需要一条期望（--expect 或工具模板）")
        then_lines = [norm_posting(p, self.default_unit) for p in then]
        if then_lines:
            check_balanced(then_lines)
            if any(is_reserved(p) for p in then_lines):
                raise LedgerError("then 分录不能涉及系统科目/单位")
        exps = []
        for i, e in enumerate(parsed):
            missing = False
            if e["probe"].startswith("claim:"):
                base = "0"
            else:
                try:
                    m = self.prober.measure(e["probe"])
                    base = m if isinstance(m, str) else dfmt(m)
                except ProbeUnavailable:
                    if not e["op"].startswith("="):
                        raise LedgerError(f"{e['probe']} 当前取不到值，无法作为 {e['op']} 的基线；"
                                          "改用 =N（目标绝对值），或配合 sha256:<文件> changed")
                    base, missing = "0", True  # =N 的判定只看结算时的绝对值，与基线无关
            d = self._expected_delta(e, base)
            x = dict(e, index=i, baseline=base, expected=dfmt(d), vacuous=(d == 0))
            if missing:
                x["baseline_missing"] = True
            exps.append(x)
        if all(x["vacuous"] for x in exps):
            if then_lines:
                raise CheckFailed("所有期望的预期变化都是 0（基线已满足或写了 +0），却挂了 then 分录——"
                                  "这等于不做任何事就更新状态。请写出会真实变化的期望")
            if not allow_noop:
                raise CheckFailed("所有期望的预期变化都是 0，结算必然通过，检查形同虚设。"
                                  "若确实只想确认「没变」，加 --allow-noop")

        def build(st):
            if intent_id in st.intents:
                it = st.intents[intent_id]
                if it["status"] in ("written_off", "cancelled"):
                    raise CheckFailed(f"intent {intent_id} 已{it['status']}，重做请用新的 id")
                old = it["open"]
                same = ([(e["probe"], e["op"]) for e in old["expects"]] == [(e["probe"], e["op"]) for e in exps]
                        and postings_fingerprint(old.get("then", [])) == postings_fingerprint(then_lines))
                if same:
                    return dict(copy.deepcopy(old), _duplicate=True)
                raise CheckFailed(f"intent {intent_id} 已存在且内容不同")
            if not allow_shared_probe:
                clash = []
                for oid, it in st.intents.items():
                    if it["status"] in FINAL_INTENT:
                        continue
                    for a in exps:
                        for b in it["open"]["expects"]:
                            if self.prober.overlap(a["probe"], b["probe"]):
                                clash.append(f"{a['probe']} ↔ {oid}:{b['probe']}")
                if clash:
                    raise CheckFailed("与未结意图共用探针，变化量会互相污染（先结掉对方，或改用指向具体文件的探针；"
                                      "确认无并发时加 --allow-shared-probe）：" + "；".join(clash), clash)
            postings = []
            for x in exps:
                d = to_dec(x["expected"])
                if d != 0:
                    postings += [{"account": f"clearing:intent:{intent_id}:{x['index']}", "amount": dfmt(d), "unit": DELTA_UNIT},
                                 {"account": "intent:planned", "amount": dfmt(-d), "unit": DELTA_UNIT}]
            rec = self._base("intent_open", actor, memo, meta)
            rec.update({"intent": intent_id, "tool": tool or "", "args": args or {}, "expects": exps,
                        "then": then_lines, "postings": postings})
            flags = [f for f, on in (("allow_noop", allow_noop), ("allow_shared_probe", allow_shared_probe)) if on]
            if flags:
                rec["overrides"] = flags
            return rec

        return self._append(build)

    @_exact
    def intent_observe(self, intent_id, metric, value, source, evidence=None, actor=None):
        """为 claim 期望登记实际值。harness 进程持有 observer 密钥时自动签名。"""
        if source not in OBS_SOURCES:
            raise LedgerError(f"source 必须是 {OBS_SOURCES} 之一")
        _check_text(evidence, "evidence")
        val = dfmt(to_dec(value))
        key = os.environ.get(self.config.get("observer_key_env") or "")

        def build(st):
            it = st.intents.get(intent_id)
            if not it:
                raise LedgerError(f"找不到 intent：{intent_id}")
            if it["status"] in FINAL_INTENT:
                raise CheckFailed(f"intent {intent_id} 已是 {it['status']}，不能再登记观测")
            claims = {e["probe"][6:] for e in it["open"]["expects"] if e["probe"].startswith("claim:")}
            if metric not in claims:
                raise LedgerError(f"intent {intent_id} 没有 claim:{metric} 这条期望（有：{sorted(claims)}）")
            rec = self._base("intent_observe", actor, None, None)
            rec.update({"intent": intent_id, "metric": metric, "value": val, "source": source,
                        "evidence": evidence or ""})
            if key and source != "agent":
                rec["sig"] = obs_sig(key, intent_id, metric, val)
            return rec

        return self._append(build)

    @_exact
    def intent_settle(self, intent_id, actor=None):
        """结算：代码重新取数，贷记清账科目。全部相符才过账 then；then 被规则拒绝时状态为 then_rejected。"""
        def build(st):
            it = st.intents.get(intent_id)
            if not it:
                raise LedgerError(f"找不到 intent：{intent_id}")
            if it["status"] == "settled":
                return dict(copy.deepcopy(it["last_settle"]), _duplicate=True)
            if it["status"] in FINAL_INTENT:
                raise CheckFailed(f"intent {intent_id} 已{'核销' if it['status'] == 'written_off' else '撤销'}，"
                                  "效果不会再被记账；重做请开新的 intent")
            results, postings, ok = [], [], True
            for i, e in enumerate(it["open"]["expects"]):
                exp = to_dec(e["expected"])
                actual, note = self._actual_delta(it, e)
                prev = it["credited"][i]
                credited = prev if actual is None else actual
                inc = credited - prev
                if inc != 0:
                    postings += [{"account": f"clearing:intent:{intent_id}:{i}", "amount": dfmt(-inc), "unit": DELTA_UNIT},
                                 {"account": "intent:effected", "amount": dfmt(inc), "unit": DELTA_UNIT}]
                match = actual is not None and actual == exp
                ok = ok and match
                r = {"index": i, "probe": e["probe"], "op": e["op"], "expected": dfmt(exp),
                     "actual": None if actual is None else dfmt(actual), "credited": dfmt(credited),
                     "residual": dfmt(exp - credited), "match": match}
                if note:
                    r["note"] = note
                if e["probe"].startswith("claim:"):
                    r["trust"] = self._claim_trust(it, e)
                results.append(r)
            status, then_error = ("settled" if ok else "mismatch"), None
            then_lines = it["open"].get("then", [])
            if ok and then_lines:
                try:
                    self._check_rules(st, postings + then_lines)
                    postings += then_lines  # 记忆/状态只在效果核实后才写
                except CheckFailed as ex:
                    status, then_error = "then_rejected", str(ex)
            rec = self._base("intent_settle", actor, None, None)
            rec.update({"intent": intent_id, "status": status, "results": results, "postings": postings})
            if then_error:
                rec["then_error"] = then_error
            return rec

        return self._append(build)

    @_exact
    def intent_writeoff(self, intent_id, reason, actor=None):
        """核销：残差逐条转入挂账科目 suspense:intent:<id>:<i>，必须在结账前查明并清掉。"""
        if not reason:
            raise LedgerError("核销必须写明原因（--reason）")

        def build(st):
            it = st.intents.get(intent_id)
            if not it:
                raise LedgerError(f"找不到 intent：{intent_id}")
            if it["status"] in FINAL_INTENT:
                raise CheckFailed(f"intent {intent_id} 已是 {it['status']}")
            if it["status"] == "then_rejected":
                raise CheckFailed("效果已核实，问题在账面（then 分录违反规则）：先用 reconcile --adjust 或 "
                                  "post --direct 更正账面，再重新 settle")
            postings = []
            for i, e in enumerate(it["open"]["expects"]):
                r = to_dec(e["expected"]) - it["credited"][i]
                if r != 0:
                    postings += [{"account": f"clearing:intent:{intent_id}:{i}", "amount": dfmt(-r), "unit": DELTA_UNIT},
                                 # 每条期望单独挂账：+1/-1 若放进同一科目会相互抵消，掩盖异常
                                 {"account": f"suspense:intent:{intent_id}:{i}", "amount": dfmt(r), "unit": DELTA_UNIT}]
            rec = self._base("intent_writeoff", actor, reason, None)
            rec.update({"intent": intent_id, "postings": postings})
            return rec

        return self._append(build)

    @_exact
    def intent_cancel(self, intent_id, reason, actor=None):
        """撤销：仅当代码核实「什么都没发生」时允许；结果未知（缺观测、探针失败）也不允许。"""
        if not reason:
            raise LedgerError("撤销必须写明原因（--reason）")

        def build(st):
            it = st.intents.get(intent_id)
            if not it:
                raise LedgerError(f"找不到 intent：{intent_id}")
            if it["status"] in FINAL_INTENT:
                raise CheckFailed(f"intent {intent_id} 已是 {it['status']}")
            blockers = []
            for i, e in enumerate(it["open"]["expects"]):
                if e.get("baseline_missing") and it["credited"][i] == 0:
                    try:
                        self.prober.measure(e["probe"])
                    except ProbeUnavailable:
                        continue  # 开单时不存在、现在仍不存在：确实没发生
                actual, note = self._actual_delta(it, e)
                if actual is None:
                    blockers.append(f"{e['probe']} 结果未知（{note}）——无法证明什么都没发生")
                elif actual != 0 or it["credited"][i] != 0:
                    blockers.append(f"{e['probe']} 实际变化 {dfmt(actual)}")
            if blockers:
                raise CheckFailed("不能撤销（请 settle 或 writeoff）：" + "；".join(blockers), blockers)
            postings = []
            for i, e in enumerate(it["open"]["expects"]):
                d = to_dec(e["expected"])
                if d != 0:
                    postings += [{"account": f"clearing:intent:{intent_id}:{i}", "amount": dfmt(-d), "unit": DELTA_UNIT},
                                 {"account": "intent:planned", "amount": dfmt(d), "unit": DELTA_UNIT}]
            rec = self._base("intent_cancel", actor, reason, None)
            rec.update({"intent": intent_id, "postings": postings})
            return rec

        return self._append(build)

    @_exact
    def intents(self, status=None):
        _, st = self._snapshot()
        out = []
        for iid, it in st.intents.items():
            if status and it["status"] != status and not (status == "unresolved" and it["status"] not in FINAL_INTENT):
                continue
            out.append({"intent": iid, "status": it["status"], "tool": it["open"].get("tool", ""),
                        "opened_seq": it["open"]["seq"], "opened_ts": it["open"].get("ts"),
                        "expects": [f"{e['probe']} {e['op']}" for e in it["open"]["expects"]]})
        return out

    # ---- 3. 外部对账 / 挂账 / 结账

    @_exact
    def reconcile(self, snapshot=None, adjust=False, since_close=False, actor=None, reason=None):
        """账面 vs 真实世界。adjust=True 时把差额逐目标记入 suspense:reconcile:<科目>（记下来，而不是悄悄改平）。
        since_close=True 时比较「上次结账以来的发生额」，适合按期出具的用量账单。"""
        targets = []
        for item in self.config.get("reconcile") or []:
            targets.append((item["account"], item.get("unit") or self.default_unit, "probe", item["probe"]))
        if snapshot is not None and not isinstance(snapshot, dict):
            raise LedgerError("snapshot 格式应为 {account: {unit: value}}")
        for acct, units in (snapshot or {}).items():
            if not isinstance(units, dict):
                raise LedgerError("snapshot 格式应为 {account: {unit: value}}")
            for unit, val in units.items():
                targets.append((acct, unit, "snapshot", val))
        if not targets:
            raise LedgerError("没有对账目标：请在配置 reconcile 中声明探针，或用 --snapshot 提供外部快照")
        measured = []
        for acct, unit, kind, src in targets:
            check_account(acct)
            check_unit(unit)
            if acct.startswith(RESERVED_PREFIXES) or unit in RESERVED_UNITS:
                raise LedgerError(f"系统科目/单位不能作为对账目标：{acct} [{unit}]")
            actual = self.prober.measure(src) if kind == "probe" else to_dec(src)
            if isinstance(actual, str):
                raise LedgerError(f"对账探针必须是数值型：{src}")
            measured.append((acct, unit, kind, src, actual))

        def build(st):
            opening = {}
            if since_close and st.closes:
                for k, v in st.closes[-1]["balances"].items():
                    opening[parse_bal_key(k)] = to_dec(v)
            results, postings = [], []
            for acct, unit, kind, src, actual in measured:
                book = st.bal(acct, unit) - opening.get((acct, unit), Decimal(0))
                diff = actual - book
                results.append({"account": acct, "unit": unit, "source": "snapshot" if kind == "snapshot" else src,
                                "book": dfmt(book), "actual": dfmt(actual), "diff": dfmt(diff), "match": diff == 0})
                if adjust and diff != 0:
                    if self._is_guarded(acct) and not reason:
                        raise LedgerError(f"调整受保护科目 {acct} 必须写明原因（--reason）")
                    # 逐目标挂账：done +1 与 todo -1 若进同一个挂账科目会相互抵消
                    postings += [{"account": acct, "amount": dfmt(diff), "unit": unit},
                                 {"account": f"suspense:reconcile:{acct}", "amount": dfmt(-diff), "unit": unit}]
            rec = self._base("reconcile", actor, reason, None)
            rec.update({"ok": all(r["match"] for r in results), "adjusted": bool(postings),
                        "since_close": bool(since_close), "results": results, "postings": postings})
            return rec

        return self._append(build)

    @_exact
    def suspense(self):
        """未清的挂账项及其来源记录。"""
        records, st = self._snapshot()
        out = []
        for (a, u), v in sorted(st.balances.items()):
            if a.startswith("suspense:"):
                src = [{"seq": r["seq"], "type": r["type"], "memo": r.get("memo", ""), "ts": r.get("ts")}
                       for r in records if any(p["account"] == a and p["unit"] == u for p in r.get("postings") or [])]
                out.append({"account": a, "unit": u, "balance": dfmt(v), "sources": src})
        return out

    @_exact
    def clear_suspense(self, account, to, reason, actor=None):
        """清理挂账：把某个 suspense 科目的全部余额转到写明的去向科目，必须写原因。"""
        if not account.startswith("suspense:"):
            raise LedgerError("只能清理 suspense: 科目")
        check_account(to)
        if to.startswith(RESERVED_PREFIXES):
            raise LedgerError("去向科目不能是系统科目")
        if not reason:
            raise LedgerError("清理挂账必须写明原因（--reason）")

        def build(st):
            postings = []
            for (a, u), v in sorted(st.balances.items()):
                if a == account:
                    postings += [{"account": a, "amount": dfmt(-v), "unit": u},
                                 {"account": to, "amount": dfmt(v), "unit": u}]
            if not postings:
                raise LedgerError(f"{account} 没有未清余额")
            rec = self._base("suspense_clear", actor, reason, None)
            rec.update({"account": account, "to": to, "postings": postings})
            return rec

        return self._append(build)

    def _close_blockers(self, st):
        out = []
        for iid, it in st.intents.items():
            if it["status"] not in FINAL_INTENT:
                out.append(f"intent {iid} 未结（{it['status']}）")
        for (a, u), v in sorted(st.balances.items()):
            for r in self._rules_for(a, u):
                if r.get("zero_at_close") and v != 0:
                    out.append(f"{a} [{u}] 结账时应为 0，当前 {dfmt(v)}（规则 {r['match']}）")
                    break
        out += [f"标红未处理：{m}" for m in self._rule_violations(st.balances.items(), mode="flag")]
        if self.config.get("require_reconcile_before_close"):
            lr = st.last_reconcile
            if not lr or not lr["ok"] or lr["seq"] < st.last_posting_seq:
                out.append("最后一笔过账之后没有一次全部相符的对账（require_reconcile_before_close）")
        return out

    @_exact
    def close(self, label, actor=None):
        """结账：全部检查通过才写 close 记录，并快照余额与链头哈希（可外部存档作锚点）。
        注意：只校验临时科目已归零，不会自动结转费用类科目。"""
        check_id(label, "close label")

        def build(st):
            if any(c["label"] == label for c in st.closes):
                raise CheckFailed(f"结账标签 {label} 已用过")
            records, _ = self._read()
            v = self._verify_records(records)
            blockers = v["violations"] + self._close_blockers(st)
            if blockers:
                raise CheckFailed("结账被拒：" + "；".join(blockers), blockers)
            snap = {bal_key(a, u): dfmt(val) for (a, u), val in sorted(st.balances.items())}
            rec = self._base("close", actor, None, None)
            rec.update({"label": label, "balances": snap, "head_before": st.head})
            return rec

        return self._append(build)

    # ---- 校验（纯代码，无模型参与）

    def _verify_records(self, records):
        violations = self._chain_problems(records)
        warnings = []
        st = State()
        txn_ids, intent_ids, reversed_ids = set(), set(), set()
        period_cfg, period_start = None, 1
        review = []  # 本期需要人复核的干预 / 未核实项；结账即视为已复核
        for r in records:
            where = f"seq {r.get('seq', '?')}"
            try:
                t = r.get("type")
                if t not in KNOWN_TYPES:
                    if isinstance(r.get("v"), int) and r["v"] > SCHEMA:
                        warnings.append(f"{where}: 由更新版本写入的记录类型 {t!r}，仅按通用分录处理")
                    else:
                        violations.append(f"{where}: 未知记录类型 {t!r}")
                        continue
                postings = r.get("postings") or []
                for p in postings:
                    norm_posting(p, self.default_unit)
                check_balanced(postings, require_lines=(t == "txn"))
                if t == "txn":
                    if r.get("id") in txn_ids:
                        violations.append(f"{where}: txn id 重复 {r.get('id')}")
                    txn_ids.add(r.get("id"))
                    if r.get("reverses"):
                        if r["reverses"] not in txn_ids:
                            violations.append(f"{where}: 冲销了不存在的 txn {r['reverses']}")
                        if r["reverses"] in reversed_ids:
                            violations.append(f"{where}: txn {r['reverses']} 被重复冲销")
                        reversed_ids.add(r["reverses"])
                    if r.get("direct"):
                        review.append(f"seq {r['seq']} 直接改动受保护科目（{r['direct']}）")
                    elif not r.get("reverses"):
                        if any(is_reserved(p) for p in postings):
                            violations.append(f"{where}: 普通分录写入了系统科目/单位")
                elif t == "intent_open":
                    if r.get("intent") in intent_ids:
                        violations.append(f"{where}: intent 重复开立 {r.get('intent')}")
                    intent_ids.add(r.get("intent"))
                    if r.get("overrides"):
                        review.append(f"seq {r['seq']} intent {r['intent']} 使用了 {'/'.join(r['overrides'])}")
                elif t in ("intent_observe", "intent_settle", "intent_writeoff", "intent_cancel"):
                    if r.get("intent") not in intent_ids:
                        violations.append(f"{where}: 引用了未开立的 intent {r.get('intent')}")
                        continue
                    if t == "intent_observe" and r.get("sig") and self._trust(r) == "forged":
                        violations.append(f"{where}: claim 观测签名无效（伪造或密钥不符）")
                    if t == "intent_settle":
                        for x in r.get("results", []):
                            if x.get("trust") not in (None, "verified") and x.get("match"):
                                review.append(f"seq {r['seq']} intent {r['intent']} 的 {x['probe']} 未经签名核实（{x['trust']}）")
                    if t == "intent_writeoff":
                        review.append(f"seq {r['seq']} 核销 intent {r['intent']}（{r.get('memo', '')}）")
                elif t == "suspense_clear":
                    review.append(f"seq {r['seq']} 清理挂账 {r.get('account')} → {r.get('to')}（{r.get('memo', '')}）")
                elif t == "reconcile" and r.get("adjusted"):
                    review.append(f"seq {r['seq']} 对账调整账面（{r.get('memo') or '差额转挂账'}）")
                touched = {(p["account"], p["unit"]) for p in postings}
                if t in KNOWN_TYPES:
                    st.apply(r)
                else:
                    st.seq, st.head = r["seq"], r["hash"]
                    for p in postings:
                        k = (p["account"], p["unit"])
                        st.balances[k] = st.balances.get(k, Decimal(0)) + to_dec(p["amount"])
                for msg in self._rule_violations(((k, st.bal(*k)) for k in touched), mode="reject"):
                    violations.append(f"{where}: {msg}")
                if r.get("cfg") is not None:
                    if period_cfg is not None and r["cfg"] != period_cfg:
                        warnings.append(f"{where}: 本期内配置被修改（自 seq {period_start} 起）")
                    period_cfg = r["cfg"]
                if t == "close":
                    snap = {bal_key(a, u): dfmt(v) for (a, u), v in sorted(st.balances.items())}
                    if snap != r.get("balances"):
                        violations.append(f"{where}: 结账快照与回放余额不一致——结账前的历史被改写")
                    period_cfg, period_start, review = None, r["seq"] + 1, []
            except Exception as e:  # noqa: BLE001  任何畸形记录都报告为违规，而不是崩溃
                violations.append(f"{where}: 记录结构异常 {e}")
        sums = {}
        for (a, u), v in st.balances.items():
            sums[u] = sums.get(u, Decimal(0)) + v
        for u, s in sums.items():
            if s != 0:
                violations.append(f"试算不平：单位 {u} 差额 {dfmt(s)}")
        for iid, it in st.intents.items():
            if it["status"] not in FINAL_INTENT:
                warnings.append(f"intent {iid} 未结（{it['status']}，seq {it['open']['seq']} 开立）")
        for (a, u), v in sorted(st.balances.items()):
            if a.startswith("suspense:"):
                warnings.append(f"挂账未清：{a} [{u}] = {dfmt(v)}")
        warnings += [f"标红：{m}" for m in self._rule_violations(st.balances.items(), mode="flag")]
        if review:
            warnings.append(f"本期有 {len(review)} 项人工干预或未核实项，需要人复核（结账即视为已复核）：" + "；".join(review[:8])
                            + ("……" if len(review) > 8 else ""))
        return {"violations": violations, "warnings": warnings, "state": st}

    @_exact
    def verify(self, anchors=(), strict=False):
        with self._locked():
            records, problems = self._read()
        v = self._verify_records(records)
        violations = [p["msg"] for p in problems] + v["violations"]
        hashes = {r.get("hash") for r in records}
        for a in anchors:
            if a not in hashes:
                violations.append(f"锚点哈希 {a[:12]}… 不在链上——锚点之前的历史被改写或截断")
        st = v["state"]
        ok = not violations and not (strict and v["warnings"])
        return {"ok": ok, "strict": strict, "violations": violations, "warnings": v["warnings"],
                "records": len(records), "head": st.head,
                "unresolved_intents": sum(1 for it in st.intents.values() if it["status"] not in FINAL_INTENT),
                "last_close": st.closes[-1]["label"] if st.closes else None}

    def _drop_torn_tail(self):
        data = self.journal.read_bytes()
        keep = data[: data.rfind(b"\n") + 1]
        backup = self.journal.with_name(self.journal.name + ".torn-" +
                                        datetime.datetime.now().strftime("%Y%m%d%H%M%S%f"))
        backup.write_bytes(data)
        with open(self.journal, "wb") as f:
            f.write(keep)
            f.flush()
            os.fsync(f.fileno())
        return {"dropped_bytes": len(data) - len(keep), "backup": str(backup)}

    def repair_drop_torn_tail(self):
        with self._locked():
            _, problems = self._read()
            kinds = {p["kind"] for p in problems}
            if not problems:
                raise LedgerError("日记账无需修复")
            if kinds != {"torn_tail"}:
                raise LedgerError("只有「末行不完整」这一种问题时才能自动修复；其他损坏请人工处理：" +
                                  "；".join(p["msg"] for p in problems))
            return self._drop_torn_tail()


# ---------------------------------------------------------------- CLI

def _out(args, obj, human):
    if args.json:
        print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))
    else:
        print(human)


def _strip(rec):
    return {k: v for k, v in rec.items() if not k.startswith("_")} if isinstance(rec, dict) else rec


def _rec_line(rec):
    if rec.get("_duplicate"):
        return f"重复提交，已忽略（幂等）：seq {rec.get('seq')}"
    return f"已记账 seq {rec['seq']} {rec['type']} {rec.get('id') or rec.get('intent') or rec.get('label') or ''}".rstrip()


def _build_parser():
    ap = argparse.ArgumentParser(prog="ledger.py", description="面向智能体 harness 的复式记账账本。"
                                 "负数参数请用 --opt=-3 的写法。")
    ap.add_argument("--journal", help="日记账路径（默认 $LEDGER_JOURNAL 或 ./.ledger/journal.jsonl）")
    ap.add_argument("--config", help="配置文件（默认 $LEDGER_CONFIG 或 日记账同目录 config.json）")
    ap.add_argument("--actor", help="记账人标签（默认 $LEDGER_ACTOR）")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--version", action="version", version=VERSION)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="创建日记账与默认配置")

    p = sub.add_parser("post", help="记一笔分录：POSTING 形如 'world:created -1' 'state:todo 1'")
    p.add_argument("--id", required=True)
    p.add_argument("--memo")
    p.add_argument("--meta", help="JSON 对象，如关联的 tool_use_id / openspec change")
    p.add_argument("--direct", metavar="REASON", help="不经意图核实、直接迁移受保护科目时必须写明原因")
    p.add_argument("postings", nargs="+")

    p = sub.add_parser("transfer", help="从 --from 转 --amount 到 --to（一借一贷）")
    p.add_argument("--id", required=True)
    p.add_argument("--from", dest="src", required=True)
    p.add_argument("--to", dest="dst", required=True)
    p.add_argument("--amount", required=True)
    p.add_argument("--unit")
    p.add_argument("--memo")
    p.add_argument("--meta")
    p.add_argument("--direct", metavar="REASON")

    p = sub.add_parser("reverse", help="红字冲销一笔 txn")
    p.add_argument("--id", required=True)
    p.add_argument("--of", required=True)
    p.add_argument("--reason", required=True)

    p = sub.add_parser("balance", help="科目余额（支持 fnmatch 模式）")
    p.add_argument("pattern", nargs="?", default="*")

    sub.add_parser("trial", help="试算平衡表（只证明账本内部一致）")

    p = sub.add_parser("verify", help="全量校验：哈希链 / 借贷平衡 / 科目规则 / 结账快照 / 锚点")
    p.add_argument("--anchor", action="append", default=[], help="账外存档的链头哈希，可重复")
    p.add_argument("--strict", action="store_true", help="有任何提示（未结意图、未清挂账、标红、直接改动）也算不通过")
    p.add_argument("--quiet", action="store_true", help="通过时只输出一行")

    ip = sub.add_parser("intent", help="意图-效果对账").add_subparsers(dest="icmd", required=True)
    p = ip.add_parser("open", help="动手前开立意图，登记预期效果并抓基线")
    p.add_argument("intent")
    p.add_argument("--expect", action="append", default=[],
                   help="'PROBE OP'，如 'glob-count:tasks/done/*.md +1'、'exists:out/a.txt =1'、'sha256:src/a.py changed'")
    p.add_argument("--then", action="append", default=[], help="效果核实后才过账的分录（须借贷平衡），可重复")
    p.add_argument("--tool", help="工具名；配置了 expect_templates 时据此生成期望")
    p.add_argument("--arg", action="append", default=[], metavar="K=V", help="期望模板参数，可重复")
    p.add_argument("--allow-noop", action="store_true", help="允许所有期望都是「不变」")
    p.add_argument("--allow-shared-probe", action="store_true", help="允许与未结意图共用探针")
    p.add_argument("--memo")
    p.add_argument("--meta")
    p = ip.add_parser("observe", help="为 claim 期望登记实际值（应由 harness 从工具返回值写入）")
    p.add_argument("intent")
    p.add_argument("--metric", required=True)
    p.add_argument("--value", required=True)
    p.add_argument("--source", required=True, choices=OBS_SOURCES)
    p.add_argument("--evidence")
    p = ip.add_parser("settle", help="动手后结算：代码重新取数并比对")
    p.add_argument("intent")
    p = ip.add_parser("writeoff", help="残差转入挂账")
    p.add_argument("intent")
    p.add_argument("--reason", required=True)
    p = ip.add_parser("cancel", help="代码核实未产生任何效果后撤销")
    p.add_argument("intent")
    p.add_argument("--reason", required=True)
    p = ip.add_parser("list", help="列出意图")
    p.add_argument("--status", choices=["open", "mismatch", "then_rejected", "settled", "written_off",
                                        "cancelled", "unresolved"])

    p = sub.add_parser("reconcile", help="外部对账：账面 vs 探针 / 外部快照")
    p.add_argument("--snapshot", help="JSON 文件 {account: {unit: value}}，'-' 表示 stdin")
    p.add_argument("--adjust", action="store_true", help="把差额记入 suspense:reconcile:<科目>")
    p.add_argument("--reason", help="--adjust 涉及受保护科目时必填")
    p.add_argument("--since-close", action="store_true", help="比较上次结账以来的发生额（按期账单用）")

    sub.add_parser("suspense", help="列出未清挂账")
    p = sub.add_parser("clear-suspense", help="清理挂账：余额转到写明的去向科目")
    p.add_argument("account")
    p.add_argument("--to", required=True)
    p.add_argument("--reason", required=True)

    p = sub.add_parser("close", help="结账（全部检查通过才写入）")
    p.add_argument("label")

    p = sub.add_parser("repair", help="手动处理崩溃导致的不完整末行")
    p.add_argument("--drop-torn-tail", action="store_true", required=True)
    return ap


def main(argv=None):
    args = _build_parser().parse_args(argv)
    try:
        with decimal.localcontext(EXACT):
            return _dispatch(args)
    except CheckFailed as e:
        _err(args, str(e), e.details)
        return e.exit_code
    except LedgerError as e:
        _err(args, str(e))
        return e.exit_code
    except Exception as e:  # noqa: BLE001  未预料的异常一律按 2 退出，不与「检查未通过」混淆
        _err(args, f"内部错误：{e!r}")
        return 2


def _err(args, msg, details=None):
    if args.json:
        print(json.dumps({"ok": False, "error": msg, "details": details or []}, ensure_ascii=False, indent=2))
    else:
        print(f"✗ {msg}", file=sys.stderr)


def _meta(s):
    if not s:
        return None
    try:
        m = json.loads(s)
    except ValueError as e:
        raise LedgerError(f"--meta 不是合法 JSON：{e}")
    if not isinstance(m, dict):
        raise LedgerError("--meta 必须是 JSON 对象")
    return m


def _kv(items):
    out = {}
    for it in items:
        k, sep, v = it.partition("=")
        if not sep or not k:
            raise LedgerError(f"--arg 格式应为 K=V：{it!r}")
        out[k] = v
    return out


def _dispatch(args):
    if args.cmd == "init":
        j = Path(args.journal or os.environ.get("LEDGER_JOURNAL") or ".ledger/journal.jsonl")
        j.parent.mkdir(parents=True, exist_ok=True)
        j.touch(exist_ok=True)
        cfg = Path(args.config) if args.config else j.parent / "config.json"
        created = not cfg.exists()
        if created:
            cfg.write_text(json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        _out(args, {"journal": str(j), "config": str(cfg), "config_created": created},
             f"日记账：{j}\n配置：{cfg}{'（新建）' if created else '（已存在，未改动）'}")
        return 0

    led = Ledger(args.journal, args.config)
    rc = _run(led, args)
    for n in led.notices:
        print(f"! {n}", file=sys.stderr)
    return rc


def _run(led, args):
    a = args.actor
    c = args.cmd
    if c == "post":
        rec = led.post(args.id, args.postings, args.memo, a, _meta(args.meta), args.direct)
        _out(args, _strip(rec), _rec_line(rec))
    elif c == "transfer":
        rec = led.transfer(args.id, args.src, args.dst, args.amount, args.unit, args.memo, a, _meta(args.meta), args.direct)
        _out(args, _strip(rec), _rec_line(rec))
    elif c == "reverse":
        rec = led.reverse(args.id, args.of, args.reason, a)
        _out(args, _strip(rec), _rec_line(rec))
    elif c == "balance":
        b = led.balances(args.pattern)
        _out(args, b, "\n".join(f"{k:<48} {v:>14}" for k, v in b.items()) or "（无非零余额）")
    elif c == "trial":
        t = led.trial()
        lines = [f"{'unit':<12}{'借方':>16}{'贷方':>16}{'差额':>10}"]
        lines += [f"{r['unit']:<12}{r['debit']:>16}{r['credit']:>16}{r['diff']:>10}" for r in t["units"]]
        lines.append("试算平衡 ✓（仅说明账本内部一致，不说明账实相符）" if t["balanced"] else "试算不平 ✗")
        _out(args, t, "\n".join(lines))
        return 0 if t["balanced"] else 1
    elif c == "verify":
        v = led.verify(args.anchor, args.strict)
        if args.json:
            _out(args, v, "")
        elif v["ok"] and (args.quiet or not v["warnings"]):
            print(f"✓ 日记账内部一致：{v['records']} 条记录，链头 {v['head'][:12]}，未结意图 {v['unresolved_intents']}"
                  + (f"，提示 {len(v['warnings'])} 条（加 --strict 视为不通过）" if v["warnings"] else ""))
        else:
            for m in v["violations"]:
                print(f"✗ {m}")
            for m in v["warnings"]:
                print(f"! {m}")
            print(("✓ 日记账内部一致" if v["ok"] else "✗ 未通过") + f"（{v['records']} 条记录，链头 {v['head'][:12]}）")
        return 0 if v["ok"] else 1
    elif c == "intent":
        return _run_intent(led, args, a)
    elif c == "reconcile":
        snap = None
        if args.snapshot:
            try:
                snap = json.load(sys.stdin) if args.snapshot == "-" else json.loads(Path(args.snapshot).read_text(encoding="utf-8"))
            except (OSError, ValueError) as e:
                raise LedgerError(f"snapshot 读取失败：{e}")
        rec = led.reconcile(snap, args.adjust, args.since_close, a, args.reason)
        lines = [f"{'✓' if r['match'] else '✗'} {r['account']} [{r['unit']}] 账面 {r['book']} / 实际 {r['actual']}"
                 + (f" / 差额 {r['diff']}" if not r["match"] else "") for r in rec["results"]]
        lines.append("账实相符 ✓" if rec["ok"] else ("账实不符 ✗，差额已逐项转入 suspense:reconcile:*" if rec["adjusted"] else "账实不符 ✗"))
        _out(args, _strip(rec), "\n".join(lines))
        return 0 if rec["ok"] else 1
    elif c == "suspense":
        items = led.suspense()
        _out(args, items, "\n".join(f"{i['account']} [{i['unit']}] = {i['balance']}  来源 seq "
                                    + ",".join(str(s["seq"]) for s in i["sources"]) for i in items) or "（无未清挂账）")
    elif c == "clear-suspense":
        rec = led.clear_suspense(args.account, args.to, args.reason, a)
        _out(args, _strip(rec), _rec_line(rec))
    elif c == "close":
        rec = led.close(args.label, a)
        _out(args, _strip(rec), f"已结账 {rec['label']}（seq {rec['seq']}）。请把链头哈希存到账外作锚点：{rec['hash']}")
    elif c == "repair":
        r = led.repair_drop_torn_tail()
        _out(args, r, f"已截掉不完整末行 {r['dropped_bytes']} 字节，原文件备份于 {r['backup']}")
    return 0


def _run_intent(led, args, a):
    ic = args.icmd
    if ic == "open":
        rec = led.intent_open(args.intent, args.expect, args.then, args.tool, _kv(args.arg), args.memo, a,
                              _meta(args.meta), args.allow_noop, args.allow_shared_probe)
        human = _rec_line(rec)
        if not rec.get("_duplicate"):
            human += "\n" + "\n".join(
                f"  期望 {e['probe']} {e['op']}（基线 {e['baseline'][:12] if e['probe'].startswith('sha256:') else e['baseline']}"
                f"，预期变化 {e['expected']}{'，⚠ 不变' if e['vacuous'] else ''}）" for e in rec["expects"])
        _out(args, _strip(rec), human)
    elif ic == "observe":
        rec = led.intent_observe(args.intent, args.metric, args.value, args.source, args.evidence, a)
        _out(args, _strip(rec), _rec_line(rec) + ("（已签名）" if rec.get("sig") else ""))
    elif ic == "settle":
        rec = led.intent_settle(args.intent, a)
        st = rec["status"]
        head = {"settled": "✓ 效果与计划相符", "mismatch": "✗ 效果与计划不符，不能报告为已完成",
                "then_rejected": "✗ 效果已核实，但 then 分录违反科目规则，状态未更新"}[st]
        lines = [f"intent {args.intent}：{head}" + ("（此前已结算）" if rec.get("_duplicate") else "")]
        for r in rec["results"]:
            lines.append(f"  {'✓' if r['match'] else '✗'} {r['probe']} {r['op']}：预期 {r['expected']}，实际 {r['actual']}"
                         + (f"，残差 {r['residual']}" if r["residual"] != "0" else "")
                         + (f"（{r['note']}）" if r.get("note") else ""))
        if rec.get("then_error"):
            lines.append(f"  {rec['then_error']}")
        _out(args, _strip(rec), "\n".join(lines))
        return 0 if st == "settled" else 1
    elif ic == "writeoff":
        rec = led.intent_writeoff(args.intent, args.reason, a)
        _out(args, _strip(rec), _rec_line(rec) + "（残差已转入 suspense，结账前须查明并 clear-suspense）")
    elif ic == "cancel":
        rec = led.intent_cancel(args.intent, args.reason, a)
        _out(args, _strip(rec), _rec_line(rec))
    elif ic == "list":
        items = led.intents(args.status)
        _out(args, items, "\n".join(f"{i['intent']:<24} {i['status']:<14} {i['tool']:<16} {'; '.join(i['expects'])}"
                                    for i in items) or "（无）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
