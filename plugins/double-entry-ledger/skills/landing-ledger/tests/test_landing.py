"""landing.py 的测试：演示项目的逐笔结论、门禁三态、核验规则、反向扫描、输入校验。

运行：python3 -m unittest discover -s tests
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import textwrap
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(SKILL, "scripts"))
import landing  # noqa: E402

DEMO = os.path.join(SKILL, "examples", "demo")
PROJ = os.path.join(DEMO, "project")


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = landing.main(list(argv))
    return code, out.getvalue() + err.getvalue()


def load(name):
    with open(os.path.join(DEMO, name), encoding="utf-8") as fh:
        return json.load(fh)


class Project:
    """临时快照：write(rel, text) 建文件；debits / vouchers 写成 JSON。"""

    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name

    def write(self, rel, text):
        p = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(textwrap.dedent(text).lstrip("\n"))
        return rel

    def json(self, name, obj):
        p = os.path.join(self.root, "..", os.path.basename(self.root) + "-" + name)
        with open(p, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, ensure_ascii=False)
        self._extra = getattr(self, "_extra", []) + [p]
        return p

    def close(self):
        for p in set(getattr(self, "_extra", [])):
            if os.path.exists(p):
                os.remove(p)
        self.tmp.cleanup()


def debit(id_, **ev):
    return {"id": id_, "source": "docs/adr.md:3", "doc_says": "unspecified", "claim": "c",
            "code_checkable": True, "evidence": ev}


def landed(id_, defs=(), sites=(), tests=(), **kw):
    v = {"debit_id": id_, "status": "claimed_landed", "defs": list(defs), "sites": list(sites), "tests": list(tests)}
    v.update(kw)
    return v


class TestDemo(unittest.TestCase):
    def test_demo_verdicts(self):
        res = landing.reconcile(PROJ, load("debits.json"), load("vouchers.json"), landing.load_config(None))
        got = {r["id"]: r["verdict"] for r in res["rows"]}
        self.assertEqual(got, {
            "ADR-001#1": "CREDITED_NO_TEST",
            "ADR-001#2": "CREDITED",
            "ADR-002#1": "UNLANDED",
            "ADR-002#2": "SUPERSEDED_UNRECORDED",
            "ADR-002#3": "CREDITED",
            "ADR-003#1": "REJECTED",           # 索引挪到了调用方：机械驳回，进复核清单（导入路径漏了）
            "ADR-004#1": "DEVIATION",
            "ADR-005#1": "REJECTED",           # 文档名和代码名漂移：误报，进复核清单
            "ADR-005#2": "NO_VOUCHER",
        })
        self.assertEqual(res["out_of_scope"], 1)
        self.assertEqual(res["gate"], "block")

    def test_demo_cli_exit_and_doc_done_flag(self):
        code, out = run("reconcile", PROJ, "--debits", os.path.join(DEMO, "debits.json"),
                        "--vouchers", os.path.join(DEMO, "vouchers.json"))
        self.assertEqual(code, 1)
        self.assertIn("门禁：✗ 阻断（4 项）", out)
        self.assertIn("ADR-004#1  docs/decisions/ADR-004-share-links.md:7  ⚠ 文档自称已完成", out)
        self.assertIn("没查（--tests not-run 或没给", out)
        self.assertIn("不在本账范围（流程/运维类，本门禁不对它们负责）：ADR-002#4", out)

    def test_adversarial_voucher_fools_weak_debit_only(self):
        cfg = landing.load_config(None)
        adv = load("adversarial/voucher.json")
        strong = landing.reconcile(PROJ, load("adversarial/debit-strong.json"), adv, cfg)
        weak = landing.reconcile(PROJ, load("adversarial/debit-weak.json"), adv, cfg)
        self.assertEqual(strong["rows"][0]["verdict"], "REJECTED")
        self.assertIn("没有合格的调用点：recordAudit @ notebooks 含 ['DELETE']", strong["rows"][0]["reasons"])
        self.assertEqual(weak["rows"][0]["verdict"], "CREDITED_NO_TEST")   # 被骗过：机械核验通过
        errors, warns = landing.lint(load("adversarial/debit-weak.json"), [])
        self.assertEqual(errors, [])
        self.assertTrue(any("接线约束过弱" in w for w in warns))
        errors, warns = landing.lint(load("adversarial/debit-strong.json"), [])
        self.assertFalse(any("接线约束过弱" in w for w in warns))

    def test_demo_orphans(self):
        res = landing.orphans(PROJ, landing.load_config(None))
        got = {o["symbol"]: o["category"] for o in res["orphans"]}
        self.assertEqual(got, {"verifyWebhookSignature": "tested_unwired", "rateLimit": "tested_unwired",
                               "legacyFormatBytes": "dead", "listUnusedTags": "ops_only"})
        self.assertEqual(res["test_hooks_filtered"], 1)  # __resetLimitsForTest

    def test_demo_lint_clean(self):
        code, out = run("lint", "--debits", os.path.join(DEMO, "debits.json"),
                        "--vouchers", os.path.join(DEMO, "vouchers.json"))
        self.assertEqual(code, 0)
        self.assertIn("1 笔代码层借方没有凭证", out)


class TestGate(unittest.TestCase):
    def setUp(self):
        self.p = Project()
        self.p.write("src/a.ts", """
            export function doThing(x: number) {
              return x + 1;
            }
            """)
        self.p.write("src/api.ts", """
            import { doThing } from './a';
            export function handler() {
              return doThing(1);
            }
            """)
        self.p.write("tests/a.test.ts", "import { doThing } from '../src/a';\ntest('x', () => doThing(1));\n")
        self.d = self.p.json("d.json", [debit("D#1", symbols=["doThing"],
                                              call={"symbol": "doThing", "scope_hint": "api"})])
        self.v = self.p.json("v.json", [landed("D#1", defs=[{"file": "src/a.ts", "line": 1, "symbol": "doThing"}],
                                               sites=[{"file": "src/api.ts", "line": 3, "symbol": "doThing"}],
                                               tests=[{"file": "tests/a.test.ts"}])])

    def tearDown(self):
        self.p.close()

    def test_three_states(self):
        base = ("reconcile", self.p.root, "--debits", self.d, "--vouchers", self.v)
        self.assertEqual(run(*base)[0], 3)                         # 测试没查：不是「全部通过」
        code, out = run(*base, "--tests", "passed")
        self.assertEqual(code, 0)
        self.assertIn("门禁：✓ 通过", out)
        code, out = run(*base, "--tests", "failed")
        self.assertEqual(code, 1)

    def test_json_output(self):
        out = os.path.join(self.p.root, "res.json")
        run("reconcile", self.p.root, "--debits", self.d, "--vouchers", self.v, "--json", out)
        with open(out, encoding="utf-8") as fh:
            res = json.load(fh)
        self.assertEqual(res["gate"], "review")
        self.assertEqual(res["counts"]["CREDITED"], 1)


class TestRules(unittest.TestCase):
    def setUp(self):
        self.p = Project()
        self.cfg = landing.load_config(None)
        self.p.write("docs/adr.md", "# ADR\n\n1. 用 doThing\n2. 旧决定\n\n> 第 2 条已被 spec 取代\n")
        self.p.write("docs/spec.md", "# spec\n\n改成新做法\n")
        self.p.write("src/a.ts", """
            export function doThing(x: number) {
              return x + 1;
            }
            // doThing(2) 在注释里
            """)
        self.p.write("src/w.ts", """
            import { doThing } from './a';
            export function inner() {
              return doThing(1);
            }
            export function outer() {
              return inner();
            }
            """)
        self.p.write("src/gen/entry.ts", """
            import { outer } from '../w';
            export function entry() {
              return outer();
            }
            """)
        self.p.write("tests/a.test.ts", "doThing(1)\n")

    def tearDown(self):
        self.p.close()

    def verdict(self, d, v, cfg=None):
        res = landing.reconcile(self.p.root, [d], [v] if v else [], cfg or self.cfg)
        return res["rows"][0]["verdict"], res["rows"][0]["reasons"]

    def test_site_must_be_prod_code_not_comment_not_definition(self):
        d = debit("D", symbols=["doThing"], call={"symbol": "doThing", "scope_hint": "src"})
        defs = [{"file": "src/a.ts", "line": 1, "symbol": "doThing"}]
        vd, rs = self.verdict(d, landed("D", defs, [{"file": "tests/a.test.ts", "line": 1}]))
        self.assertEqual(vd, "REJECTED")
        self.assertTrue(any("不是生产代码" in r for r in rs))
        vd, rs = self.verdict(d, landed("D", defs, [{"file": "src/a.ts", "line": 4}]))
        self.assertTrue(any("注释" in r for r in rs))
        vd, rs = self.verdict(d, landed("D", defs, [{"file": "src/a.ts", "line": 1, "symbol": "doThing"}]))
        self.assertEqual(vd, "REJECTED")                            # 定义行不是调用点
        self.assertTrue(any("没有合格的调用点" in r for r in rs))

    def test_wrapper_hops(self):
        d = debit("D", symbols=["doThing"], call={"symbol": "doThing", "scope_hint": "gen"})
        v = landed("D", [{"file": "src/a.ts", "line": 1, "symbol": "doThing"}],
                   [{"file": "src/gen/entry.ts", "line": 3, "symbol": "outer"}], [{"file": "tests/a.test.ts"}])
        self.assertEqual(self.verdict(d, v)[0], "CREDITED")         # entry → outer → inner → doThing
        cfg = dict(self.cfg, wrap_hops=1)
        self.assertEqual(self.verdict(d, v, cfg)[0], "REJECTED")

    def test_must_include_and_scope(self):
        d = debit("D", call={"symbol": "doThing", "scope_hint": "gen", "must_include": ["DELETE"]})
        v = landed("D", sites=[{"file": "src/w.ts", "line": 3, "symbol": "doThing"}])
        vd, rs = self.verdict(d, v)
        self.assertEqual(vd, "REJECTED")
        self.assertIn("没有合格的调用点：doThing @ gen 含 ['DELETE']", rs)

    def test_superseded(self):
        d = debit("D", symbols=["x"])
        d["source"] = "docs/adr.md:4"
        self.assertEqual(self.verdict(d, {"debit_id": "D", "status": "superseded",
                                          "supersede_ref": "docs/adr.md:6"})[0], "SUPERSEDED_RECORDED")
        self.assertEqual(self.verdict(d, {"debit_id": "D", "status": "superseded",
                                          "supersede_ref": "docs/spec.md:3"})[0], "SUPERSEDED_UNRECORDED")
        self.assertEqual(self.verdict(d, {"debit_id": "D", "status": "superseded",
                                          "supersede_ref": "docs/nope.md:1"})[0], "BAD_VOUCHER")
        self.assertEqual(self.verdict(d, {"debit_id": "D", "status": "superseded",
                                          "supersede_ref": "docs/spec.md:99"})[0], "BAD_VOUCHER")

    def test_deviation_beats_mechanical_pass(self):
        d = debit("D", symbols=["doThing"])
        v = landed("D", [{"file": "src/a.ts", "line": 1}], [{"file": "src/w.ts", "line": 3}],
                   [{"file": "tests/a.test.ts"}], deviations=["只做了一半"])
        self.assertEqual(self.verdict(d, v)[0], "DEVIATION")

    def test_bad_status_and_missing(self):
        d = debit("D", symbols=["doThing"])
        with self.assertRaises(landing.UsageError):  # 状态不明是输入错误，不是门禁结论
            self.verdict(d, {"debit_id": "D", "status": "done"})
        self.assertEqual(self.verdict(d, None)[0], "NO_VOUCHER")

    def test_write_literal(self):
        d = debit("D", literals=[{"text": "requestId", "role": "write"}])
        v = landed("D", sites=[{"file": "src/w.ts", "line": 3, "symbol": "doThing"}])
        vd, rs = self.verdict(d, v)
        self.assertTrue(any(r.startswith("没有写入侧落点：requestId") for r in rs))

    def test_path_outside_root_is_not_read(self):
        d = debit("D", symbols=["doThing"])
        v = landed("D", sites=[{"file": "../../../../etc/hosts", "line": 1}])
        vd, rs = self.verdict(d, v)
        self.assertTrue(any("落点文件不存在" in r for r in rs))

    def test_strict_blocks_review_items(self):
        d = debit("D", symbols=["missing"])
        v = landed("D", sites=[{"file": "src/w.ts", "line": 3}])
        res = landing.reconcile(self.p.root, [d], [v], self.cfg, tests="passed", strict=True)
        self.assertEqual(res["gate"], "block")
        res = landing.reconcile(self.p.root, [d], [v], self.cfg, tests="passed")
        self.assertEqual(res["gate"], "review")

    def test_duplicate_vouchers_and_debits_refused(self):
        d = debit("D", symbols=["doThing"])
        v = {"debit_id": "D", "status": "admitted_unlanded"}
        with self.assertRaises(landing.UsageError):
            landing.reconcile(self.p.root, [d], [v, landed("D")], self.cfg)
        with self.assertRaises(landing.UsageError):
            landing.reconcile(self.p.root, [d, d], [v], self.cfg)


class TestDefinitions(unittest.TestCase):
    def test_def_re_forms(self):
        cases = {
            "doThing": ["export async function doThing(a) {", "const doThing = (a) => a", "  doThing(a: string): void {",
                        "  private async doThing<T>(a: T) {", "const [doThing, setDoThing] = useState(0)",
                        "  doThing?: string;", "def doThing(a):", "class doThing:", "doThing = 1"],
        }
        for sym, lines in cases.items():
            for ln in lines:
                self.assertTrue(landing.def_re(sym).search(ln), ln)
        self.assertFalse(landing.def_line_re("doThing").search("  doThing(a);"))
        self.assertFalse(landing.def_line_re("doThing").search("  const r = doThing(a);"))
        self.assertTrue(landing.def_line_re("doThing").search("  async doThing(a: string): Promise<void> {"))


class TestOrphans(unittest.TestCase):
    def setUp(self):
        self.p = Project()

    def tearDown(self):
        self.p.close()

    def test_typescript_multiline_import_is_not_a_reference(self):
        self.p.write("src/lib.ts", "export function used() {}\nexport function onlyImported() {}\n")
        self.p.write("src/main.ts", """
            import {
              used,
              onlyImported,
            } from './lib';
            used();
            """)
        res = landing.orphans(self.p.root, landing.load_config(None))
        self.assertEqual([o["symbol"] for o in res["orphans"]], ["onlyImported"])

    def test_python(self):
        self.p.write("pkg/core.py", """
            def wired():
                return 1


            def verify_sig(blob):
                return blob


            def _private():
                pass


            class Store:
                pass
            """)
        self.p.write("pkg/app.py", """
            from pkg.core import (
                wired,
                verify_sig,
                Store,
            )
            __all__ = ["verify_sig"]
            print(wired(), Store())
            """)
        self.p.write("tests/test_core.py", "from pkg.core import verify_sig\nassert verify_sig('x') == 'x'\n")
        res = landing.orphans(self.p.root, landing.load_config(None))
        self.assertEqual({o["symbol"]: o["category"] for o in res["orphans"]}, {"verify_sig": "tested_unwired"})

    def test_orphan_roots(self):
        self.p.write("a/x.ts", "export const lonelyA = 1;\n")
        self.p.write("b/y.ts", "export const lonelyB = 1;\n")
        cfg = landing.load_config(None)
        cfg["orphan_roots"] = ["a/"]
        self.assertEqual([o["symbol"] for o in landing.orphans(self.p.root, cfg)["orphans"]], ["lonelyA"])


class TestInputs(unittest.TestCase):
    def test_lint_errors(self):
        errors, _ = landing.lint([{"claim": "x", "code_checkable": "yes"}],
                                 [{"debit_id": "A", "status": "superseded"}, {"debit_id": "B", "status": "ok"}])
        text = "\n".join(errors)
        for needle in ("缺 id", "code_checkable 必须是", "superseded 必须给 supersede_ref", "status 只能是"):
            self.assertIn(needle, text)

    def test_unknown_config_field(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump({"wrap_hop": 3}, fh)
        try:
            code, out = run("orphans", PROJ, "--config", fh.name)
            self.assertEqual(code, 2)
            self.assertIn("未知字段", out)
        finally:
            os.remove(fh.name)

    def test_bad_json(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            fh.write("{not json")
        try:
            self.assertEqual(run("lint", "--debits", fh.name)[0], 2)
        finally:
            os.remove(fh.name)


class TestReviewRegressions(unittest.TestCase):
    """对抗审查复现过的假通过、崩溃、越界读取（每条都曾经让门禁给出 0 或 traceback）。"""

    DEBIT = {"id": "A#1", "source": "docs/adr.md:3", "claim": "删除前写审计", "code_checkable": True,
             "evidence": {"symbols": ["recordAudit"],
                          "call": {"symbol": "recordAudit", "scope_hint": "notebooks", "must_include": ["DELETE"]}}}

    def setUp(self):
        self.p = Project()
        self.cfg = landing.load_config(None)
        self.p.write("src/audit/log.ts", "export function recordAudit(action: string, id: string) {\n"
                                         "  db.insert({ action, id });\n}\n")
        self.p.write("tests/audit.test.ts", "import { recordAudit } from '../src/audit/log';\nrecordAudit('DELETE', '1');\n")
        self.p.write("docs/adr.md", "# ADR\n\n删除笔记本前必须调用 recordAudit，action 为 DELETE。\n\n> 上一条已被 spec 取代，不再要求。\n")

    def tearDown(self):
        self.p.close()

    def verdict(self, file_text, line, debit=None, path="src/notebooks/notebooks.ts", **vkw):
        self.p.write(path, file_text)
        v = landed("A#1", [{"file": "src/audit/log.ts", "line": 1, "symbol": "recordAudit"}],
                   [{"file": path, "line": line, "symbol": vkw.pop("symbol", "recordAudit")}],
                   [{"file": "tests/audit.test.ts", "symbol": "recordAudit"}], **vkw)
        res = landing.reconcile(self.p.root, [debit or self.DEBIT], [v], self.cfg, tests="passed")
        return res["rows"][0]["verdict"], res["gate"]

    def test_control_real_call_is_credited(self):
        self.assertEqual(self.verdict("export function del(id) {\n  recordAudit('DELETE', id);\n}\n", 2),
                         ("CREDITED", "pass"))

    def test_comments_strings_imports_do_not_count_as_calls(self):
        cases = {
            "import": ("import { recordAudit } from '../audit/log';\nexport function del(id) {\n"
                       "  db.remove(id, { reason: 'DELETE' });\n}\n", 1),
            "import-window": ("import { recordAudit } from '../audit/log';\nimport { db } from '../db';\n"
                              "export function del(id){\n  db.remove(id, { reason: 'DELETE' });\n}\n", 3),
            "block-comment": ("/*\n  TODO: call recordAudit('DELETE', id) here\n*/\nexport const x = 1;\n", 4),
            "trailing-comment": ("export function del(id) {\n  db.remove(id); // TODO recordAudit DELETE\n}\n", 2),
            "string": ("export function del(id) {\n  log.info('skipped recordAudit for DELETE');\n}\n", 2),
            "definition-next-door": ("const ACTIONS = ['DELETE'];\nexport function recordAudit(a, id) {\n"
                                     "  db.insert({ a, id });\n}\n", 3),
        }
        for name, (text, line) in cases.items():
            with self.subTest(name):
                self.assertEqual(self.verdict(text, line)[0], "REJECTED")

    def test_python_docstring_is_not_a_call(self):
        d = json.loads(json.dumps(self.DEBIT))
        text = 'def delete_notebook(id):\n    """Should call recordAudit("DELETE") first -- not done yet."""\n    db.remove(id)\n'
        self.assertEqual(self.verdict(text, 2, d, path="src/notebooks/svc.py")[0], "REJECTED")

    def test_scope_is_matched_on_path_segments_only(self):
        text = ("// see also notebooks module\nimport { x } from '../notebooks/store';\n"
                "export function purge(id) {\n  recordAudit('DELETE', id);\n}\n")
        self.assertEqual(self.verdict(text, 4, path="src/trash/purge.ts")[0], "REJECTED")
        d = json.loads(json.dumps(self.DEBIT))
        d["evidence"]["call"]["scope_hint"] = "notes"
        call = "export function sync(id) {\n  recordAudit('DELETE', id);\n}\n"
        self.assertEqual(self.verdict(call, 2, d, path="src/notesync/sync.ts")[0], "REJECTED")
        self.assertEqual(self.verdict(call, 2, d, path="src/notes/sync.ts")[0], "CREDITED")

    def test_wrapper_body_ends_at_its_closing_brace(self):
        text = ("export function deleteNotebook(id) {\n  removeRow(id, 'DELETE');\n}\n"
                "function removeRow(id, why) {\n  db.remove(id);\n}\n"
                "export function renameNotebook(id) {\n  recordAudit('RENAME', id);\n}\n")
        self.assertEqual(self.verdict(text, 2, symbol="removeRow")[0], "REJECTED")

    def test_ignored_dirs_are_not_production(self):
        self.assertEqual(self.verdict("recordAudit('DELETE', id);\n", 1, path="dist/notebooks/n.js")[0], "REJECTED")

    def test_tests_must_reference_the_debit_by_word(self):
        self.p.write("tests/other.test.ts", "describe('x', () => expect(1).toBe(1));\n")
        self.p.write("tests/logger.test.ts", "recordAuditLogger();\n")
        d = {"id": "A#1", "source": "docs/adr.md:3", "claim": "c", "code_checkable": True,
             "evidence": {"symbols": ["recordAudit"]}}
        for tfile, sym in (("tests/other.test.ts", "describe"), ("tests/logger.test.ts", "recordAudit")):
            v = landed("A#1", [{"file": "src/audit/log.ts", "line": 1, "symbol": sym}],
                       [{"file": "src/audit/log.ts", "line": 1, "symbol": sym}], [{"file": tfile}])
            res = landing.reconcile(self.p.root, [d], [v], self.cfg, tests="passed")
            self.assertEqual(res["rows"][0]["verdict"], "CREDITED_NO_TEST", tfile)

    def test_unanchored_debit_goes_to_review(self):
        d = {"id": "A#1", "source": "docs/adr.md:3", "claim": "c", "code_checkable": True, "evidence": {}}
        v = landed("A#1", sites=[{"file": "src/audit/log.ts", "line": 2, "symbol": "db"}])
        res = landing.reconcile(self.p.root, [d], [v], self.cfg, tests="passed")
        self.assertEqual((res["rows"][0]["verdict"], res["gate"]), ("UNANCHORED", "review"))

    def test_write_literal_is_not_satisfied_by_a_read(self):
        d = json.loads(json.dumps(self.DEBIT))
        d["evidence"]["literals"] = [{"text": "requestId", "role": "write"}]
        read = "export function del(req) {\n  const rid = req.headers.requestId;\n  recordAudit('DELETE', rid);\n}\n"
        self.assertEqual(self.verdict(read, 3, d)[0], "REJECTED")
        write = "export function del(req) {\n  const row = { requestId: req.id };\n  recordAudit('DELETE', row);\n}\n"
        self.assertEqual(self.verdict(write, 3, d)[0], "CREDITED")

    def test_supersede_ref_must_point_at_a_reversal(self):
        def sv(ref):
            v = {"debit_id": "A#1", "status": "superseded", "supersede_ref": ref}
            return landing.reconcile(self.p.root, [self.DEBIT], [v], self.cfg)["rows"][0]["verdict"]
        self.assertEqual(sv("docs/adr.md:3"), "SUPERSEDED_UNRECORDED")     # 指向借方出处本身
        self.assertEqual(sv("docs/adr.md:1"), "SUPERSEDED_UNRECORDED")     # 那一行没有取代说明
        self.assertEqual(sv("docs/adr.md"), "BAD_VOUCHER")                 # 没写行号
        self.assertEqual(sv("docs/adr.md:\u00b2"), "BAD_VOUCHER")          # 非 ASCII 数字不当行号
        self.assertEqual(sv("docs/adr.md:5"), "SUPERSEDED_RECORDED")
        self.assertEqual(sv("./docs//adr.md:5"), "SUPERSEDED_RECORDED")     # 路径写法不同也认

    def test_symlink_outside_root_is_not_read(self):
        outside = tempfile.NamedTemporaryFile("w", suffix=".ts", delete=False)
        outside.write("recordAudit('DELETE', id);\n")
        outside.close()
        try:
            os.makedirs(os.path.join(self.p.root, "src/notebooks"), exist_ok=True)
            os.symlink(outside.name, os.path.join(self.p.root, "src/notebooks/link.ts"))
            v = landed("A#1", [{"file": "src/audit/log.ts", "line": 1}], [{"file": "src/notebooks/link.ts", "line": 1}])
            res = landing.reconcile(self.p.root, [self.DEBIT], [v], self.cfg, tests="passed")
            self.assertEqual(res["rows"][0]["verdict"], "REJECTED")
            self.assertIn("落点文件不存在：src/notebooks/link.ts", res["rows"][0]["reasons"])
        finally:
            os.remove(outside.name)

    def test_empty_debits_never_pass(self):
        res = landing.reconcile(self.p.root, [], [], self.cfg, tests="passed")
        self.assertEqual(res["gate"], "review")

    def test_malformed_inputs_are_usage_errors(self):
        good = landed("A#1", [{"file": "src/audit/log.ts", "line": 1}], [{"file": "src/audit/log.ts", "line": 1}])
        bad_debits = [
            dict(self.DEBIT, code_checkable="true"),
            {k: v for k, v in self.DEBIT.items() if k != "code_checkable"},
            dict(self.DEBIT, evidence=["x"]),
            dict(self.DEBIT, evidence={"literals": ["requestId"]}),
            dict(self.DEBIT, evidence={"literals": [{"text": 42}]}),
            dict(self.DEBIT, evidence={"call": "recordAudit"}),
            dict(self.DEBIT, evidence={"call": {"symbol": "recordAudit", "scope_hint": ["notebooks"]}}),
            dict(self.DEBIT, evidence={"call": {"symbol": "recordAudit", "must_include": "DELETE"}}),
            dict(self.DEBIT, evidence={"call": {"symbol": 7}}),
        ]
        bad_vouchers = [
            dict(good, sites="src/a.ts:2"), dict(good, tests="x"), dict(good, defs={"file": "a"}),
            dict(good, debit_id=["A#1"]), dict(good, sites=[{"file": 5, "line": 1}]),
            dict(good, sites=[{"file": "src/a.ts", "line": "2"}]), dict(good, status="done"),
        ]
        for d in bad_debits:
            with self.subTest(debit=d):
                with self.assertRaises(landing.UsageError):
                    landing.reconcile(self.p.root, [d], [good], self.cfg)
        for v in bad_vouchers:
            with self.subTest(voucher=v):
                with self.assertRaises(landing.UsageError):
                    landing.reconcile(self.p.root, [self.DEBIT], [v], self.cfg)
        dp = self.p.json("bad-d.json", [dict(self.DEBIT, evidence={"call": "x"})])
        self.assertEqual(run("lint", "--debits", dp)[0], 1)                       # lint 报错，不崩
        vp = self.p.json("v.json", [good])
        self.assertEqual(run("reconcile", self.p.root, "--debits", dp, "--vouchers", vp)[0], 2)

    def test_bad_config_values(self):
        for cfg in ({"test_patterns": ["("]}, {"window": {"site": "2"}}, {"window": None}, {"wrap_hops": True}):
            path = self.p.json("cfg.json", cfg)
            with self.subTest(cfg=cfg):
                self.assertEqual(run("orphans", self.p.root, "--config", path)[0], 2)


class TestOrphanForms(unittest.TestCase):
    def setUp(self):
        self.p = Project()

    def tearDown(self):
        self.p.close()

    def scan(self):
        return {o["symbol"]: o["category"] for o in landing.orphans(self.p.root, landing.load_config(None))["orphans"]}

    def test_typescript_export_forms(self):
        self.p.write("src/a.ts", "export function aImpl() {}\n")
        self.p.write("src/index.ts", "export { aImpl as a } from './a';\n")
        self.p.write("src/main.ts", "import { a } from './index';\na();\n")
        self.p.write("src/color.ts", "export const enum Color { Red }\n")
        self.p.write("src/b.ts", "function deadB() {}\nfunction liveB() {}\nexport { deadB, liveB };\n")
        self.p.write("src/useb.ts", "import { liveB } from './b';\nliveB();\n")
        self.p.write("src/d.ts", "export function deadD() {}\n")
        self.p.write("src/used.tsx", "import React, {\n  deadD,\n} from './d';\nexport default function App() { return React; }\n")
        self.p.write("src/rec.ts", "export function walk(n) {\n  if (n) {\n    walk(n - 1);\n  }\n}\n")
        got = self.scan()
        self.assertNotIn("aImpl", got)
        self.assertNotIn("enum", got)
        self.assertEqual(got.get("Color"), "dead")
        self.assertEqual(got.get("deadB"), "dead")
        self.assertNotIn("liveB", got)
        self.assertEqual(got.get("deadD"), "dead")
        self.assertEqual(got.get("walk"), "dead")                  # 自递归不算有人调用

    def test_comment_mentions_are_not_references(self):
        self.p.write("src/x.ts", "export function lonely() {}\n")
        self.p.write("src/y.ts", "/*\n  lonely() 以后再接\n*/\nexport const y = 'lonely';\n")
        self.p.write("src/z.ts", "import { y } from './y';\nconsole.log(y);\n")
        self.assertEqual(self.scan(), {"lonely": "dead"})


if __name__ == "__main__":
    unittest.main()
