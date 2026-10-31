"""python3 -m unittest discover -s tests   （在 skill 根目录运行）

每个以 test_r_ 开头的用例对应一条对抗性审查发现（见 tests/README 或 结果.md），防回归。
"""
import contextlib
import io
import json
import multiprocessing
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import ledger as L  # noqa: E402


def _worker(journal, n, tag):
    led = L.Ledger(journal)
    for i in range(n):
        led.transfer(f"{tag}-{i}", "world:source", "pool:x", 1)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.j = self.root / ".ledger" / "journal.jsonl"
        self.led = L.Ledger(self.j)
        self._env = dict(os.environ)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        self.tmp.cleanup()

    def cli(self, *args):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return L.main(["--journal", str(self.j), *args])

    def bal(self, acct, unit="count", led=None):
        return (led or self.led).balances(acct).get(f"{acct} [{unit}]", "0")

    def rewrite(self, mutate):
        """模拟篡改：改记录后重算整条链。"""
        recs = [json.loads(x) for x in self.j.read_text().splitlines()]
        recs = mutate(recs) or recs
        prev = L.GENESIS
        for i, r in enumerate(recs, 1):
            r["seq"], r["prev"] = i, prev
            r["hash"] = L.rec_hash(r)
            prev = r["hash"]
        self.j.write_text("".join(L.canon(r) + "\n" for r in recs))


class TestPosting(Base):
    def test_balanced_post_and_trial(self):
        self.led.post("t1", ["world:created -2", "state:todo 2"])
        self.led.post("t2", ["state:todo -1", "state:done 1"], direct="手工更正")
        self.assertEqual(self.bal("state:todo"), "1")
        self.assertTrue(self.led.trial()["balanced"])
        self.assertTrue(self.led.verify()["ok"])

    def test_unbalanced_rejected(self):
        with self.assertRaises(L.CheckFailed):
            self.led.post("t1", ["a:x -1", "a:y 2"])
        with self.assertRaises(L.CheckFailed):
            self.led.post("t1", ["a:y 1"])  # 有借必有贷
        self.assertEqual(self.led.verify()["records"], 0)

    def test_units_do_not_net(self):
        with self.assertRaises(L.CheckFailed):
            self.led.post("t1", ["budget:pool 5 tokens", "equity:x -5 usd"])

    def test_float_and_weird_numbers_rejected(self):
        for bad in (0.1, "1_000", "١", "NaN", "Infinity", "1E+1000000", "0x10"):
            with self.assertRaises(L.LedgerError, msg=bad):
                self.led.post("t1", [{"account": "a:x", "amount": bad}, {"account": "b:y", "amount": "-1"}])

    def test_decimal_precision(self):
        self.led.post("t1", ["equity:fund -0.3 usd", "budget:pool 0.1 usd", "budget:pool 0.2 usd"])
        self.assertEqual(self.bal("budget:pool", "usd"), "0.3")
        self.led.post("t2", ["a:x 1.500000000000000000000", "b:y -1.5"])  # 尾随 0 不算超位

    def test_r_precision_off_by_one_not_rounded_away(self):
        with self.assertRaises(L.LedgerError):
            self.led.post("p1", ["a:x 10000000000000000000000000000001", "b:y -10000000000000000000000000000000"])
        with self.assertRaises(L.LedgerError):
            self.led.post("p2", ["a:x 1.0000000000000000000000000001", "b:y -1"])
        with self.assertRaises(L.CheckFailed):  # 在允许范围内的差 1 必须被发现
            self.led.post("p3", ["a:x 100000000000000000000000000001", "b:y -100000000000000000000000000000"])

    def test_min_rule_blocks_double_completion(self):
        self.led.post("t1", ["world:created -1", "state:todo 1"])
        self.led.post("t2", ["state:todo -1", "state:done 1"], direct="x")
        with self.assertRaises(L.CheckFailed):
            self.led.post("t3", ["state:todo -1", "state:done 1"], direct="x")
        self.assertEqual(self.bal("state:done"), "1")

    def test_idempotent_retry(self):
        r1 = self.led.transfer("t1", "world:x", "pool:a", 3)
        r2 = self.led.transfer("t1", "world:x", "pool:a", 3)
        self.assertTrue(r2.get("_duplicate"))
        self.assertEqual(r1["seq"], r2["seq"])
        self.assertEqual(self.bal("pool:a"), "3")
        with self.assertRaises(L.CheckFailed):
            self.led.transfer("t1", "world:x", "pool:a", 4)

    def test_reverse(self):
        self.led.post("t1", ["world:created -1", "state:todo 1"])
        self.led.reverse("t1r", "t1", "建错了")
        self.assertEqual(self.bal("state:todo"), "0")
        self.assertTrue(self.led.reverse("t1r", "t1", "重试").get("_duplicate"))
        with self.assertRaises(L.CheckFailed):
            self.led.reverse("t1r2", "t1", "再来一次")
        with self.assertRaises(L.LedgerError):
            self.led.reverse("x", "t1r", "冲销冲销")
        self.assertTrue(self.led.verify()["ok"])

    def test_r_concurrent_reverse_only_once(self):
        self.led.post("A", ["world:x -1", "pool:x 1"])
        errs = []

        def rev(i):
            try:
                L.Ledger(self.j).reverse(f"r{i}", "A", "并发")
            except L.CheckFailed as e:
                errs.append(e)
        ts = [threading.Thread(target=rev, args=(i,)) for i in range(6)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(len(errs), 5)
        self.assertEqual(self.bal("pool:x"), "0")

    def test_r_reversal_id_reuse_against_other_txn(self):
        self.led.post("A", ["world:x -1", "pool:x 1"])
        self.led.post("B", ["world:x -1", "pool:x 1"])
        self.led.reverse("r1", "A", "x")
        with self.assertRaises(L.CheckFailed):
            self.led.reverse("r1", "B", "x")

    def test_bad_names(self):
        for bad in (["state:to do 1", "x -1"], ["st*ate 1", "x -1"], ["a 1 bad unit!", "b -1"]):
            with self.assertRaises(L.LedgerError):
                self.led.post("t", bad)
        for bad_id in ("job[1]", "a*", "a b", "a/b", ""):
            with self.assertRaises(L.LedgerError):
                self.led.intent_open(bad_id, ["claim:n =1"])

    def test_r_reserved_accounts_and_units(self):
        for bad in (["suspense:x -1", "equity:x 1"], ["clearing:intent:i:0 1", "a:x -1"],
                    ["intent:planned 1", "a:x -1"], ["a:x 1 delta", "b:y -1 delta"]):
            with self.assertRaises(L.LedgerError, msg=bad):
                self.led.post("t", bad, direct="试图绕过")

    def test_r_guarded_state_requires_intent_or_direct(self):
        self.led.post("new", ["world:created -1", "state:todo 1"])  # 新建实体：允许
        with self.assertRaises(L.LedgerError):
            self.led.post("mv", ["state:todo -1", "state:done 1"])  # 迁移：必须经核实
        with self.assertRaises(L.LedgerError):
            self.led.post("mv", ["state:todo -1", "misc:x 1"])
        self.led.post("mv", ["state:todo -1", "state:done 1"], direct="人工确认已完成")
        v = self.led.verify()
        self.assertTrue(v["ok"])
        self.assertTrue(any("直接改动受保护科目" in w for w in v["warnings"]))
        self.assertFalse(self.led.verify(strict=True)["ok"])


class TestIntegrity(Base):
    def _seed(self):
        self.led.post("t1", ["world:created -2", "pool:a 2"])
        self.led.transfer("t2", "pool:a", "pool:b", 1)
        self.led.transfer("t3", "pool:a", "pool:b", 1)

    def test_tamper_detected_and_blocks_writes(self):
        self._seed()
        lines = self.j.read_text().splitlines()
        rec = json.loads(lines[1])
        rec["postings"][0]["amount"], rec["postings"][1]["amount"] = "-2", "2"
        lines[1] = json.dumps(rec)
        self.j.write_text("\n".join(lines) + "\n")
        v = self.led.verify()
        self.assertFalse(v["ok"])
        self.assertTrue(any("哈希不符" in m for m in v["violations"]))
        with self.assertRaises(L.LedgerError):  # r: 篡改后的账本不能继续记账
            self.led.transfer("t4", "pool:b", "pool:c", 1)
        with self.assertRaises(L.LedgerError):
            self.led.balances()

    def test_r_long_lived_instance_detects_same_length_tamper(self):
        self._seed()
        self.led.balances()  # 建立进程内缓存
        lines = self.j.read_text().splitlines()
        lines[0] = lines[0].replace('"amount":"2"', '"amount":"3"').replace('"amount":"-2"', '"amount":"-3"')
        self.j.write_text("\n".join(lines) + "\n")
        with self.assertRaises(L.LedgerError):
            self.led.transfer("t4", "pool:b", "pool:c", 1)

    def test_cache_sees_other_writers(self):
        self._seed()
        self.led.balances()
        L.Ledger(self.j).transfer("o1", "world:x", "pool:a", 5)
        self.assertEqual(self.bal("pool:a"), "5")
        self.led.transfer("o2", "pool:a", "pool:c", 5)
        self.assertTrue(L.Ledger(self.j).verify()["ok"])

    def test_tamper_with_rehash_caught_by_anchor(self):
        self._seed()
        anchor = self.led.verify()["head"]
        self.rewrite(lambda recs: recs[:1] + recs[2:])
        v = self.led.verify([anchor])
        self.assertFalse(v["ok"])
        self.assertTrue(any("锚点" in m for m in v["violations"]))

    def test_deleted_line_detected(self):
        self._seed()
        lines = self.j.read_text().splitlines()
        del lines[1]
        self.j.write_text("\n".join(lines) + "\n")
        self.assertFalse(self.led.verify()["ok"])

    def test_torn_tail_auto_repair_on_append(self):
        self._seed()
        with open(self.j, "ab") as f:
            f.write(b'{"type":"txn","id":"half')
        self.assertFalse(self.led.verify()["ok"])
        self.led.transfer("t4", "world:x", "pool:a", 1)
        self.assertTrue(any("自动截掉" in n for n in self.led.notices))
        self.assertTrue(self.led.verify()["ok"])
        self.assertTrue(list(self.j.parent.glob("journal.jsonl.torn-*")))

    def test_torn_tail_manual_mode(self):
        led = L.Ledger(self.j, {"auto_repair_torn_tail": False})
        led.post("t1", ["world:created -1", "pool:a 1"])
        with open(self.j, "ab") as f:
            f.write(b'{"type":"txn"')
        with self.assertRaises(L.LedgerError):
            led.transfer("t2", "world:x", "pool:a", 1)
        led.repair_drop_torn_tail()
        led.transfer("t2", "world:x", "pool:a", 1)

    def test_repair_refuses_mid_file_corruption(self):
        self._seed()
        lines = self.j.read_text().splitlines()
        lines[0] = "garbage"
        self.j.write_text("\n".join(lines) + "\n")
        with self.assertRaises(L.LedgerError):
            self.led.repair_drop_torn_tail()
        with self.assertRaises(L.LedgerError):
            self.led.transfer("t9", "world:x", "pool:a", 1)

    def test_r_malformed_records_reported_not_crashing(self):
        self._seed()
        for bad in ({"postings": 5}, {"postings": ["x"]}, {"postings": [{"account": "a:x", "amount": "1"}]}):
            self.rewrite(lambda recs: recs[:2] + [dict(recs[2], **bad)])
            v = self.led.verify()
            self.assertFalse(v["ok"])
            self.assertEqual(self.cli("verify"), 1)
            self.assertEqual(self.cli("balance"), 2)

    def test_concurrent_writers(self):
        procs = [multiprocessing.Process(target=_worker, args=(str(self.j), 25, f"w{k}")) for k in range(4)]
        [p.start() for p in procs]
        for p in procs:
            p.join()
            self.assertEqual(p.exitcode, 0)
        v = self.led.verify()
        self.assertTrue(v["ok"], v["violations"])
        self.assertEqual(v["records"], 100)

    def test_r_symlinked_journal_shares_lock(self):
        link_dir = self.root / "alias"
        link_dir.symlink_to(self.j.parent, target_is_directory=True)
        self.j.parent.mkdir(parents=True, exist_ok=True)
        procs = [multiprocessing.Process(target=_worker, args=(str(path), 20, f"s{k}"))
                 for k, path in enumerate([self.j, link_dir / "journal.jsonl"] * 2)]
        [p.start() for p in procs]
        [p.join() for p in procs]
        v = self.led.verify()
        self.assertTrue(v["ok"], v["violations"][:3])
        self.assertEqual(v["records"], 80)


class TestIntent(Base):
    def setUp(self):
        super().setUp()
        for d in ("tasks/todo", "tasks/done", "out"):
            (self.root / d).mkdir(parents=True)
        (self.root / "tasks/todo/x.md").write_text("x")
        self.led.post("seed", ["world:created -1", "state:todo 1"])

    def move(self):
        os.rename(self.root / "tasks/todo/x.md", self.root / "tasks/done/x.md")

    def open_move(self, iid="i1", **kw):
        return self.led.intent_open(iid, ["exists:tasks/done/x.md =1", "exists:tasks/todo/x.md =0"],
                                    then=["state:todo -1", "state:done 1"], tool="mv", **kw)

    def test_settle_ok_posts_then(self):
        self.open_move()
        self.move()
        r = self.led.intent_settle("i1")
        self.assertEqual(r["status"], "settled")
        self.assertEqual(self.bal("state:done"), "1")
        self.assertEqual(self.led.balances("clearing:*"), {})
        self.assertTrue(self.led.verify(strict=True)["ok"])
        self.led.close("p1")

    def test_plan_treated_as_result_is_caught(self):
        self.open_move()
        r = self.led.intent_settle("i1")
        self.assertEqual(r["status"], "mismatch")
        self.assertEqual(self.bal("state:done"), "0")
        with self.assertRaises(L.CheckFailed):
            self.led.close("p1")

    def test_no_cross_dimension_netting(self):
        self.open_move()
        (self.root / "tasks/done/x.md").write_text("x")  # 复制而非移动
        r = self.led.intent_settle("i1")
        self.assertEqual(r["status"], "mismatch")
        self.assertEqual([x["match"] for x in r["results"]], [True, False])

    def test_resettle_after_retry_is_incremental(self):
        self.open_move()
        self.assertEqual(self.led.intent_settle("i1")["status"], "mismatch")
        self.move()
        self.assertEqual(self.led.intent_settle("i1")["status"], "settled")
        self.assertEqual(self.led.balances("clearing:*"), {})
        self.assertTrue(self.led.intent_settle("i1").get("_duplicate"))
        self.assertEqual(self.bal("state:done"), "1")

    def test_writeoff_to_suspense_blocks_close(self):
        self.open_move()
        self.led.intent_settle("i1")
        self.led.intent_writeoff("i1", "工具超时，结果不明")
        self.assertEqual(self.led.balances("clearing:*"), {})
        self.assertEqual(len(self.led.suspense()), 2)  # 两条期望各自挂账，不相互抵消
        self.assertFalse(self.led.verify(strict=True)["ok"])
        with self.assertRaises(L.CheckFailed):
            self.led.close("p1")
        with self.assertRaises(L.LedgerError):  # r: 不能用普通 post 偷偷清挂账
            self.led.post("clr", ["suspense:intent:i1:0 -1 delta", "loss:x 1 delta"])
        self.led.clear_suspense("suspense:intent:i1:0", "loss:intent", "查明：mv 未执行")
        self.led.clear_suspense("suspense:intent:i1:1", "loss:intent", "同上")
        self.assertEqual(self.led.suspense(), [])
        self.led.close("p1", reviewed_by="张三")

    def test_cancel_only_if_nothing_happened(self):
        self.open_move()
        self.led.intent_cancel("i1", "不做了")
        self.assertEqual(self.led.balances("clearing:*"), {})
        self.open_move("i2")
        self.move()
        with self.assertRaises(L.CheckFailed):
            self.led.intent_cancel("i2", "假装没做")

    def test_r_cancel_refused_when_outcome_unknown(self):
        self.led.intent_open("e1", ["claim:sent =1"])
        with self.assertRaises(L.CheckFailed):
            self.led.intent_cancel("e1", "进程崩了，不知道发没发")

    def test_r_cancelled_intent_cannot_be_reopened_or_settled(self):
        self.open_move()
        self.led.intent_cancel("i1", "x")
        with self.assertRaises(L.CheckFailed):
            self.open_move()
        with self.assertRaises(L.CheckFailed):
            self.led.intent_settle("i1")
        self.assertEqual(self.cli("intent", "settle", "i1"), 1)

    def test_r_vacuous_expectations_rejected(self):
        self.move()  # 文件早就在 done 里
        with self.assertRaises(L.CheckFailed):
            self.led.intent_open("v1", ["exists:tasks/done/x.md =1"], then=["state:todo -1", "state:done 1"])
        with self.assertRaises(L.CheckFailed):
            self.led.intent_open("v2", ["glob-count:out/* +0"])
        with self.assertRaises(L.CheckFailed):
            self.led.intent_open("v3", ["sha256:tasks/done/x.md unchanged"], then=["state:todo -1", "state:done 1"],
                                 allow_noop=True)
        r = self.led.intent_open("v4", ["sha256:tasks/done/x.md unchanged"], allow_noop=True)
        self.assertTrue(r["expects"][0]["vacuous"])

    def test_claim_and_trust_levels(self):
        self.led.intent_open("e1", ["claim:emails_sent =1"])
        self.assertEqual(self.led.intent_settle("e1")["status"], "unknown")  # r3: 缺观测是未知，不是没发生
        self.led.intent_observe("e1", "emails_sent", 1, "agent", "我发了")
        r = self.led.intent_settle("e1")
        self.assertEqual(r["status"], "settled")
        self.assertIn("自证", r["results"][0]["note"])
        self.assertTrue(any("签名" in w for w in self.led.verify()["warnings"]))

    def test_r_source_label_is_not_trust_without_signature(self):
        strict = L.Ledger(self.j, {"reject_self_certified": True})
        strict.intent_open("e2", ["claim:emails_sent =1"])
        strict.intent_observe("e2", "emails_sent", 1, "tool", "我说是工具写的")  # 模型冒充 tool
        self.assertEqual(strict.intent_settle("e2")["status"], "unknown")  # 不采信，结果仍未知
        os.environ["LEDGER_OBSERVER_KEY"] = "harness-secret"
        strict.intent_observe("e2", "emails_sent", 1, "tool", "smtp 250 id=123")  # harness 持钥签名
        r = strict.intent_settle("e2")
        self.assertEqual(r["status"], "settled")

    def test_r_signed_observation_not_overridden_by_later_unsigned(self):
        os.environ["LEDGER_OBSERVER_KEY"] = "k"
        self.led.intent_open("e3", ["claim:rows =5"])
        self.led.intent_observe("e3", "rows", 3, "tool", "db rowcount")  # harness 签名：只有 3
        os.environ.pop("LEDGER_OBSERVER_KEY")
        self.led.intent_observe("e3", "rows", 5, "tool", "模型补一条")   # 未签名
        os.environ["LEDGER_OBSERVER_KEY"] = "k"
        r = self.led.intent_settle("e3")
        self.assertEqual(r["status"], "mismatch")
        self.assertEqual(r["results"][0]["actual"], "3")

    def test_r_expect_templates(self):
        cfg = {"expect_templates": {"mv": {"expect": ["exists:{dst} =1", "exists:{src} =0"],
                                           "then": ["state:todo -1", "state:done 1"]}},
               "require_expect_template": True}
        led = L.Ledger(self.j, cfg)
        with self.assertRaises(L.LedgerError):
            led.intent_open("t1", ["exists:out/whatever =1"], tool="write")  # 无模板的工具
        with self.assertRaises(L.LedgerError):
            led.intent_open("t1", [], tool="mv", args={"dst": "tasks/done/x.md"})  # 缺参数
        with self.assertRaises(L.LedgerError):  # r2: 模型不能自带 then
            led.intent_open("t1", [], tool="mv", args={"dst": "tasks/done/x.md", "src": "tasks/todo/x.md"},
                            then=["state:todo -1", "state:done 1"])
        r = led.intent_open("t1", [], tool="mv", args={"dst": "tasks/done/x.md", "src": "tasks/todo/x.md"})
        self.assertEqual(len(r["then"]), 2)
        self.assertEqual([e["probe"] for e in r["expects"]], ["exists:tasks/done/x.md", "exists:tasks/todo/x.md"])
        self.move()
        self.assertEqual(led.intent_settle("t1")["status"], "settled")

    def test_r_shared_probe_between_open_intents_refused(self):
        (self.root / "tasks/todo/y.md").write_text("y")
        self.led.intent_open("a", ["glob-count:tasks/done/*.md +1"])
        with self.assertRaises(L.CheckFailed):
            self.led.intent_open("b", ["exists:tasks/done/y.md =1"])
        self.led.intent_open("b", ["exists:tasks/done/y.md =1"], allow_shared_probe=True)
        self.led.intent_open("c", ["exists:out/c.txt =1"])  # 不相干路径：允许

    def test_sha256_changed(self):
        f = self.root / "a.txt"
        f.write_text("1")
        self.led.intent_open("h1", ["sha256:a.txt changed", "sha256:tasks/todo/x.md unchanged"])
        self.assertEqual(self.led.intent_settle("h1")["status"], "mismatch")
        f.write_text("2")
        self.assertEqual(self.led.intent_settle("h1")["status"], "settled")

    def test_absolute_expectation(self):
        self.led.intent_open("a1", ["exists:out/report.md =1", "lines:out/report.md =3"])
        (self.root / "out/report.md").write_text("a\nb\nc\n")
        self.assertEqual(self.led.intent_settle("a1")["status"], "settled")

    def test_r_json_probe_before_report_exists(self):
        self.led.intent_open("j1", ["json:out/report.json#failed =0", "sha256:out/report.json changed"])
        self.assertEqual(self.led.intent_settle("j1")["status"], "mismatch")  # 报告还没生成：不符而非崩溃
        (self.root / "out/report.json").write_text('{"failed": 0}')
        self.assertEqual(self.led.intent_settle("j1")["status"], "settled")
        with self.assertRaises(L.LedgerError):
            self.led.intent_open("j2", ["json:out/nope.json#n +1"])  # 变化量需要基线

    def test_r_stale_report_caught_by_changed_guard(self):
        (self.root / "out/report.json").write_text('{"failed": 0}')  # 上次的旧报告
        self.led.intent_open("j3", ["json:out/report.json#failed =0", "sha256:out/report.json changed"])
        self.assertEqual(self.led.intent_settle("j3")["status"], "mismatch")  # 没重新跑测试

    def test_then_must_balance(self):
        with self.assertRaises(L.CheckFailed):
            self.led.intent_open("b1", ["exists:nope =1"], then=["state:done 1"])

    def test_r_then_rejected_keeps_verified_effect(self):
        # 账面与现实不一致：todo 账上为 0，但文件确实被移走了
        self.led.post("fix", ["state:todo -1", "state:done 1"], direct="提前手工标了完成")
        self.open_move("m1")
        self.move()
        r = self.led.intent_settle("m1")
        self.assertEqual(r["status"], "then_rejected")
        self.assertEqual(self.led.balances("clearing:*"), {})  # 效果已核实入账
        with self.assertRaises(L.CheckFailed):
            self.led.intent_writeoff("m1", "想把已完成的事挂成未完成")
        self.led.reverse("fix-r", "fix", "撤回提前标记")
        self.assertEqual(self.led.intent_settle("m1")["status"], "settled")
        self.assertEqual(self.bal("state:done"), "1")

    def test_probe_cannot_escape_root(self):
        for bad in ("exists:../../etc/passwd =1", "exists:/etc/passwd =1", "glob-count:**/../../* +1"):
            with self.assertRaises(L.LedgerError):
                self.led.intent_open("p1", [bad])
        (self.root / "link").symlink_to("/etc")
        with self.assertRaises(L.LedgerError):
            self.led.intent_open("p2", ["exists:link/passwd =1"])

    def test_r_probe_io_errors_are_ledger_errors(self):
        (self.root / "adir").mkdir()
        with self.assertRaises(L.LedgerError):
            self.led.intent_open("d1", ["sha256:adir changed"])
        self.assertEqual(self.cli("intent", "open", "d2", "--expect", "lines:adir +1"), 2)

    def test_intent_idempotent_open(self):
        a = self.open_move()
        b = self.open_move()
        self.assertTrue(b.get("_duplicate"))
        self.assertEqual(a["seq"], b["seq"])
        with self.assertRaises(L.CheckFailed):
            self.led.intent_open("i1", ["exists:zzz =1"])

    def test_crash_recovery_open_intent_visible(self):
        self.open_move()
        self.move()
        fresh = L.Ledger(self.j)
        self.assertEqual(fresh.verify()["unresolved_intents"], 1)
        self.assertFalse(fresh.verify(strict=True)["ok"])
        self.assertEqual(fresh.intent_settle("i1")["status"], "settled")


class TestReconcileBudgetClose(Base):
    def test_reconcile_probe_and_adjust(self):
        (self.root / "tasks/done").mkdir(parents=True)
        led = L.Ledger(self.j, {"reconcile": [{"account": "state:done", "probe": "glob-count:tasks/done/*.md"}]})
        led.post("t1", ["world:created -1", "state:done 1"])  # 账上说完成了 1 个，目录里没有
        self.assertFalse(led.reconcile()["ok"])
        with self.assertRaises(L.LedgerError):  # 调整受保护科目必须写原因
            led.reconcile(adjust=True)
        self.assertTrue(led.reconcile(adjust=True, reason="目录里没有该文件")["adjusted"])
        self.assertEqual(led.balances("state:done"), {})
        self.assertEqual(led.balances("suspense:*"), {"suspense:reconcile:state:done [count]": "1"})
        with self.assertRaises(L.CheckFailed):
            led.close("p")
        self.assertTrue(led.reconcile()["ok"])

    def test_budget_multi_agent(self):
        led = self.led
        led.post("fund", ["equity:funding -1000 tokens", "budget:pool 1000 tokens"])
        led.transfer("g-a", "budget:pool", "budget:agent:A", 400, "tokens")
        led.transfer("g-b", "budget:pool", "budget:agent:B", 400, "tokens")
        led.transfer("s-a", "budget:agent:A", "expense:tokens:A", 350, "tokens")
        led.transfer("r-a", "budget:agent:A", "budget:pool", 50, "tokens")
        led.transfer("s-b", "budget:agent:B", "expense:tokens:B", 300, "tokens")
        with self.assertRaises(L.CheckFailed) as cm:  # B 失联：没退回剩余 100
            led.close("run-1")
        self.assertTrue(any("budget:agent:B" in d for d in cm.exception.details))
        r = led.reconcile({"expense:tokens:A": {"tokens": 350}, "expense:tokens:B": {"tokens": 350}})
        self.assertFalse(r["ok"])  # 供应商计量 700，账上 650：有人漏报
        led.transfer("r-b", "budget:agent:B", "budget:pool", 100, "tokens")
        led.close("run-1")

    def test_r_budget_overspend_recorded_and_flagged(self):
        led = self.led
        led.post("fund", ["equity:funding -100 tokens", "budget:pool 100 tokens"])
        led.transfer("g", "budget:pool", "budget:agent:A", 10, "tokens")
        led.transfer("s", "budget:agent:A", "expense:tokens:A", 15, "tokens")  # 钱已经花了：必须记
        self.assertEqual(self.bal("expense:tokens:A", "tokens"), "15")
        v = led.verify()
        self.assertTrue(v["ok"])
        self.assertTrue(any("标红" in w for w in v["warnings"]))
        with self.assertRaises(L.CheckFailed):
            led.close("p")
        led.transfer("topup", "budget:pool", "budget:agent:A", 5, "tokens", memo="补批超支")
        led.close("p")

    def test_r_reconcile_since_close(self):
        led = self.led
        led.post("fund", ["equity:funding -100 tokens", "budget:pool 100 tokens"])
        led.transfer("s1", "budget:pool", "expense:tokens:A", 30, "tokens")
        self.assertTrue(led.reconcile({"expense:tokens:A": {"tokens": 30}}, since_close=True)["ok"])
        led.close("day1")
        led.transfer("s2", "budget:pool", "expense:tokens:A", 20, "tokens")
        self.assertTrue(led.reconcile({"expense:tokens:A": {"tokens": 20}}, since_close=True)["ok"])  # 当期账单
        self.assertFalse(led.reconcile({"expense:tokens:A": {"tokens": 20}})["ok"])  # 累计口径不符

    def test_require_reconcile_before_close(self):
        led = L.Ledger(self.j, {"require_reconcile_before_close": True})
        led.post("t1", ["world:created -1", "state:todo 1"])
        with self.assertRaises(L.CheckFailed):
            led.close("p")
        led.reconcile({"state:todo": {"count": 1}})
        led.close("p")
        with self.assertRaises(L.CheckFailed):
            led.close("p")  # r: 重复标签

    def test_close_snapshot_detects_rewrite(self):
        self.led.post("t1", ["world:created -1", "state:todo 1"])
        self.led.close("p1")

        def m(recs):
            recs[0]["postings"] = [{"account": "world:created", "amount": "-2", "unit": "count"},
                                   {"account": "state:todo", "amount": "2", "unit": "count"}]
        self.rewrite(m)
        self.assertTrue(any("结账快照" in m for m in self.led.verify()["violations"]))

    def test_r_default_rules_cannot_be_silently_dropped(self):
        led = L.Ledger(self.j, {"rules": [{"match": "misc:*", "min": 0}]})
        led.post("t1", ["world:created -1", "state:todo 1"])
        with self.assertRaises(L.CheckFailed):
            led.post("t2", ["state:todo -2", "state:done 2"], direct="x")
        loose = L.Ledger(self.j, {"replace_default_rules": True})
        loose.post("t3", ["state:todo -2", "state:done 2"], direct="显式放宽")
        self.assertTrue(any("配置被修改" in w for w in loose.verify()["warnings"]))

    def test_r_newer_schema_record_is_warning(self):
        self.led.post("t1", ["world:created -1", "pool:a 1"])
        self.rewrite(lambda recs: recs + [dict(recs[0], type="future_thing", v=99, id="f1")])
        v = self.led.verify()
        self.assertTrue(v["ok"], v["violations"])
        self.assertTrue(any("更新版本" in w for w in v["warnings"]))


class TestRound2(Base):
    def test_r2_reconcile_cannot_move_state_by_netting(self):
        self.led.post("c1", ["world:created -1", "state:todo 1"])
        snap = {"state:done": {"count": 1}, "state:todo": {"count": 0}}
        self.led.reconcile(snap, adjust=True, reason="模型自称已完成")
        self.assertEqual(len(self.led.suspense()), 2)  # 两边各自挂账，不相互抵消
        self.assertFalse(self.led.verify(strict=True)["ok"])
        with self.assertRaises(L.CheckFailed):
            self.led.close("p1")

    def test_r2_reconcile_rejects_system_targets(self):
        for snap in ({"clearing:intent:i:0": {"delta": 0}}, {"intent:planned": {"delta": 0}}, {"x:y": {"delta": 1}}):
            with self.assertRaises(L.LedgerError):
                self.led.reconcile(snap, adjust=True, reason="x")

    def test_r2_creation_shaped_moves_need_direct(self):
        self.led.post("c1", ["world:created -2", "state:todo 2"])
        for bad in (["world:x -1", "world:x 1", "state:todo -1", "state:done 1"],
                    ["world:created -1", "state:todo -1", "state:done 2"],
                    ["state:todo -1", "world:created 1"]):
            with self.assertRaises(L.LedgerError, msg=bad):
                self.led.post("m", bad)

    def test_r2_entity_created_once_with_unit_glob_rule(self):
        led = L.Ledger(self.j, {"rules": [{"match": "world:created", "unit": "task*", "min": -1}]})
        led.post("e1", ["world:created -1 task42", "state:todo 1 task42"])
        with self.assertRaises(L.CheckFailed):
            led.post("e2", ["world:created -1 task42", "state:done 1 task42"])

    def test_r2_strict_uses_trust_recorded_at_settle(self):
        os.environ["LEDGER_OBSERVER_KEY"] = "k"
        self.led.intent_open("e1", ["claim:sent =1"])
        self.led.intent_observe("e1", "sent", 1, "tool", "smtp 250")
        self.led.intent_settle("e1")
        os.environ.pop("LEDGER_OBSERVER_KEY")
        v = self.led.verify(strict=True)  # 模型侧没有密钥也能通过
        self.assertTrue(v["ok"], v["warnings"])
        # 没签名的结算会被列入复核
        self.led.intent_open("e2", ["claim:sent =1"])
        self.led.intent_observe("e2", "sent", 1, "tool", "我说的")
        self.led.intent_settle("e2")
        self.assertFalse(self.led.verify(strict=True)["ok"])

    def test_r2_forged_signature_is_violation(self):
        os.environ["LEDGER_OBSERVER_KEY"] = "real"
        self.led.intent_open("e1", ["claim:sent =1"])
        os.environ["LEDGER_OBSERVER_KEY"] = "guess"
        self.led.intent_observe("e1", "sent", 1, "tool")  # 用错误密钥签名
        os.environ["LEDGER_OBSERVER_KEY"] = "real"
        self.assertFalse(self.led.verify()["ok"])

    def test_r2_overlap_normalization(self):
        (self.root / "tasks/todo").mkdir(parents=True)
        (self.root / "tasks/done").mkdir(parents=True)
        self.led.intent_open("o1", ["exists:tasks/done/42.md =1"])
        for i, bad in enumerate(("./tasks/done/42.md", "tasks//done/42.md", "tasks/../tasks/done/42.md",
                                 "tasks/done", "tasks")):
            with self.assertRaises(L.CheckFailed, msg=bad):
                self.led.intent_open(f"x{i}", [f"exists:{bad} =1"])
        with self.assertRaises(L.CheckFailed):
            self.led.intent_open("g1", ["glob-count:tasks/*/4?.md +1"])
        self.led.intent_open("ok", ["exists:tasks/done/43.md =1"])

    def test_r2_overrides_recorded_and_reviewed(self):
        (self.root / "a.txt").write_text("1")
        r = self.led.intent_open("n1", ["sha256:a.txt unchanged"], allow_noop=True)
        self.assertEqual(r["overrides"], ["allow_noop"])
        self.assertFalse(self.led.verify(strict=True)["ok"])

    def test_r2_writeoff_and_clear_suspense_need_review(self):
        self.led.intent_open("w1", ["exists:out.txt =1"])
        self.led.intent_settle("w1")
        self.led.intent_writeoff("w1", "放弃")
        self.led.clear_suspense("suspense:intent:w1:0", "loss:x", "放弃")
        v = self.led.verify(strict=True)
        self.assertFalse(v["ok"])
        with self.assertRaises(L.CheckFailed):  # r3: 有复核项时必须写明复核人
            self.led.close("p1")
        rec = self.led.close("p1", reviewed_by="张三")  # 人复核后结账
        self.assertEqual(rec["reviewed_by"], "张三")
        self.assertEqual(len(rec["reviewed"]), 2)
        self.assertTrue(self.led.verify(strict=True)["ok"])

    def test_r2_cancel_when_json_report_never_appeared(self):
        self.led.intent_open("t1", ["json:report.json#failed =0", "sha256:report.json changed"])
        self.led.intent_cancel("t1", "测试没跑")

    def test_r2_config_type_validation(self):
        for bad in ({"guarded": "state:*"}, {"reconcile": [{"account": "a"}]}, {"rules": {}},
                    {"expect_templates": {"mv": "exists:{dst} =1"}}):
            with self.assertRaises(L.LedgerError, msg=bad):
                L.Ledger(self.j, bad)

    def test_r2_nan_meta_and_dup_isolation(self):
        with self.assertRaises(L.LedgerError):
            self.led.post("m", ["world:a -1", "pool:a 1"], meta={"x": float("nan")})
        self.led.post("d", ["world:a -1", "pool:a 1"])
        dup = self.led.post("d", ["world:a -1", "pool:a 1"])
        dup["postings"][0]["amount"] = "-999"
        self.led.reverse("dr", "d", "x")
        self.assertEqual(self.bal("pool:a"), "0")


class TestRound3(Base):
    """T0010 实测发现的问题"""

    def test_r3_strict_lists_close_gaps(self):
        self.led.post("f", ["equity:funding -100 tokens", "budget:pool 100 tokens"])
        self.led.transfer("g", "budget:pool", "budget:agent:A", 50, unit="tokens")
        v = self.led.verify(strict=True)
        self.assertFalse(v["ok"])  # 子预算没归零：strict 与 close 口径一致
        self.assertTrue(any("结账门槛" in w and "budget:agent:A" in w for w in v["warnings"]))
        self.assertTrue(self.led.verify()["ok"])  # 非 strict 只是提示
        self.led.transfer("r", "budget:agent:A", "budget:pool", 50, unit="tokens")
        self.assertTrue(self.led.verify(strict=True)["ok"])
        self.led.close("p1")

    def test_r3_close_gaps_not_duplicated_for_intents_and_suspense(self):
        self.led.intent_open("w1", ["exists:out.txt =1"])
        self.assertFalse(any("结账门槛" in w for w in self.led.verify()["warnings"]))  # 未结意图已单独列出
        self.led.intent_settle("w1")
        self.led.intent_writeoff("w1", "放弃")
        self.assertFalse(any("结账门槛" in w for w in self.led.verify()["warnings"]))  # 挂账已单独列出

    def test_r3_close_blockers_not_duplicated_for_open_intent(self):
        self.led.intent_open("e1", ["claim:sent =1"])
        with self.assertRaises(L.CheckFailed) as cm:
            self.led.close("p1")
        self.assertEqual(len(cm.exception.details), 1, cm.exception.details)  # 只列「intent 未结」，不再重复列清账余额

    def test_r3_reconcile_requirement_visible_in_strict(self):
        led = L.Ledger(self.j, {"require_reconcile_before_close": True,
                                "reconcile": [{"account": "pool:a", "probe": "exists:a.txt"}]})
        (self.root / "a.txt").write_text("x")
        led.post("t", ["world:a -1", "pool:a 1"])
        self.assertTrue(any("对账" in w for w in led.verify(strict=True)["warnings"]))
        led.reconcile()
        self.assertTrue(led.verify(strict=True)["ok"])

    def test_r3_unknown_vs_mismatch(self):
        self.led.intent_open("e1", ["claim:sent =1"])
        r = self.led.intent_settle("e1")
        self.assertEqual(r["status"], "unknown")
        self.assertTrue(r["results"][0]["unknown"])
        self.assertEqual([i["intent"] for i in self.led.intents("unknown")], ["e1"])
        self.assertEqual([i["intent"] for i in self.led.intents("unresolved")], ["e1"])
        with self.assertRaises(L.CheckFailed):  # 未知仍不能撤销
            self.led.intent_cancel("e1", "不知道")
        # 有一条确定不符时就是 mismatch
        self.led.intent_open("e2", ["claim:sent =1", "exists:out/mail.log =1"])
        self.assertEqual(self.led.intent_settle("e2")["status"], "mismatch")
        # 报告文件还没生成：确定尚未产生，不是未知
        self.led.intent_open("j1", ["json:report.json#failed =0", "sha256:report.json changed"])
        r = self.led.intent_settle("j1")
        self.assertEqual(r["status"], "mismatch")
        self.assertFalse(any(x.get("unknown") for x in r["results"]))
        # 观测补上后可以结清
        self.led.intent_observe("e1", "sent", 1, "observer", "人工查了发件箱")
        self.assertEqual(self.led.intent_settle("e1")["status"], "settled")
        self.assertEqual(self.cli("intent", "list", "--status", "unknown"), 0)

    def test_r3_unknown_cli_exit_code(self):
        self.led.intent_open("e1", ["claim:sent =1"])
        self.assertEqual(self.cli("intent", "settle", "e1"), 3)  # 未知与失败分开，脚本化调用也能区分
        self.led.intent_open("e2", ["exists:nope =1"])
        self.assertEqual(self.cli("intent", "settle", "e2"), 1)

    def test_r3_timestamps_carry_local_offset(self):
        rec = self.led.post("t", ["world:a -1", "pool:a 1"])
        import datetime as dt
        ts = dt.datetime.fromisoformat(rec["ts"])
        self.assertIsNotNone(ts.tzinfo)
        self.assertEqual(ts.utcoffset(), dt.datetime.now().astimezone().utcoffset())
        self.assertIn(L.local_ts(rec["ts"])[:10], rec["ts"])
        self.assertEqual(L.local_ts("garbage"), "garbage")
        # 老版本写入的 UTC 时间戳也能换算成本地时间
        self.assertEqual(L.local_ts("2026-10-31T22:27:58.494+00:00"),
                         dt.datetime(2026, 10, 31, 22, 27, 58, tzinfo=dt.timezone.utc).astimezone()
                         .strftime("%Y-%m-%d %H:%M:%S%z"))


class TestCLI(Base):
    def test_cli_flow_and_exit_codes(self):
        self.assertEqual(self.cli("init"), 0)
        self.assertTrue((self.j.parent / "config.json").exists())
        self.assertEqual(self.cli("post", "--id", "t1", "world:created -1", "state:todo 1"), 0)
        self.assertEqual(self.cli("post", "--id", "t2", "state:todo -1", "state:done 2"), 1)
        self.assertEqual(self.cli("post", "--id", "t3", "state:todo -1", "state:done 1"), 2)  # 受保护
        self.assertEqual(self.cli("verify", "--quiet"), 0)
        self.assertEqual(self.cli("trial"), 0)
        self.assertEqual(self.cli("intent", "open", "i1", "--expect", "exists:zz =1"), 0)
        self.assertEqual(self.cli("intent", "settle", "i1"), 1)
        self.assertEqual(self.cli("verify", "--strict"), 1)
        self.assertEqual(self.cli("close", "p"), 1)
        self.assertEqual(self.cli("intent", "writeoff", "i1", "--reason", "放弃"), 0)
        self.assertEqual(self.cli("--json", "suspense"), 0)
        self.assertEqual(self.cli("clear-suspense", "suspense:intent:i1:0", "--to", "loss:x", "--reason", "放弃"), 0)
        self.assertEqual(self.cli("close", "p"), 1)  # 核销、清挂账待复核：没写复核人
        self.assertEqual(self.cli("close", "p", "--reviewed-by", "张三"), 0)
        self.assertEqual(self.cli("post", "--id", "bad", "only:one 1"), 1)
        self.assertEqual(self.cli("reverse", "--id", "r", "--of", "nope", "--reason", "x"), 2)
        self.assertEqual(self.cli("post", "--id", "m", "--meta", '{"x":"\\ud800"}', "world:a -1", "pool:a 1"), 2)
        self.assertEqual(self.cli("transfer", "--id", "n", "--from", "world:a", "--to", "pool:a", "--amount=-3"), 2)

    def test_config_unknown_field_rejected(self):
        cfg = self.root / "c.json"
        cfg.write_text(json.dumps({"rulez": []}))
        with self.assertRaises(L.LedgerError):
            L.Ledger(self.j, str(cfg))

    def test_snapshot_shape_errors(self):
        self.led.post("t1", ["world:created -1", "pool:a 1"])
        snap = self.root / "s.json"
        for bad in ("[1]", '{"pool:a": 1}'):
            snap.write_text(bad)
            self.assertEqual(self.cli("reconcile", "--snapshot", str(snap)), 2)


if __name__ == "__main__":
    unittest.main()
