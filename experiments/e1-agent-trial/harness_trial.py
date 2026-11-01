#!/usr/bin/env python3
"""
搭建者视角的实测：按 integration.md 把账本挂到一个最小 harness 的工具执行器上，
然后逐个注入常见故障，看账本能不能抓到、在哪一层抓到。

每个场景一个独立临时目录。输出一张表：场景 / 预期 / 实际检出层 / 是否符合预期。
用法：python3 harness_trial.py <skill 目录>
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

SK = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(SK / "scripts"))
from ledger import CheckFailed, Ledger, LedgerError  # noqa: E402

KEY = "harness-secret-not-visible-to-model"

CONFIG = {
    "expect_templates": {
        "mv": {"expect": ["exists:{dst} =1", "exists:{src} =0"],
               "then": ["state:todo -1 {task}", "state:done 1 {task}"]},
        "write_file": ["exists:{path} =1", "sha256:{path} changed"],
        "run_tests": ["json:{report}#failed =0", "sha256:{report} changed"],
        "send_email": ["claim:sent =1"],
    },
    "require_expect_template": True,
    "reject_self_certified": True,
    "rules": [{"match": "world:created", "unit": "task*", "min": -1}],
    "reconcile": [],
}


class Harness:
    """最小 harness：工具执行器在调用前后自动 open / observe / settle。模型只决定调用哪个工具。"""

    def __init__(self, root: Path, tasks=("a", "b", "c")):
        self.root = root
        cfg = json.loads(json.dumps(CONFIG))
        cfg["reconcile"] = [{"account": "state:done", "unit": f"task{t}", "probe": f"exists:tasks/done/{t}.md"}
                            for t in tasks] + \
                           [{"account": "state:todo", "unit": f"task{t}", "probe": f"exists:tasks/todo/{t}.md"}
                            for t in tasks]
        (root / ".ledger").mkdir(parents=True, exist_ok=True)
        (root / ".ledger/config.json").write_text(json.dumps(cfg, ensure_ascii=False))
        os.environ["LEDGER_OBSERVER_KEY"] = KEY
        self.led = Ledger(root / ".ledger/journal.jsonl")
        self.n = 0
        for t in tasks:
            (root / "tasks/todo").mkdir(parents=True, exist_ok=True)
            (root / "tasks/done").mkdir(parents=True, exist_ok=True)
            (root / f"tasks/todo/{t}.md").write_text(f"# {t}\n")
            self.led.post(f"create-{t}", [f"world:created -1 task{t}", f"state:todo 1 task{t}"])

    def call(self, tool, args, impl, claims=None, settle=True):
        """impl：真正执行工具的函数（故障在这里注入）。claims：从工具结构化返回值里取的观测。"""
        self.n += 1
        cid = f"{tool}-{self.n}"
        self.led.intent_open(cid, tool=tool, args=args)
        result = impl()
        for name, val in (claims(result) if claims else []):
            self.led.intent_observe(cid, name, val, source="tool", evidence=str(result))
        if not settle:
            return cid, None
        return cid, self.led.intent_settle(cid)


# ------------------------------------------------------------- 工具实现（正确版）

def mv(root, src, dst):
    return lambda: shutil.move(str(root / src), str(root / dst))


def write(root, path, text):
    def f():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(text)
    return f


def tests(root, report, failed):
    def f():
        (root / report).write_text(json.dumps({"failed": failed, "ts": time.time()}))
    return f


def mv_args(t):
    return {"src": f"tasks/todo/{t}.md", "dst": f"tasks/done/{t}.md", "task": f"task{t}"}


# ------------------------------------------------------------- 场景

def happy(h, r):
    """无故障：3 个任务都写页面、移动卡片、跑测试。期望：全部 settled，verify --strict 通过，可结账。"""
    for t in "abc":
        h.call("write_file", {"path": f"site/{t}.html"}, write(r, f"site/{t}.html", f"<h1>{t}</h1>"))
        h.call("mv", mv_args(t), mv(r, f"tasks/todo/{t}.md", f"tasks/done/{t}.md"))
    (r / "report.json").write_text('{"failed": 0}')
    h.call("run_tests", {"report": "report.json"}, tests(r, "report.json", 0))
    rec = h.led.reconcile()
    v = h.led.verify(strict=True)
    h.led.close("p1")
    return f"reconcile ok={rec['ok']}, strict={v['ok']}, close 成功"


def plan_as_result(h, r):
    """工具返回成功但什么都没做（模型把计划当结果）。"""
    _, s = h.call("mv", mv_args("a"), lambda: "ok")
    return s["status"]


def copy_not_move(h, r):
    """mv 实际变成了 cp：目标有了，源还在。"""
    _, s = h.call("mv", mv_args("a"), lambda: shutil.copy(r / "tasks/todo/a.md", r / "tasks/done/a.md"))
    return s["status"] + " " + str([x["probe"] for x in s["results"] if not x["match"]])


def wrong_dst(h, r):
    """移到了错误目录。"""
    (r / "tasks/archive").mkdir()
    _, s = h.call("mv", mv_args("a"), mv(r, "tasks/todo/a.md", "tasks/archive/a.md"))
    return s["status"]


def stale_report(h, r):
    """测试脚本崩了但退出码 0，report.json 是上个月的旧报告 failed=0。"""
    (r / "report.json").write_text('{"failed": 0, "generated": "2026-10-01"}')
    _, s = h.call("run_tests", {"report": "report.json"}, lambda: None)
    return s["status"] + " " + str([x["probe"] for x in s["results"] if not x["match"]])


def tests_fail(h, r):
    (r / "report.json").write_text('{"failed": 0}')
    _, s = h.call("run_tests", {"report": "report.json"}, tests(r, "report.json", 2))
    return s["status"]


def forged_claim(h, r):
    """邮件工具没返回 sent；模型自己用 CLI 写一条 --source tool 的观测（没有密钥）。"""
    cid, _ = h.call("send_email", {}, lambda: {"error": "smtp timeout"}, settle=False)
    env = {k: v for k, v in os.environ.items() if k != "LEDGER_OBSERVER_KEY"}
    p = subprocess.run([sys.executable, str(SK / "scripts/ledger.py"), "--journal", str(r / ".ledger/journal.jsonl"),
                        "intent", "observe", cid, "--metric", "sent", "--value", "1", "--source", "tool"],
                       env=env, capture_output=True, text=True)
    s = h.led.intent_settle(cid)
    note = [x.get("note") for x in s["results"]]
    return f"模型写入观测 exit={p.returncode}；settle={s['status']} {note}"


def unknown_outcome(h, r):
    """邮件工具调用后进程崩了，没有任何返回值：结果未知，不能当作没发生。"""
    cid, _ = h.call("send_email", {}, lambda: None, settle=False)
    s = h.led.intent_settle(cid)
    try:
        h.led.intent_cancel(cid, "崩了")
        c = "cancel 未拦截"
    except CheckFailed:
        c = "cancel 被拒"
    return f"settle={s['status']}，{c}"


def signed_claim(h, r):
    """对照：工具真的返回 sent=1，harness 签名观测。"""
    _, s = h.call("send_email", {}, lambda: {"sent": 1, "id": "m-1"}, claims=lambda res: [("sent", res["sent"])])
    return s["status"] + " trust=" + str(s["results"][0].get("trust"))


def crash_recovery(h, r):
    """mv 执行完、settle 前崩溃。新进程按 rule 6：list unresolved → settle，不重跑动作。"""
    cid, _ = h.call("mv", mv_args("a"), mv(r, "tasks/todo/a.md", "tasks/done/a.md"), settle=False)
    led2 = Ledger(r / ".ledger/journal.jsonl")
    pend = [i["intent"] for i in led2.intents("unresolved")]
    s = led2.intent_settle(pend[0])
    return f"恢复出 {pend}，settle={s['status']}，state:done={led2.balances('state:done')}"


def external_drift(h, r):
    """settle 之后，有人在账本之外把 done 里的卡删了。intent 层抓不到，靠 reconcile。"""
    h.call("mv", mv_args("a"), mv(r, "tasks/todo/a.md", "tasks/done/a.md"))
    os.remove(r / "tasks/done/a.md")
    rec = h.led.reconcile()
    bad = [f"{x['account']}[{x['unit']}] 账{x['book']}/实{x['actual']}" for x in rec["results"] if not x["match"]]
    return f"reconcile ok={rec['ok']} {bad}"


def direct_transition(h, r):
    """模型绕过 intent 直接 post 状态迁移。"""
    try:
        h.led.post("cheat", ["state:todo -1 taska", "state:done 1 taska"])
        return "未拦截"
    except LedgerError as e:
        return "拒绝：" + str(e)[:40]


def fake_creation_in_done(h, r):
    """把实体直接「新建」在 done 池（伪装成新建）。"""
    try:
        h.led.post("cheat2", ["world:created -1 taska", "state:done 1 taska"])
        return "未拦截"
    except CheckFailed as e:
        return "拒绝：" + str(e)[:60]


def double_done(h, r):
    """同一个任务第二次 mv（源已不存在）。"""
    h.call("mv", mv_args("a"), mv(r, "tasks/todo/a.md", "tasks/done/a.md"))
    try:
        _, s = h.call("mv", mv_args("a"), lambda: None)
        return s["status"]
    except CheckFailed as e:
        return "open 被拒：" + str(e)[:50]


def wrong_content(h, r):
    """页面写了，但内容是错的（空页面）。已知局限：数量/存在核实不了质量。"""
    _, s = h.call("write_file", {"path": "site/a.html"}, write(r, "site/a.html", ""))
    return s["status"]


def collateral_damage(h, r):
    """写 a.html 时顺手删了 data 目录里的东西（期望之外的副作用）。"""
    (r / "data").mkdir()
    (r / "data/b.txt").write_text("x")

    def impl():
        write(r, "site/a.html", "<h1>a</h1>")()
        os.remove(r / "data/b.txt")
    _, s = h.call("write_file", {"path": "site/a.html"}, impl)
    return s["status"]


def budget_lost_agent(h, r):
    """子 agent 拿了预算，花了一部分，失联没退回。"""
    led = h.led
    led.post("fund", ["equity:funding -10000 tokens", "budget:pool 10000 tokens"])
    led.transfer("g-A", "budget:pool", "budget:agent:A", 3000, unit="tokens")
    led.transfer("s-A-req1", "budget:agent:A", "expense:tokens:A", 1200, unit="tokens")
    v = led.verify(strict=True)
    pre = f"strict ok={v['ok']}{'（列出了子预算）' if any('budget:agent:A' in w for w in v['warnings']) else '（未列出子预算）'}；"
    try:
        led.close("p1")
        return pre + "未拦截"
    except CheckFailed as e:
        return pre + "close 被拒：" + "; ".join(d for d in e.details if "agent" in d)[:80]


def budget_vendor_mismatch(h, r):
    """包装层漏记一次请求：供应商账单比账面多 800。"""
    led = h.led
    led.post("fund", ["equity:funding -10000 tokens", "budget:pool 10000 tokens"])
    led.transfer("g-A", "budget:pool", "budget:agent:A", 3000, unit="tokens")
    led.transfer("s-A-1", "budget:agent:A", "expense:tokens:A", 1200, unit="tokens")
    rec = led.reconcile(snapshot={"expense:tokens:A": {"tokens": "2000"}})
    return f"ok={rec['ok']} diff={[x['diff'] for x in rec['results'] if x['account'].startswith('expense')]}"


def tamper(h, r):
    """有人手改日记账（改了一条金额）。"""
    h.call("mv", mv_args("a"), mv(r, "tasks/todo/a.md", "tasks/done/a.md"))
    j = r / ".ledger/journal.jsonl"
    j.write_text(j.read_text().replace('"amount":"1","unit":"taskb"', '"amount":"2","unit":"taskb"', 1)
                 .replace('"amount":"-1","unit":"taskb"', '"amount":"-2","unit":"taskb"', 1))
    led2 = Ledger(j)
    v = led2.verify()
    try:
        led2.post("x", ["world:created -1 taskz", "state:todo 1 taskz"])
        app = "追加未被拒"
    except LedgerError:
        app = "追加被拒"
    return f"verify ok={v['ok']}（{v['violations'][0][:20]}…），{app}"


SCENARIOS = [
    # (函数, 预期是否检出, 预期检出层)
    (happy, False, "无故障对照：不应误报"),
    (plan_as_result, True, "intent settle"),
    (copy_not_move, True, "intent settle（逐条清账）"),
    (wrong_dst, True, "intent settle"),
    (stale_report, True, "intent settle（sha256 changed）"),
    (tests_fail, True, "intent settle（json 探针）"),
    (forged_claim, True, "签名观测 + 严格模式"),
    (unknown_outcome, True, "settle=unknown（0.3.0）"),
    (signed_claim, False, "对照：真实签名观测不应误报"),
    (crash_recovery, False, "WAL 恢复：不重跑也能结清"),
    (external_drift, True, "reconcile"),
    (direct_transition, True, "受保护科目"),
    (fake_creation_in_done, True, "只创建一次规则"),
    (double_done, True, "intent settle / state 规则"),
    (wrong_content, False, "已知局限：质量问题本就抓不到"),
    (collateral_damage, False, "已知局限：期望之外的副作用抓不到"),
    (budget_lost_agent, True, "close（子预算归零）"),
    (budget_vendor_mismatch, True, "reconcile --snapshot"),
    (tamper, True, "哈希链"),
]


def detected(out: str):
    o = out.lower()
    return any(k in o for k in ("mismatch", "unknown", "拒绝", "被拒", "ok=false", "未经签名")) and "未拦截" not in out


def main():
    rows = []
    for fn, exp, layer in SCENARIOS:
        root = Path(tempfile.mkdtemp(prefix=f"ht-{fn.__name__}-", dir=os.environ.get("TMPDIR")))
        try:
            h = Harness(root)
            out = fn(h, root)
        except Exception as e:  # noqa: BLE001
            out = f"异常 {type(e).__name__}: {e}"
        got = detected(out)
        rows.append((fn.__name__, layer, exp, got, out))
    w = max(len(r[0]) for r in rows)
    for name, layer, exp, got, out in rows:
        mark = "✓" if exp == got else "✗"
        print(f"{mark} {name:<{w}}  预期{'检出' if exp else '不报'} / 实际{'检出' if got else '未报'}  [{layer}]  {out}")
    print(f"\n符合预期 {sum(1 for r in rows if r[2] == r[3])}/{len(rows)}")


if __name__ == "__main__":
    main()
