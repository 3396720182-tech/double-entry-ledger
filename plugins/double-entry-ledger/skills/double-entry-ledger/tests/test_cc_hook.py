"""python3 -m unittest discover -s tests   （在 skill 根目录运行）

Claude Code 钩子适配器：用子进程按真实钩子协议（stdin JSON → stdout JSON）驱动 cc_hook.py。
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
HOOK = SCRIPTS / "cc_hook.py"
sys.path.insert(0, str(SCRIPTS))
import cc_hook as H  # noqa: E402
import ledger as L  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(os.path.realpath(self.tmp.name))
        self.n = 0
        self.sess = "s-" + uuid.uuid4().hex

    def tearDown(self):
        self.tmp.cleanup()

    def hook(self, event, payload, env=None):
        e = dict(os.environ, CLAUDE_PROJECT_DIR=str(self.root))
        e.pop("LEDGER_CC_HOOKS", None)
        e.update(env or {})
        data = dict({"session_id": self.sess, "cwd": str(self.root), "prompt_id": "p1"}, **payload)
        p = subprocess.run([sys.executable, str(HOOK), event], input=json.dumps(data), capture_output=True,
                           text=True, env=e, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        return json.loads(p.stdout) if p.stdout.strip() else {}

    def call(self, tool, tool_input, act, fail=False):
        """完整走一次 Pre → 执行 → Post（或 PostToolUseFailure）。返回 (pre_out, post_out, tool_use_id)。"""
        self.n += 1
        tid = f"toolu_{self.n:04d}"
        pre = self.hook("pre", {"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": tool_input,
                                "tool_use_id": tid})
        if pre.get("hookSpecificOutput", {}).get("permissionDecision") == "deny":
            return pre, None, tid
        act()
        ev = "fail" if fail else "post"
        post = self.hook(ev, {"hook_event_name": "PostToolUseFailure" if fail else "PostToolUse", "tool_name": tool,
                              "tool_input": tool_input, "tool_use_id": tid})
        return pre, post, tid

    def stop(self, active=False):
        return self.hook("stop", {"hook_event_name": "Stop", "stop_hook_active": active,
                                  "last_assistant_message": "done"})

    def led(self):
        return L.Ledger(self.root / ".ledger" / "journal.jsonl")

    def status(self, iid):
        return {i["intent"]: i["status"] for i in self.led().intents()}.get(iid)

    def w(self, rel, text="x"):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        return p

    def ops(self, cmd, cwd=None):
        return H.bash_ops(str(self.root), str(cwd or self.root), cmd)


class TestBashParse(Base):
    def eff(self, cmd, cwd=None):
        out = {}
        for _, e in self.ops(cmd, cwd):
            out.update(e)
        return out

    def test_mv_file_and_into_dir(self):
        self.w("todo/a.md")
        (self.root / "done").mkdir()
        self.assertEqual(self.eff("mv todo/a.md done/"), {"todo/a.md": "absent", "done/a.md": "exists"})
        self.assertEqual(self.eff("mv todo/a.md todo/b.md"), {"todo/a.md": "absent", "todo/b.md": "exists"})

    def test_mv_glob_and_multiple_sources(self):
        self.w("todo/a.md")
        self.w("todo/b.md")
        (self.root / "done").mkdir()
        self.assertEqual(self.eff("mv todo/*.md done"),
                         {"todo/a.md": "absent", "done/a.md": "exists", "todo/b.md": "absent", "done/b.md": "exists"})

    def test_mv_to_project_root(self):
        self.w("sub/a.md")
        self.assertEqual(self.eff("mv sub/a.md ."), {"sub/a.md": "absent", "a.md": "exists"})

    def test_rm_mkdir_touch_cp_tee_redirect(self):
        self.w("a.txt")
        self.assertEqual(self.eff("rm -f a.txt && mkdir -p -m 755 out/x && touch out/x/k && cp a.txt b.txt"
                                  " ; echo hi > c.txt ; echo hi | tee d.txt"),
                         {"a.txt": "absent", "out/x": "exists", "out/x/k": "exists", "b.txt": "exists",
                          "c.txt": "exists", "d.txt": "exists"})

    def test_sed_in_place_bsd_and_gnu_with_glob(self):
        self.w("p/a/pkg.toml")
        self.w("p/b/pkg.toml")
        self.assertEqual(self.eff("sed -i '' 's/1.4.2/1.5.0/' p/*/pkg.toml"),  # 多个文件：账外核对
                         {"p/a/pkg.toml": "maybe-changed", "p/b/pkg.toml": "maybe-changed"})
        self.assertEqual(self.eff("sed -i.bak -e 's/a/b/' -e 's/c/d/' p/a/pkg.toml"), {"p/a/pkg.toml": "changed"})
        self.assertEqual(self.eff("sed 's/a/b/' p/a/pkg.toml > out.toml"), {"out.toml": "exists"})

    def test_each_independent_op_is_its_own_intent(self):
        self.w("a")
        self.w("b")
        groups = self.ops("rm a; rm b")
        self.assertEqual(len(groups), 2)
        groups = self.ops("mkdir d && touch d/x && mv d/x d/y")
        self.assertEqual(len(groups), 1)  # 同一路径被多段改动：并成一笔
        self.assertEqual(groups[0][1], {"d": "exists", "d/x": "absent", "d/y": "exists"})

    def test_swallowed_failure_still_tracked(self):
        self.w("a")
        self.assertEqual(self.eff("mv a b 2>/dev/null || true"), {"a": "absent", "b": "exists"})

    def test_real_fallback_branch_not_tracked(self):
        self.w("a")
        self.assertEqual(self.eff("mv a b || cp a b"), {})

    def test_cd_changes_base(self):
        self.w("sub/a")
        self.assertEqual(self.eff("cd sub && mv a b"), {"sub/a": "absent", "sub/b": "exists"})
        self.assertEqual(self.eff("cd $X && mv a b"), {})

    def test_unknown_control_vars_background_outside(self):
        self.assertEqual(self.eff("python3 build.py"), {})
        self.assertEqual(self.eff("for f in *.md; do mv $f done/; done"), {})
        self.assertEqual(self.eff("mv $SRC done/"), {})
        self.assertEqual(self.eff("touch a &"), {})
        self.assertEqual(self.eff("touch /etc/x ../outside"), {})
        self.assertEqual(self.eff("rm -rf .ledger"), {})

    def test_function_definition_skipped(self):
        self.w("a")
        self.assertEqual(self.eff('L() { python3 /x/ledger.py "$@"; }; L intent open t --expect "exists:b =1"; mv a b'),
                         {"a": "absent", "b": "exists"})

    def test_ops_before_unknown_command_not_checked(self):
        # 改名之后紧跟着项目脚本：脚本可能把刚改名的文件又挪走，前面的改名不再核对（避免误报）
        self.w("todo/rel-07.md")
        groups, unknown = H.bash_parse(str(self.root), str(self.root),
                                       "mv todo/rel-07.md todo/rel-7.md && tools/close_task.sh rel-7 && ls done")
        self.assertEqual(groups, [])
        self.assertEqual(unknown, ["close_task.sh"])
        # 只读命令不算：sed 之后 grep 检查，sed 照样核对
        self.w("a.toml")
        self.assertEqual(self.eff("sed -i '' 's/a/b/' a.toml && grep -n b a.toml && git diff"), {"a.toml": "changed"})
        # 看不懂的命令之后的操作照样核对
        self.assertEqual(self.eff("python3 gen.py && mv a.toml b.toml"), {"a.toml": "absent", "b.toml": "exists"})
        # 推不出参数的 mv（-n）也可能改文件
        self.assertEqual(self.eff("touch x && mv -n a.toml c.toml"), {})

    def test_heredoc_only_first_line(self):
        self.assertEqual(self.eff("cat > notes.md <<'EOF'\nrm -rf everything\nEOF"), {"notes.md": "exists"})

    def test_git_mv_rm(self):
        self.w("a")
        self.w("b")
        self.assertEqual(self.eff("git mv a c && git rm -q b"), {"a": "absent", "c": "exists", "b": "absent"})
        self.assertEqual(self.eff("git rm --cached b"), {})


class TestToolIntents(Base):
    def test_write_new_file_settles_silently(self):
        f = self.root / "out" / "a.md"
        pre, post, tid = self.call("Write", {"file_path": str(f), "content": "hi"},
                                   lambda: (f.parent.mkdir(), f.write_text("hi")))
        self.assertEqual(pre, {})
        self.assertEqual(post, {})
        self.assertEqual(self.status(f"cc-{tid}"), "settled")

    def test_write_same_content_opens_nothing(self):
        f = self.w("a.md", "same")
        pre, post, tid = self.call("Write", {"file_path": str(f), "content": "same"}, lambda: None)
        self.assertFalse((self.root / ".ledger" / "journal.jsonl").exists())

    def test_edit_that_changes_nothing_is_flagged(self):
        f = self.w("a.md", "v1")
        _, post, tid = self.call("Edit", {"file_path": str(f), "old_string": "v1", "new_string": "v2"}, lambda: None)
        self.assertEqual(post["decision"], "block")
        self.assertIn("sha256:a.md changed", post["reason"])
        self.assertEqual(self.status(f"cc-{tid}"), "mismatch")

    def test_sed_partial_no_match_lists_unchanged_files(self):
        a = self.w("p/a.toml", 'version = "1.4.2"\n')
        self.w("p/b.toml", 'version="1.4.2"\n')

        def run():  # 模拟 sed：只有格式对得上的那个文件被改
            a.write_text('version = "1.5.0"\n')
        _, post, tid = self.call("Bash", {"command": "sed -i '' 's/version = \"1.4.2\"/version = \"1.5.0\"/' p/a.toml p/b.toml"}, run)
        ctx = post["hookSpecificOutput"]["additionalContext"]  # 部分没变：提示，不记成未兑现
        self.assertIn("p/b.toml", ctx)
        self.assertNotIn("p/a.toml", ctx.split("内容没变")[1])
        self.assertEqual(self.stop().get("hookSpecificOutput"), None)

    def test_sed_matching_nothing_is_flagged(self):
        self.w("p/a.toml", 'v=1')
        self.w("p/b.toml", 'v=1')
        _, post, _ = self.call("Bash", {"command": "sed -i '' 's/v = 1/v = 2/' p/a.toml p/b.toml"}, lambda: None)
        self.assertEqual(post["decision"], "block")
        self.assertIn("一处都没匹配到", post["reason"])
        f = self.w("one.toml", "v=1")
        _, post, _ = self.call("Bash", {"command": "sed -i '' 's/v = 1/v = 2/' one.toml"}, lambda: None)
        self.assertIn("✗ sha256:one.toml changed", post["reason"])

    def test_tool_failure_without_effect_is_cancelled_quietly(self):
        self.w("a")
        _, post, tid = self.call("Bash", {"command": "mv a missing_dir/b"}, lambda: None, fail=True)
        self.assertEqual(post, {})
        self.assertEqual(self.status(f"cc-{tid}"), "cancelled")

    def test_tool_failure_with_partial_effect_stays_open(self):
        self.w("a")
        self.w("b")

        def run():
            (self.root / "a").unlink()
        _, post, tid = self.call("Bash", {"command": "rm a && rm b"}, run, fail=True)
        # rm a 与 rm b 是两笔：a 已结清，b 撤销（没动）——都不该打扰
        self.assertEqual(post, {})
        _, post, tid = self.call("Bash", {"command": "mkdir d && touch d/x"}, lambda: (self.root / "d").mkdir(), fail=True)
        self.assertIn("部分发生", post["hookSpecificOutput"]["additionalContext"])

    def test_retry_supersedes_previous_no_effect_intent(self):
        self.w("a")
        _, post, t1 = self.call("Bash", {"command": "mv a b 2>/dev/null || true"}, lambda: None)
        self.assertEqual(self.status(f"cc-{t1}"), "mismatch")
        _, post, t2 = self.call("Bash", {"command": "mv a b"}, lambda: (self.root / "a").rename(self.root / "b"))
        self.assertEqual(post, {})
        self.assertEqual(self.status(f"cc-{t1}"), "cancelled")
        self.assertEqual(self.status(f"cc-{t2}"), "settled")

    def test_pre_without_post_is_cancelled_at_stop(self):
        self.w("a")
        self.hook("pre", {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": "rm a"},
                          "tool_use_id": "toolu_denied"})
        out = self.stop()
        self.assertEqual(self.status("cc-toolu_denied"), "cancelled")
        self.assertIn("已结账", out.get("systemMessage", ""))

    def test_clash_with_agent_task_intent_skips_tool_intent(self):
        self.w("todo/a.md")
        led = L.Ledger(self.root / ".ledger" / "journal.jsonl")
        led.intent_open("task-a", ["exists:done/a.md =1"])
        _, post, tid = self.call("Bash", {"command": "mkdir -p done && mv todo/a.md done/a.md"},
                                 lambda: ((self.root / "done").mkdir(), (self.root / "todo/a.md").rename(self.root / "done/a.md")))
        self.assertIsNone(self.status(f"cc-{tid}"))
        self.assertEqual(post, {})
        self.stop()
        self.assertEqual(self.status("task-a"), "settled")

    def test_overlap_with_task_intent_still_checked_off_ledger(self):
        self.w("todo/a.md")
        led = L.Ledger(self.root / ".ledger" / "journal.jsonl")
        led.intent_open("task-a", ["exists:done/a.md =1"])
        (self.root / "done").mkdir()
        _, post, tid = self.call("Bash", {"command": "mv todo/a.md done/a.md 2>/dev/null || true"}, lambda: None)
        self.assertIn("✗ exists:done/a.md =1", post["reason"])
        self.assertIn("task-a", post["reason"])
        self.assertIsNone(self.status(f"cc-{tid}"))


class TestNudge(Base):
    def pre(self, cmd, tid, sess=None):
        return self.hook("pre", {"tool_name": "Bash", "tool_input": {"command": cmd}, "tool_use_id": tid,
                                 "session_id": sess or self.sess})

    def test_script_without_task_intents_nudged_once_per_session(self):
        out = self.pre("tools/close_task.sh rel-1", "t1")
        self.assertIn("任务意图", out["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(self.pre("tools/close_task.sh rel-2", "t2"), {})
        self.assertIn("hookSpecificOutput", self.pre("tools/close_task.sh rel-3", "t3", sess=self.sess + "-2"))
        self.assertFalse((self.root / ".ledger").exists())  # 只为提醒不在项目里建账本

    def test_no_nudge_when_task_intents_exist_or_read_only(self):
        self.assertEqual(self.pre("ls -la && cat README.md | grep x", "t0"), {})
        L.Ledger(self.root / ".ledger" / "journal.jsonl").intent_open("task-a", ["exists:a =1"])
        self.assertEqual(self.pre("tools/build.sh", "t1"), {})


class TestStop(Base):
    def test_problem_reported_at_post_is_repeated_once_at_stop(self):
        f = self.w("a.md", "v1")
        _, post, tid = self.call("Edit", {"file_path": str(f), "old_string": "v1", "new_string": "v2"}, lambda: None)
        self.assertEqual(post["decision"], "block")
        self.assertIn(f"cc-{tid}", self.stop()["hookSpecificOutput"]["additionalContext"])
        self.assertIn(f"cc-{tid}", self.stop(active=True)["systemMessage"])

    def test_unfulfilled_task_intent_prompts_claude_once_then_user(self):
        led = L.Ledger(self.root / ".ledger" / "journal.jsonl")
        led.intent_open("task-docs", ["exists:docs/site/index.html =1"])
        out = self.stop()
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("task-docs", ctx)
        self.assertIn("exists:docs/site/index.html =1", ctx)
        out2 = self.stop(active=True)  # 同一组问题不再拦第二次，只给用户看
        self.assertNotIn("hookSpecificOutput", out2)
        self.assertIn("task-docs", out2["systemMessage"])
        self.w("docs/site/index.html")
        out3 = self.stop()
        self.assertEqual(self.status("task-docs"), "settled")
        self.assertIn("已结账", out3["systemMessage"])
        anchors = (self.root / ".ledger" / "anchors.txt").read_text()
        self.assertIn(self.led().verify()["head"], anchors)

    def test_new_problem_prompts_again(self):
        led = L.Ledger(self.root / ".ledger" / "journal.jsonl")
        led.intent_open("t1", ["exists:x =1"])
        self.assertIn("hookSpecificOutput", self.stop())
        led.intent_open("t2", ["exists:y =1"])
        self.assertIn("t2", self.stop()["hookSpecificOutput"]["additionalContext"])

    def test_never_prompts_again_in_a_hook_continued_turn(self):
        # 账面一直在变（比如日志在长）也不能让 Stop 无限续轮：钩子让它继续的那一轮只给用户看
        led = L.Ledger(self.root / ".ledger" / "journal.jsonl")
        led.intent_open("grow", ["lines:app.log +100"])
        self.assertIn("hookSpecificOutput", self.stop())
        for i in range(3):
            self.w("app.log", "x\n" * (i + 1))
            out = self.stop(active=True)
            self.assertNotIn("hookSpecificOutput", out)
            self.assertIn("grow", out["systemMessage"])

    def test_review_items_block_autoclose_and_never_fill_reviewer(self):
        led = L.Ledger(self.root / ".ledger" / "journal.jsonl")
        led.intent_open("t1", ["exists:x =1"])
        led.intent_settle("t1")
        led.intent_writeoff("t1", "放弃")
        led.clear_suspense("suspense:intent:t1:0", "expense:abandoned", "人确认放弃")
        out = self.stop()
        self.assertIn("--reviewed-by", out["systemMessage"])
        self.assertEqual(self.led().verify()["last_close"], None)

    def test_no_ledger_no_output(self):
        self.assertEqual(self.stop(), {})
        self.assertFalse((self.root / ".ledger").exists())

    def test_corrupt_journal_fails_open_with_notice(self):
        self.w(".ledger/journal.jsonl", "garbage\n")
        self.assertEqual(self.hook("pre", {"tool_name": "Bash", "tool_input": {"command": "touch a"},
                                           "tool_use_id": "t"}), {})
        self.assertIn("出错", self.stop()["systemMessage"])
        self.assertIn("出错", (self.root / ".ledger" / "cc-hook.log").read_text())

    def test_disabled_by_env(self):
        out = self.hook("pre", {"tool_name": "Bash", "tool_input": {"command": "touch a"}, "tool_use_id": "t"},
                        env={"LEDGER_CC_HOOKS": "off"})
        self.assertEqual(out, {})
        self.assertFalse((self.root / ".ledger").exists())


class TestGuard(Base):
    def deny(self, tool, ti):
        out = self.hook("pre", {"tool_name": tool, "tool_input": ti, "tool_use_id": "g"})
        return out.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"

    def test_ledger_dir_writes_denied(self):
        self.assertTrue(self.deny("Write", {"file_path": str(self.root / ".ledger" / "journal.jsonl"), "content": ""}))
        self.assertTrue(self.deny("Edit", {"file_path": str(self.root / ".ledger" / "config.json"),
                                           "old_string": "a", "new_string": "b"}))
        self.assertTrue(self.deny("Bash", {"command": "echo '{}' >> .ledger/journal.jsonl"}))
        self.assertTrue(self.deny("Bash", {"command": "rm -rf .ledger"}))
        self.assertTrue(self.deny("Bash", {"command": "cd .ledger && rm journal.jsonl"}))
        self.assertTrue(self.deny("Bash", {"command": "python3 -c \"open('.ledger/journal.jsonl','a').write('x')\""}))
        self.assertFalse(self.deny("Bash", {"command": "cat .ledger/journal.jsonl | head"}))
        self.assertFalse(self.deny("Bash", {"command": "L() { python3 /x/ledger.py \"$@\"; }; [ -d .ledger ] || L init"}))

    def test_human_only_ledger_commands_denied(self):
        L_ = "python3 /x/skills/double-entry-ledger/scripts/ledger.py"
        self.assertTrue(self.deny("Bash", {"command": f"{L_} close c1 --reviewed-by me"}))
        self.assertTrue(self.deny("Bash", {"command": f"{L_} intent observe x --metric sent --value 1 --source tool"}))
        self.assertTrue(self.deny("Bash", {"command": f"{L_} post --id x 'state:todo -1' 'state:done 1' --direct fix"}))
        self.assertTrue(self.deny("Bash", {"command": f"{L_} --journal /tmp/j.jsonl verify"}))
        self.assertFalse(self.deny("Bash", {"command": f"{L_} intent open task-a --expect 'exists:a =1' --memo 'close the loop'"}))
        self.assertFalse(self.deny("Bash", {"command": f"{L_} verify --strict"}))


class TestReviewRegressions(Base):
    """对抗审查（第三轮，钩子适配器）发现的问题，逐条防回归。"""

    def eff(self, cmd):
        out = {}
        for _, e in self.ops(cmd):
            out.update(e)
        return out

    def test_r_brace_expansion_not_inferred(self):
        self.assertEqual(self.eff("mkdir -p src/{api,ui}/utils"), {})
        self.assertEqual(self.eff("touch {a,b}.txt"), {})

    def test_r_case_only_rename(self):
        f = self.w("readme.md")
        self.assertEqual(self.eff("mv readme.md README.md"), {"README.md": "exists"})
        _, post, tid = self.call("Bash", {"command": "git mv readme.md README.md"},
                                 lambda: f.rename(self.root / "README.md"))
        self.assertEqual(post, {})

    def test_r_cp_directory_contents_not_inferred(self):
        (self.root / "tpl").mkdir()
        self.assertEqual(self.eff("cp -R tpl/. dest/"), {})
        self.assertEqual(self.eff("cp -R tpl/ dest"), {})

    def test_r_conditional_and_chain_not_inferred(self):
        self.w("f")
        self.assertEqual(self.eff("grep -q foo f && sed -i '' s/foo/bar/ f; echo ok"), {})
        self.assertEqual(self.eff("[ -f old.cfg ] && mv old.cfg new.cfg; echo ok"), {})
        self.assertEqual(self.eff("[ -f old.cfg ] && mv old.cfg new.cfg; touch done.txt"), {"done.txt": "exists"})

    def test_r_pushd_and_hash_inside_word_and_heredoc_tail(self):
        self.w("sub/a")
        self.assertEqual(self.eff("pushd sub && mv a b"), {"sub/a": "absent", "sub/b": "exists"})
        self.assertEqual(self.eff("echo x > a.html; echo a#b; mv a.html c.html"), {"a.html": "absent", "c.html": "exists"})
        self.assertEqual(self.eff("mkdir -p d\ncat > d/x.md <<'E'\nrm -rf d\nE\nmv d/x.md d/y.md"),
                         {"d": "exists", "d/x.md": "absent", "d/y.md": "exists"})
        self.assertEqual(self.eff("echo hi # mv a b"), {})

    def test_r_other_session_pending_intent_untouched_by_stop(self):
        self.w("a")
        self.hook("pre", {"tool_name": "Bash", "tool_input": {"command": "rm a"}, "tool_use_id": "t-s2",
                          "session_id": "other-session"})
        self.stop()
        self.assertEqual(self.status("cc-t-s2"), "open")

    def test_r_parallel_calls_do_not_supersede_each_other(self):
        self.w("a.md", "v1")
        ti = {"file_path": str(self.root / "a.md"), "old_string": "v1", "new_string": "v2"}
        self.hook("pre", {"tool_name": "Edit", "tool_input": ti, "tool_use_id": "t1"})
        self.hook("pre", {"tool_name": "Edit", "tool_input": ti, "tool_use_id": "t2"})
        self.assertEqual(self.status("cc-t1"), "open")
        self.assertIsNone(self.status("cc-t2"))

    def test_r_denied_call_does_not_block_agent_intent(self):
        self.w("a")
        self.hook("pre", {"tool_name": "Bash", "tool_input": {"command": "mv a b"}, "tool_use_id": "t-denied"})
        self.hook("pre", {"tool_name": "Bash", "tool_use_id": "t-next", "tool_input": {
            "command": "python3 /x/scripts/ledger.py intent open task-b --expect 'exists:b =1'"}})
        self.assertEqual(self.status("cc-t-denied"), "cancelled")

    def test_r_stale_intent_from_earlier_turn_cleared(self):
        self.w("a")
        self.hook("pre", {"tool_name": "Bash", "tool_input": {"command": "mv a b"}, "tool_use_id": "t-old",
                          "prompt_id": "p0"})
        self.hook("pre", {"tool_name": "Bash", "tool_input": {"command": "ls"}, "tool_use_id": "t-new",
                          "prompt_id": "p1"})
        self.assertEqual(self.status("cc-t-old"), "cancelled")

    def deny(self, cmd):
        out = self.hook("pre", {"tool_name": "Bash", "tool_input": {"command": cmd}, "tool_use_id": "g"})
        return out.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"

    def test_r_guard_allows_read_only(self):
        self.w(".ledger/journal.jsonl", "")
        self.w(".ledger/config.json", "{}")
        for cmd in ["sed -n 1,5p .ledger/journal.jsonl", "git log --oneline -- .ledger", "git diff .ledger/config.json",
                    "cp .ledger/journal.jsonl /tmp/backup.jsonl", "python3 -m json.tool .ledger/config.json",
                    "grep -n close scripts/ledger.py", "cp notes.md .ledger-notes.md", "cd .ledger && git status",
                    "python3 scripts/ledger.py intent open t --expect 'exists:a =1' --memo observe"]:
            self.assertFalse(self.deny(cmd), cmd)

    def test_r_guard_blocks_writes_and_human_only(self):
        self.w(".ledger/journal.jsonl", "")
        for cmd in ["rm -rf .led*", "rm -rf .LEDGER", "find . -path './.ledger/*' -delete",
                    "echo '{}' >> \"$PWD/.ledger/journal.jsonl\"", "echo .ledger | xargs rm -rf", "git clean -fdX",
                    "cat <<'EOF' >/dev/null\nhi\nEOF\nrm -rf .ledger",
                    "python3 - <<'EOF'\nopen('.ledger/journal.jsonl','a').write('x')\nEOF",
                    "python3 -c \"import ledger;ledger.Ledger().close('c1')\"",
                    "true a#b; python3 /x/ledger.py close c1", "python3 /x/ledger.py $(echo close) c1"]:
            self.assertTrue(self.deny(cmd), cmd)

    def test_r_project_script_nudged_once_even_with_task_intents(self):
        L.Ledger(self.root / ".ledger" / "journal.jsonl").intent_open("task-x", ["exists:x =1"])
        self.w("tools/close_task.sh", "#!/bin/sh\n")
        pre = lambda cmd, tid: self.hook("pre", {"tool_name": "Bash", "tool_input": {"command": cmd}, "tool_use_id": tid})
        self.assertIn("tools/close_task.sh", pre("tools/close_task.sh rel-1", "n1")["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(pre("sh tools/close_task.sh rel-2", "n2"), {})
        self.assertEqual(pre("npm --version", "n3"), {})
        self.w("tools/build.sh", "#!/bin/sh\n")
        out = pre("for i in 1 2; do tools/build.sh $i; done", "n4")  # 循环里调用的脚本也提醒
        self.assertIn("tools/build.sh", out["hookSpecificOutput"]["additionalContext"])


if __name__ == "__main__":
    unittest.main()
