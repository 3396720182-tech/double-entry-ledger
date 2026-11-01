#!/usr/bin/env python3
"""
cc_hook.py — Claude Code 钩子适配器（仅依赖标准库）

由 SKILL.md 前言里的 hooks 注册：本会话里调用一次 skill，之后的工具调用都会经过它。
  pre   PreToolUse          由代码从工具参数推出期望，开立意图 cc-<tool_use_id>（借：计划）；
                            拦截对 .ledger/ 的改写，以及应由人执行的账本命令
  post  PostToolUse         代码重新取数、结算（贷：实际）；不符时把结论贴在工具结果旁边
  fail  PostToolUseFailure  工具报错：代码证明什么都没发生就撤销；部分生效则保持未结并告知
  stop  Stop                重新结算全部未结意图 → 校验 → 试算；有未兑现的意图时让 Claude 再看一次，
                            并把账面结论直接显示给用户；账目干净时自动结账

能推出期望的动作：Write / Edit / MultiEdit / NotebookEdit，以及 Bash 里的简单文件命令
（mv cp rm rmdir mkdir touch tee、git mv/rm、sed -i、重定向 > >>）。解析是保守的，宁可不推断也不误报：
变量、命令替换、花括号展开、循环与条件、`A || 备选`、`[ 条件 ] && 操作` 都不推断；一条推不出效果的命令
（脚本、构建、测试）之前的操作不再核对，因为它可能又动了那些文件。多个文件的 sed -i 只作账外核对：
全都没变才算没生效，部分没变只提示。推不出效果的交付靠 agent 开立的任务意图在 Stop 时结算；
项目自己的脚本第一次被调用时，提醒 agent 先登记任务意图。

钩子自身出错一律放行（fail-open），错误写到 .ledger/cc-hook.log，不打断用户的会话。
钩子每次给 Claude 或用户的提示也记在 .ledger/cc-hook.log，事后可以查「账本当时说了什么」。
环境变量：LEDGER_CC_HOOKS=off 关闭；LEDGER_CC_AUTOCLOSE=0 不自动结账。
"""
from __future__ import annotations

import contextlib
import datetime
import glob
import hashlib
import json
import os
import re
import shlex
import sys
import tempfile
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ledger as L  # noqa: E402

FILE_TOOLS = {"Write": "file_path", "Edit": "file_path", "MultiEdit": "file_path", "NotebookEdit": "notebook_path"}
SHELL_TOOLS = ("Bash",)
# 这些账本操作是人的签收或人工干预，agent 不能自己做；Stop 钩子在账目干净时会自动结账
HUMAN_ONLY = {"close": "结账", "observe": "登记 claim 观测", "clear-suspense": "清理挂账", "repair": "修复日记账",
              "--direct": "直接改受保护科目", "--config": "改用别的配置", "--journal": "改用别的日记账"}
SWALLOW = {"true", ":", "echo", "printf"}
CONTROL_WORDS = {"if", "then", "else", "elif", "fi", "for", "while", "until", "do", "done", "case", "esac",
                 "function", "{", "}", "(", ")", "select"}
SEPARATORS = {";", "&&", "||", "|", "&", "\n", ";;", "|&"}
REDIRECTS = {">", ">>", ">|", "&>", "&>>"}
MAX_REASON = 3000


# ---------------------------------------------------------------- 路径

def project_root(data):
    return os.path.realpath(os.environ.get("CLAUDE_PROJECT_DIR") or data.get("cwd") or os.getcwd())


def ledger_dir(root):
    return os.path.join(root, ".ledger")


def rel(root, cwd, p, allow_root=False):
    """工具参数里的路径 → 相对项目根的规范路径；越出项目根、含通配符或指向 .ledger/ 的返回 None。
    allow_root=True 时项目根本身返回 ""（如 `mv sub/a.md .` 的目标目录）。"""
    if not p or "$" in p or "`" in p:
        return None
    p = os.path.expanduser(p)
    full = os.path.realpath(os.path.join(cwd, p))
    if full == root:
        return "" if allow_root else None
    if not full.startswith(root + os.sep):
        return None
    r = os.path.relpath(full, root)
    if glob.has_magic(r) or r == ".ledger" or r.startswith(".ledger" + os.sep):
        return None
    return r


def in_ledger_dir(root, cwd, p):
    if not p:
        return False
    # 按不区分大小写比较：macOS 默认文件系统上 .LEDGER/ 就是 .ledger/
    full = os.path.realpath(os.path.join(cwd, os.path.expanduser(p))).casefold()
    d = ledger_dir(root).casefold()
    return full == d or full.startswith(d + os.sep)


# ---------------------------------------------------------------- 由工具参数推出期望

def file_tool_ops(root, tool, ti):
    """返回 [(op_label, {path: 'exists'|'absent'|'changed'})]。"""
    path = ti.get(FILE_TOOLS[tool])
    r = rel(root, root, path) if path else None
    if r is None:
        return []
    full = os.path.join(root, r)
    if not os.path.exists(full):
        return [(f"{tool} {r}", {r: "exists"})]
    if tool == "Write":
        content = ti.get("content")
        if isinstance(content, str):
            try:
                with open(full, "rb") as f:
                    if f.read() == content.encode("utf-8"):
                        return []  # 内容相同：这次写入本来就不会改变什么
            except OSError:
                pass
    return [(f"{tool} {r}", {r: "changed"})]


HEREDOC_RE = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")


def _strip_heredocs(cmd):
    """去掉 heredoc 正文（它是数据，不是命令），返回 (命令, [正文...])；正文之后的行照常解析。"""
    lines, out, bodies, i = cmd.split("\n"), [], [], 0
    while i < len(lines):
        line = lines[i]
        out.append(line)
        i += 1
        for m in HEREDOC_RE.finditer(line):
            body = []
            while i < len(lines) and lines[i].strip() != m.group(2):
                body.append(lines[i])
                i += 1
            i += 1
            bodies.append("\n".join(body))
    return "\n".join(out), bodies


def _strip_comments(line):
    """去掉 shell 注释：只有词首、引号外的 # 才开始注释（a#b 里的 # 不算）。"""
    quote, prev = None, " "
    for i, c in enumerate(line):
        if quote:
            if c == quote:
                quote = None
        elif c in "'\"":
            quote = c
        elif c == "#" and (prev.isspace() or prev in ";&|()"):
            return line[:i]
        prev = c
    return line


def _tokens(cmd):
    # fd 重定向：2>&1、>&2 不产生文件；2>err.log 当作普通重定向
    cmd, _ = _strip_heredocs(cmd.replace("\\\n", " "))
    cmd = " ; ".join(_strip_comments(x) for x in cmd.split("\n"))
    cmd = re.sub(r"\d*>&\d*-?", " ", cmd)
    cmd = re.sub(r"(^|\s)\d+(>>?)", r"\1\2", cmd)
    lex = shlex.shlex(cmd, posix=True, punctuation_chars=";&|<>()")
    lex.whitespace_split = True
    lex.commenters = ""
    return list(lex)


def _drop_functions(tokens):
    """去掉 `NAME() { ...; }` 函数定义（SKILL 让 agent 每次都定义 L()），函数体本身不执行。"""
    out, i = [], 0
    while i < len(tokens):
        if i + 2 < len(tokens) and tokens[i + 1] == "()" and tokens[i + 2] == "{":
            depth, j = 0, i + 2
            while j < len(tokens):
                depth += {"{": 1, "}": -1}.get(tokens[j], 0)
                if depth == 0:
                    break
                j += 1
            i = j + 1
            continue
        out.append(tokens[i])
        i += 1
    return out


def _segments(tokens):
    """按 ; && || | & 换行切开，返回 [(words, sep_after)]。"""
    segs, cur = [], []
    for t in _drop_functions(tokens):
        if t in SEPARATORS:
            segs.append((cur, t))
            cur = []
        else:
            cur.append(t)
    segs.append((cur, None))
    return [(w, s) for w, s in segs if w or s]


def _strip_prefix(words):
    i = 0
    while i < len(words):
        w = words[i]
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", w):
            i += 1
        elif w in ("sudo", "command", "builtin", "time", "nohup", "env"):
            i += 1
            while i < len(words) and words[i].startswith("-"):
                i += 1
        else:
            break
    words = words[i:]
    if words and words[0].startswith("\\"):
        words = [words[0][1:]] + words[1:]
    return words


def _split_redirects(words):
    args, targets = [], []
    i = 0
    while i < len(words):
        w = words[i]
        if w in REDIRECTS and i + 1 < len(words):
            targets.append(words[i + 1])
            i += 2
        elif w in ("<", "<<", "<<-", "<<<") and i + 1 < len(words):
            i += 2
        else:
            args.append(w)
            i += 1
    return args, targets


def _expand(cwd, a):
    """模仿 shell 的路径展开：有通配符就展开（无匹配则原样，与 bash 默认一致）。"""
    if glob.has_magic(a):
        hits = sorted(glob.glob(os.path.join(cwd, os.path.expanduser(a))))
        return hits or [a]
    return [a]


def _opts_and_args(args, takes_value=()):
    opts, rest, i, end = [], [], 0, False
    while i < len(args):
        a = args[i]
        if not end and a == "--":
            end = True
        elif not end and a.startswith("-") and a != "-":
            opts.append(a)
            if a in takes_value and i + 1 < len(args):
                opts.append(args[i + 1])
                i += 1
        else:
            rest.append(a)
        i += 1
    return opts, rest


class Sim:
    """按顺序模拟一条命令里各段的文件效果；同一路径被多段改动时以最后状态为准。"""

    def __init__(self, root, cwd):
        self.root, self.cwd = root, cwd
        self.state = {}       # rel path -> 'exists' | 'absent' | 'changed'
        self.ops = []         # [(label, {path: effect})]
        self.cut = 0          # 这之前的操作后面跟着看不懂的命令，可能被它改掉，不再核对
        self.unknown = []     # 看不懂、又可能有副作用的命令名
        self.scripts = []     # 其中属于项目自己的脚本（相对项目根的路径）

    def exists(self, r):
        s = self.state.get(r)
        if s in ("exists", "changed", "maybe-changed"):
            return True
        if s == "absent":
            return False
        return os.path.exists(os.path.join(self.root, r))

    def isdir(self, r):
        s = self.state.get(r)
        if s == "absent":
            return False
        return os.path.isdir(os.path.join(self.root, r)) or any(
            k.startswith(r + os.sep) and v != "absent" for k, v in self.state.items())

    @staticmethod
    def _apply(state, effects):
        for r, v in effects.items():
            if v == "absent":
                for k in list(state):
                    if k.startswith(r + os.sep):
                        state[k] = "absent"
            if v in ("changed", "maybe-changed") and state.get(r) == "exists":
                continue  # 本条命令里刚建出来的文件，「存在」已经够了
            state[r] = v

    def set(self, label, effects):
        effects = {k: v for k, v in effects.items() if k is not None}
        if not effects:
            return
        self._apply(self.state, effects)
        self.ops.append((label, effects))

    def barrier(self, name):
        """后面跟着一条推不出效果的命令（脚本、构建……）：它可能动到前面操作过的文件，前面的操作不再核对。"""
        self.unknown.append(name)
        self.cut = len(self.ops)

    def kept(self):
        state = {}
        for _, eff in self.ops[self.cut:]:
            self._apply(state, eff)
        return [(label, set(eff)) for label, eff in self.ops[self.cut:]], state

    def r(self, p, allow_root=False):
        return rel(self.root, self.cwd, p, allow_root)


def _cmd_mv(sim, label, args, git=False):
    opts, rest = _opts_and_args(args, takes_value=("-t", "--target-directory"))
    if any(o in ("-n", "--no-clobber", "-i", "--interactive", "-k") for o in opts):
        return False
    target = None
    if "-t" in opts:
        target = opts[opts.index("-t") + 1]
    if target is None:
        if len(rest) < 2:
            return False
        target, srcs = rest[-1], rest[:-1]
    else:
        srcs = rest
    srcs = [x for s in srcs for x in _expand(sim.cwd, s)]
    tr = sim.r(target, allow_root=True)
    if tr is None:
        return False
    many = len(srcs) > 1
    eff = {}
    for s in srcs:
        sr = sim.r(s)
        if sr is None:
            return False
        dst = os.path.join(tr, os.path.basename(sr)) if (many or tr == "" or sim.isdir(tr)) else tr
        if sr.casefold() != dst.casefold():  # 只改大小写：在不区分大小写的文件系统上旧名照样「存在」
            eff[sr] = "absent"
        eff[dst] = "exists"
    sim.set(label, eff)
    return True


def _cmd_cp(sim, label, args):
    opts, rest = _opts_and_args(args, takes_value=("-t", "--target-directory"))
    target = opts[opts.index("-t") + 1] if "-t" in opts else None
    if target is None:
        if len(rest) < 2:
            return False
        target, srcs = rest[-1], rest[:-1]
    else:
        srcs = rest
    if any(s.endswith(("/", "/.")) for s in srcs):
        return False  # cp -R dir/ 或 dir/. 在 GNU 与 BSD 上语义不同（复制目录本身还是目录内容）
    srcs = [x for s in srcs for x in _expand(sim.cwd, s)]
    tr = sim.r(target, allow_root=True)
    if tr is None:
        return False
    many = len(srcs) > 1
    eff = {}
    for s in srcs:
        base = os.path.basename(os.path.normpath(s))
        eff[os.path.join(tr, base) if (many or tr == "" or sim.isdir(tr)) else tr] = "exists"
    sim.set(label, eff)
    return True


def _cmd_paths(sim, label, args, state, skip_if=(), takes_value=()):
    opts, rest = _opts_and_args(args, takes_value)
    if any(o in skip_if for o in opts):
        return False
    paths = [x for a in rest for x in _expand(sim.cwd, a)]
    if not paths:
        return False
    sim.set(label, {sim.r(p): state for p in paths})
    return True


def _cmd_sed(sim, label, args):
    in_place, scripts, files, i = False, False, [], 0
    while i < len(args):
        a = args[i]
        if a in ("-e", "--expression", "-f", "--file"):
            scripts = True
            i += 2
            continue
        if a.startswith("--in-place"):
            in_place = True
        elif a.startswith("-") and not a.startswith("--") and len(a) > 1:
            flags = a[1:]
            if "i" in flags:
                in_place = True
                # BSD sed：-i 后面单独一个参数是备份后缀（常见写法 -i ''）
                if flags == "i" and sys.platform == "darwin" and i + 1 < len(args) and (
                        args[i + 1] == "" or (args[i + 1].startswith(".")
                                              and not os.path.exists(os.path.join(sim.cwd, args[i + 1])))):
                    i += 1
            if flags.endswith(("e", "f")) and "i" not in flags:
                scripts = True
                i += 2
                continue
        else:
            files.append(a)
        i += 1
    if not in_place:
        return False
    if not scripts:
        files = files[1:]
    paths = [x for a in files for x in _expand(sim.cwd, a)]
    eff = {}
    # 一个文件：没改动就是没生效，记账。多个文件：替换规则本来就可能只命中其中一部分，
    # 改为账外核对——全都没变才算「没生效」，部分没变只作提示
    effect = "changed" if len(paths) == 1 else "maybe-changed"
    for p in paths:
        r = sim.r(p)
        if r is None:
            return False
        eff[r] = effect
    sim.set(label, eff)
    return bool(eff)


def bash_ops(root, cwd, cmd):
    return bash_parse(root, cwd, cmd)[0]


def bash_parse(root, cwd, cmd):
    """解析 Bash 命令里能确定效果的文件操作，返回 (分组后的操作, 推不出效果的命令名)。
    看不懂的段落跳过；看不懂又可能写文件的命令之前的操作不再核对；遇到控制结构整条放弃。"""
    try:
        tokens = _tokens(cmd)
    except ValueError:
        return [], []
    sim = Sim(root, cwd)
    segs = _segments(tokens)
    conditional = False  # `[ -f x ] && mv x y`：前面是条件判断，后面的操作本来就可能不执行
    for idx, (words, sep) in enumerate(segs):
        prev_sep = segs[idx - 1][1] if idx else None
        if prev_sep in (None, ";", "|", "&"):
            conditional = False
        if prev_sep == "&&" and _is_condition(_strip_prefix(segs[idx - 1][0])):
            conditional = True
        if prev_sep == "||":
            continue  # `A || B`：B 是兜底分支，是否执行不确定
        if sep == "&":
            continue  # 后台执行：结束时间不确定
        if sep == "||":
            nxt = _strip_prefix(segs[idx + 1][0]) if idx + 1 < len(segs) else []
            if not nxt or nxt[0] not in SWALLOW:
                continue  # `A || 真正的备选命令`：A 可能失败后由别的命令补，效果不确定
            # `A || true`：吞掉失败的典型写法，A 仍然是本意要做的事
        words = _strip_prefix(words)
        if not words:
            continue
        if words[0] in CONTROL_WORDS or words[0].endswith("()") or words[0] == "!":
            return [], [words[0]]  # 循环、条件、函数：执行路径不确定，整条命令都不推断（也算推不出效果）
        if any("$" in w or "`" in w or re.search(r"\{[^}]*(,|\.\.)[^}]*\}", w) for w in words):
            # 变量、命令替换、花括号展开：推不出实际路径
            if words[0] in ("cd", "pushd", "popd"):
                break
            if not _read_only(words):
                sim.barrier(os.path.basename(words[0]))
            continue
        if conditional:
            if not _read_only(words) and not _is_condition(words):
                sim.barrier(os.path.basename(words[0]))
            continue
        args, targets = _split_redirects(words)
        label = " ".join(words)[:200]
        for t in targets:
            r = sim.r(t)
            if r is not None:
                sim.set(f"> {r}", {r: "exists"})
        if not args:
            continue
        name, rest = os.path.basename(args[0]), args[1:]
        if name == "popd":
            break
        if name in ("cd", "pushd"):
            if not rest:
                break
            nd = os.path.realpath(os.path.join(sim.cwd, os.path.expanduser(rest[0])))
            if not os.path.isdir(nd):
                break
            sim.cwd = nd
            continue
        handled = False
        if name == "git" and rest[:1] == ["mv"]:
            handled = _cmd_mv(sim, label, rest[1:], git=True)
        elif name == "git" and rest[:1] == ["rm"]:
            handled = _cmd_paths(sim, label, rest[1:], "absent", skip_if=("--cached", "-n", "--dry-run"))
        elif name == "mv":
            handled = _cmd_mv(sim, label, rest)
        elif name == "cp":
            handled = _cmd_cp(sim, label, rest)
        elif name in ("rm", "rmdir", "unlink"):
            handled = _cmd_paths(sim, label, rest, "absent", skip_if=("-i", "-I", "--interactive"))
        elif name == "mkdir":
            handled = _cmd_paths(sim, label, rest, "exists", takes_value=("-m", "--mode"))
        elif name == "touch":
            handled = _cmd_paths(sim, label, rest, "exists", skip_if=("-c", "--no-create", "-r", "-d", "-t", "-h"))
        elif name == "tee":
            handled = _cmd_paths(sim, label, rest, "exists")
        elif name in ("sed", "gsed"):
            handled = _cmd_sed(sim, label, rest)
        if not handled and not _read_only(args) and not _non_interfering(args):
            sim.barrier(name)
    return _group(sim), sim.unknown


def bash_scripts(root, cwd, cmd):
    """命令里调用的项目自己的脚本（tools/x.sh、python3 scripts/y.py 之类）。"""
    try:
        tokens = _tokens(cmd)
    except ValueError:
        return []
    sim = Sim(root, cwd)
    for words, _ in _segments(tokens):
        while words and words[0] in ("do", "then", "else", "{", "(", "!", "time"):
            words = words[1:]  # 循环体、条件分支里的命令照样要看：for … do tools/x.sh "$i"; done
        words = _strip_prefix(words)
        if words and words[0] in ("cd", "pushd") and len(words) > 1 and "$" not in words[1]:
            nd = os.path.realpath(os.path.join(sim.cwd, os.path.expanduser(words[1])))
            if os.path.isdir(nd):
                sim.cwd = nd
            continue
        args, _ = _split_redirects(words) if words else ([], [])
        if args and not _read_only(args):
            script = _project_script(sim, args)
            if script:
                sim.scripts.append(script)
    return list(dict.fromkeys(sim.scripts))


def _project_script(sim, args):
    if not args:
        return None
    first = args[0]
    if os.path.basename(first).startswith(INTERPRETERS) and len(args) > 1:
        first = next((a for a in args[1:] if not a.startswith("-")), "")
    if "/" not in first and not os.path.exists(os.path.join(sim.cwd, first)):
        return None
    r = rel(sim.root, sim.cwd, first)
    return r if r and os.path.isfile(os.path.join(sim.root, r)) else None


READ_ONLY = {"ls", "cat", "head", "tail", "grep", "egrep", "fgrep", "rg", "wc", "echo", "printf", "pwd", "stat", "file",
             "test", "[", "[[", "true", "false", ":", "diff", "cmp", "sort", "uniq", "cut", "tr", "jq", "which", "type",
             "date", "basename", "dirname", "realpath", "readlink", "du", "df", "tree", "shasum", "sha256sum", "md5",
             "md5sum", "sleep", "printenv", "column", "nl", "less", "more", "comm", "awk", "sed", "find", "git", "L",
             "exit", "export", "set", "unset"}
GIT_READ_ONLY = {"status", "diff", "log", "show", "rev-parse", "branch", "ls-files", "blame", "grep"}


CONDITIONS = {"test", "[", "[[", "grep", "egrep", "fgrep", "rg", "cmp", "diff", "which", "type", "command"}


def _is_condition(words):
    return bool(words) and os.path.basename(words[0]) in CONDITIONS


def _non_interfering(args):
    """会写东西、但不改项目文件的存在与内容（探针只看这两样）：改权限、git 暂存与提交。"""
    name = os.path.basename(args[0])
    if name in ("chmod", "chown", "chgrp"):
        return True
    return name == "git" and len(args) > 1 and args[1] in ("add", "commit", "stage", "tag", "push", "fetch", "notes")


def _read_only(args):
    """不会改动项目文件的命令（只读查看、账本命令本身）。sed 不带 -i、find 不带 -delete/-exec 才算。"""
    name = os.path.basename(args[0])
    if "ledger.py" in " ".join(args[:3]):
        return True
    if name not in READ_ONLY:
        return False
    if name == "sed":
        return not any(a.startswith("-i") or a.startswith("--in-place") for a in args[1:])
    if name == "find":
        return not any(a in ("-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint") for a in args[1:])
    if name == "awk":
        return "inplace" not in " ".join(args)
    if name == "git":
        return len(args) > 1 and args[1] in GIT_READ_ONLY
    return True


def _group(sim):
    """同一路径被多段命令改动时并成一笔；互不相干的操作各自一笔，这样一处失败不会连累其他。"""
    def related(a, b):
        return any(x == y or x.startswith(y + os.sep) or y.startswith(x + os.sep) for x in a for y in b)

    ops, state = sim.kept()
    groups = []
    for label, paths in ops:
        hit = [g for g in groups if related(g[1], paths)]
        merged_labels = [label] + [x for g in hit for x in g[0]]
        merged_paths = set(paths).union(*[g[1] for g in hit]) if hit else set(paths)
        groups = [g for g in groups if g not in hit] + [(merged_labels, merged_paths)]
    out = []
    for labels, paths in groups:
        eff = {p: state[p] for p in sorted(paths) if p in state}
        out.append(("; ".join(reversed(labels)), eff))
    return out


def expect_lines(effects):
    m = {"exists": "exists:{} =1", "absent": "exists:{} =0", "changed": "sha256:{} changed"}
    return [m[v].format(p) for p, v in effects.items() if v in m]


def soft_lines(effects):
    return [f"sha256:{p} changed" for p, v in effects.items() if v == "maybe-changed"]


# ---------------------------------------------------------------- 账本

def open_ledger(root, create):
    d = ledger_dir(root)
    j = os.path.join(d, "journal.jsonl")
    if not os.path.exists(j):
        if not create:
            return None
        os.makedirs(d, exist_ok=True)
        open(j, "a").close()
        cfg = os.path.join(d, "config.json")
        if not os.path.exists(cfg):
            with open(cfg, "w", encoding="utf-8") as f:
                f.write(json.dumps(L.DEFAULT_CONFIG, ensure_ascii=False, indent=2) + "\n")
    # 只认项目根下的账本：模型改不了钩子进程的环境，但别让 LEDGER_CONFIG 之类的外部设置把它带偏
    return L.Ledger(j, os.path.join(d, "config.json") if os.path.exists(os.path.join(d, "config.json")) else None)


def iid_for(tool_use_id, k, n):
    base = "cc-" + re.sub(r"[^A-Za-z0-9_.:-]", "_", tool_use_id or "unknown")
    return base if n == 1 else f"{base}-{k + 1}"


def state_path(root):
    return os.path.join(ledger_dir(root), "cc-state.json")


def load_state(root):
    try:
        with open(state_path(root), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(root, st):
    p = state_path(root)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False)
    os.replace(tmp, p)


@contextlib.contextmanager
def state_lock(root):
    """cc-state.json 的读改写要串行：Claude Code 可能并行跑多个工具调用的钩子。"""
    fd = os.open(state_path(root) + ".lock", os.O_RDWR | os.O_CREAT, 0o644)
    try:
        if L.fcntl:
            L.fcntl.flock(fd, L.fcntl.LOCK_EX)
        yield
    finally:
        if L.fcntl:
            L.fcntl.flock(fd, L.fcntl.LOCK_UN)
        os.close(fd)


def log(root, msg):
    try:
        d = ledger_dir(root)
        if os.path.isdir(d):
            with open(os.path.join(d, "cc-hook.log"), "a", encoding="utf-8") as f:
                f.write(f"{datetime.datetime.now().astimezone().isoformat(timespec='seconds')} {msg}\n")
    except OSError:
        pass


def fmt_result(r):
    mark = "✓" if r["match"] else ("?" if r.get("unknown") else "✗")
    actual = r["actual"] if r["actual"] is not None else ("未知" if r.get("unknown") else "未产生")
    return f"{mark} {r['probe']} {r['op']}：预期 {r['expected']}，实际 {actual}"


def _try_settle(led, iid):
    try:
        return led.intent_settle(iid, actor="cc-hook")
    except L.CheckFailed:
        return None


def _try_cancel(led, iid, reason):
    try:
        led.intent_cancel(iid, reason, actor="cc-hook")
        return True
    except L.CheckFailed:
        return False


# ---------------------------------------------------------------- 事件

def _ledger_subcommand(args):
    """`python3 …/ledger.py [全局选项] 子命令 …` 或 agent 定义的 `L …`：返回 (子命令词表, 全局选项)；不是账本命令返回 None。"""
    idx = next((i for i, a in enumerate(args) if a.endswith("ledger.py")), None)
    if idx is None:
        if not args or args[0] != "L":
            return None
        idx = 0
    rest, opts, i = args[idx + 1:], [], 0
    while i < len(rest) and rest[i].startswith("-"):
        opts.append(rest[i])
        i += 2 if rest[i] in ("--journal", "--config", "--actor") else 1
    return rest[i:], opts


def _human_only(sub):
    if sub is None:
        return []
    words, bad = sub[0], [o for o in sub[1] if o in ("--journal", "--config")]
    head = words[:2]
    if head[:1] in (["close"], ["clear-suspense"], ["repair"]):
        bad.append(head[0])
    if head == ["intent", "observe"]:
        bad.append("observe")
    if "--direct" in words or any(w.startswith("--direct=") for w in words):
        bad.append("--direct")
    return bad


WRITE_TARGET_LAST = {"cp", "mv", "install", "rsync", "ln"}
WRITE_TARGET_ALL = {"rm", "rmdir", "unlink", "truncate", "touch", "chmod", "chown", "mkdir", "tee", "shred"}
INTERPRETERS = ("python", "perl", "ruby", "node", "sh", "bash", "zsh", "osascript")


def _mentions_ledger(text):
    return ".ledger" in text.replace("ledger.py", "").casefold()


def _ledger_write_reason(root, cwd, cmd):
    """命令是否可能改写 .ledger/，或执行应由人做的账本命令。只看写入目标，只读查看一律放行。"""
    try:
        body_cmd, bodies = _strip_heredocs(cmd)
        tokens = _tokens(cmd)
    except ValueError:
        return "ledger" if _mentions_ledger(cmd) else None
    cwd = os.path.realpath(cwd)

    def hits(p):
        return any(in_ledger_dir(root, cwd, x) for x in _expand(cwd, p))

    for words, _ in _segments(tokens):
        words = _strip_prefix(words)
        if not words:
            continue
        args, targets = _split_redirects(words)
        if any(hits(t) or ("$" in t and _mentions_ledger(t)) for t in targets):
            return "ledger"
        if not args:
            continue
        name = os.path.basename(args[0])
        sub = _ledger_subcommand(args)
        if sub is not None and any("$" in w or "`" in w for w in sub[0][:2]):
            return "human:动态子命令"  # 子命令用命令替换拼出来，看不出是不是人工专属操作
        bad = _human_only(sub)
        if bad:
            return "human:" + ",".join(dict.fromkeys(bad))
        if name in ("cd", "pushd") and len(args) > 1:
            cwd = os.path.realpath(os.path.join(cwd, os.path.expanduser(args[1])))
            continue
        inside = in_ledger_dir(root, cwd, ".")
        opts, rest = _opts_and_args(args[1:], takes_value=("-t", "--target-directory"))
        if name in WRITE_TARGET_LAST:
            dest = opts[opts.index("-t") + 1] if "-t" in opts else (rest[-1] if len(rest) >= 2 else None)
            if (dest and hits(dest)) or (inside and dest and not os.path.isabs(dest)):
                return "ledger"
        elif name in WRITE_TARGET_ALL:
            if any(hits(a) or ("$" in a and _mentions_ledger(a)) for a in rest) or (inside and rest):
                return "ledger"
        elif name in ("sed", "gsed", "perl") and any(a.startswith(("-i", "--in-place", "-pi")) for a in args[1:]):
            if any(hits(a) for a in rest) or inside:
                return "ledger"
        elif name == "dd":
            if any(a.startswith("of=") and hits(a[3:]) for a in args[1:]):
                return "ledger"
        elif name == "find":
            if any(a in ("-delete", "-exec", "-execdir", "-ok") for a in args) and (_mentions_ledger(" ".join(args)) or inside):
                return "ledger"
        elif name == "git":
            if (len(args) > 1 and args[1] in ("rm", "mv", "checkout", "restore", "clean")
                    and (_mentions_ledger(" ".join(args[2:])) or inside)):
                return "ledger"
            if len(args) > 1 and args[1] == "clean" and any(re.match(r"^-[A-Za-z]*[xX]", a) for a in args[2:]) \
                    and os.path.isdir(ledger_dir(root)):
                return "ledger"  # git clean -x/-X 会连同被忽略的 .ledger/ 一起删掉
        elif name == "xargs":
            if any(os.path.basename(a) in WRITE_TARGET_ALL | WRITE_TARGET_LAST for a in args[1:]) and _mentions_ledger(cmd):
                return "ledger"
        elif name.startswith(INTERPRETERS):
            # 内联脚本（-c / -e / heredoc）里提到 .ledger 的，按可能写入处理；运行脚本文件本身不拦
            inline = " ".join(args[1:]) if any(a in ("-c", "-e", "-") for a in args[1:]) or name == "xargs" else ""
            if _mentions_ledger(inline) or (bodies and any(_mentions_ledger(b) for b in bodies)):
                return "ledger"
            if "import ledger" in inline or any("import ledger" in b for b in bodies):
                if any(k in inline + " ".join(bodies) for k in (".close(", "intent_observe(", "clear_suspense(", "direct=")):
                    return "human:python"
    return None


def guard(root, data):
    """拦截对 .ledger/ 的改写和应由人执行的账本命令。返回拒绝理由或 None。"""
    tool, ti, cwd = data.get("tool_name"), data.get("tool_input") or {}, data.get("cwd") or root
    if tool in FILE_TOOLS:
        if in_ledger_dir(root, root, ti.get(FILE_TOOLS[tool]) or ""):
            return ".ledger/ 是账本目录，只能由账本命令追加；手改会破坏哈希链。需要更正请用 reverse，或交给人处理。"
        return None
    cmd = ti.get("command") or ""
    if "ledger" not in cmd.casefold() and not re.search(r"(^|[;&|\s])L\s", cmd) \
            and not os.path.isdir(ledger_dir(root)):
        return None  # 没有账本、也不是账本命令：不用细看
    why = _ledger_write_reason(root, cwd, cmd)
    if why and why.startswith("human:"):
        what = "、".join(f"{w}（{HUMAN_ONLY.get(w, '人工专属操作')}）" for w in why[6:].split(","))
        return (f"账本命令 {what} 要由人在自己的终端执行，agent 不能代做。"
                "账目干净时 Stop 钩子会自动结账；有待复核项时，请在汇报里写明，由人运行 close --reviewed-by。")
    if why:
        return ".ledger/ 是账本目录，只能由账本命令追加；手改会破坏哈希链。只读查看（cat、jq、git log、ledger.py verify / intent list）不受限制。"
    return None


def _meta_of(st, iid):
    return (st.intents[iid]["open"].get("meta") or {}) if iid in st.intents else {}


def _clear_stale(led, data):
    """同一会话里只开了单、从没收到 PostToolUse 的工具意图（调用被拒绝或中断）：
    属于更早的用户回合的，或者 agent 正要自己开意图时，代码证明没产生效果就撤销，免得挡路。"""
    sess, prompt = data.get("session_id"), data.get("prompt_id")
    cmd = (data.get("tool_input") or {}).get("command") or ""
    agent_opening = "intent" in cmd and "open" in cmd and ("ledger.py" in cmd or re.search(r"(^|[;&|\s])L\s", cmd))
    _, st = led._snapshot()
    for iid, it in list(st.intents.items()):
        if not iid.startswith("cc-") or it["status"] != "open":
            continue
        meta = _meta_of(st, iid)
        if meta.get("session_id") != sess or meta.get("tool_use_id") == data.get("tool_use_id"):
            continue
        if agent_opening or (prompt and meta.get("prompt_id") and meta.get("prompt_id") != prompt):
            _try_cancel(led, iid, "工具调用没有执行（被拒绝或中断），代码核实没有产生任何效果")


def on_pre(root, data, out):
    reason = guard(root, data)
    if reason:
        out.update({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                           "permissionDecisionReason": reason}})
        return
    tool, ti = data.get("tool_name"), data.get("tool_input") or {}
    if tool in FILE_TOOLS:
        ops = file_tool_ops(root, tool, ti)
    elif tool in SHELL_TOOLS:
        if ti.get("run_in_background"):
            return
        cwd = os.path.realpath(data.get("cwd") or root)
        ops, unknown = bash_parse(root, cwd, ti.get("command") or "")
        if unknown:
            text = _task_intent_nudge(root, data, unknown)
            if text:
                out.update({"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": text}})
    else:
        return
    ops = [(label, eff) for label, eff in ops if eff]
    led = open_ledger(root, create=bool(ops))
    if led is not None:
        _clear_stale(led, data)
    if not ops:
        return
    notes, shadow = [], []
    hard_ops = [(label, eff) for label, eff in ops if expect_lines(eff)]
    for label, eff in ops:
        soft = soft_lines(eff)
        if soft:
            shadow.append({"label": label, "soft": True, "checks": [[x, led.prober.measure(x.rsplit(" ", 1)[0])]
                                                                     for x in soft]})
    for k, (label, eff) in enumerate(hard_ops):
        iid = iid_for(data.get("tool_use_id"), k, len(hard_ops))
        kw = dict(expects=expect_lines(eff), tool=f"cc:{tool}", memo=label,
                  meta={"tool_use_id": data.get("tool_use_id"), "session_id": data.get("session_id"),
                        "prompt_id": data.get("prompt_id"), "agent_id": data.get("agent_id")}, actor="cc-hook")
        for attempt in (1, 2):
            try:
                led.intent_open(iid, **kw)
                break
            except L.CheckFailed as e:
                msg = str(e)
                if "预期变化都是 0" in msg:
                    break  # 目标状态已经满足（如 mkdir -p 已有目录），这次调用不会改变什么
                others = sorted({d.split(" ↔ ", 1)[1].split(":", 1)[0] for d in e.details if " ↔ " in d})
                _, st = led._snapshot()
                posted = [o for o in others if o in st.intents and st.intents[o]["status"] in ("mismatch", "unknown")]
                if attempt == 1 and others and all(o.startswith("cc-") for o in others) and posted == others:
                    # 同一路径上挂着之前已经执行过、但没兑现的工具意图（多半是重试）：先看它现在是否已兑现，
                    # 没兑现且代码证明它什么都没做，就撤销，让这次调用接着记账。还没执行完的调用不动
                    for o in others:
                        r = _try_settle(led, o)
                        if not r or r["status"] != "settled":
                            _try_cancel(led, o, f"被同一路径上的新调用 {iid} 取代；代码核实原调用没有产生任何效果")
                    continue
                if others and not any(o.startswith("cc-") for o in others):
                    # 路径已在 agent 开立的任务意图里（任务意图在 Stop 时结算）。账本不允许重叠的意图，
                    # 这里只在账外记下基线，PostToolUse 时照样核对这次调用，结论只作提示
                    shadow.append({"label": label, "tasks": others, "checks": [
                        [x, (led.prober.measure(x.rsplit(" ", 1)[0]) if x.endswith(" changed") else None)]
                        for x in kw["expects"]]})
                else:
                    notes.append(f"{label}：未记账（{msg[:200]}）")
                break
    if shadow:
        with state_lock(root):
            cs = load_state(root)
            cs.setdefault("shadow", {})[data.get("tool_use_id") or ""] = shadow
            save_state(root, cs)
    if notes:
        log(root, "pre: " + " | ".join(notes))


def _nudge_once(data, key):
    """每个会话、每个 key 只提醒一次。标记放在临时目录，不为此在项目里建 .ledger/。"""
    sess = re.sub(r"[^A-Za-z0-9_.-]", "_", data.get("session_id") or "nosession")
    mark = os.path.join(tempfile.gettempdir(), "cc-ledger-nudge-" + sess + "-"
                        + hashlib.sha256(key.encode("utf-8")).hexdigest()[:16])
    if os.path.exists(mark):
        return False
    with open(mark, "w") as f:
        f.write(key)
    return True


def _task_intent_nudge(root, data, unknown):
    """命令里有推不出效果的程序时提醒登记任务意图：账本里还没有任何任务意图时提醒一次；
    之后项目自己的脚本（tools/x.sh 这类，静默出错最常见的地方）第一次被调用时各提醒一次。"""
    led = open_ledger(root, create=False)
    has_tasks = led is not None and any(not i["intent"].startswith("cc-") for i in led.intents())
    cwd = os.path.realpath(data.get("cwd") or root)
    scripts = bash_scripts(root, cwd, (data.get("tool_input") or {}).get("command") or "")
    new_scripts = [x for x in scripts if _nudge_once(data, "script:" + x)]
    first = not has_tasks and _nudge_once(data, "no-task-intents")
    if not first and not new_scripts:
        return None
    names = "、".join(new_scripts[:3] or [x for x in dict.fromkeys(unknown) if x not in CONTROL_WORDS][:3]
                     or ["这条命令"])
    log(root, f"pre-nudge {data.get('tool_use_id')}: {names}")
    if first:
        return (f"[复式记账] 这条命令里的 {names} 推不出效果，钩子没法替你核对它到底做成了没有；"
                "能在 Stop 时核对它的只有任务意图，而账本里还没有任何任务意图。如果它在产出用户要的交付，"
                "执行前先登记，例如 L intent open task-<名> --expect 'exists:<产物路径> =1'。")
    return (f"[复式记账] {names} 是项目自己的脚本，钩子推不出它的效果，它打印的「成功」也不算数。"
            "如果它在产出用户要的交付（移动文件、生成产物、改状态），确认已有任务意图覆盖这些产物；"
            "还没有就先登记一个，Stop 时由代码核对。（每个脚本只提示一次）")


def _cc_ids(led, tool_use_id):
    base = iid_for(tool_use_id, 0, 1)
    return [i["intent"] for i in led.intents() if i["intent"] == base or i["intent"].startswith(base + "-")]


def _check_shadow(led, item):
    """账外核对。返回 (硬问题行, 提示行)。"""
    changed, miss = [], []
    for line, base in item["checks"]:
        probe, op = line.rsplit(" ", 1)
        now = led.prober.measure(probe)
        ok = (now != base) if op == "changed" else (now == L.to_dec(op[1:]))
        (changed if ok else miss).append(line)
    if not miss:
        return [], []
    if item.get("soft"):
        files = [m.split(":", 1)[1].rsplit(" ", 1)[0] for m in miss]
        if not changed:
            return [f"- {item['label']}：所有目标文件内容都没有变化（替换规则一处都没匹配到），这条命令实际没有生效"], []
        return [], [f"- {item['label']}：{len(changed)} 个文件改了，这 {len(miss)} 个内容没变（没匹配到）："
                    + "、".join(files[:12]) + ("……" if len(files) > 12 else "")
                    + "。如果它们本来也该改，说明这条命令对它们没生效"]
    return ([f"- {item['label']}（与任务意图 {', '.join(item['tasks'])} 共用路径，未单独记账，任务意图在 Stop 时由代码结算）"]
            + [f"    ✗ {m}：没有发生" for m in miss]), []


def on_post(root, data, out, failed=False):
    if data.get("tool_name") not in FILE_TOOLS and data.get("tool_name") not in SHELL_TOOLS:
        return
    led = open_ledger(root, create=False)
    if led is None:
        return
    bad = []
    for iid in _cc_ids(led, data.get("tool_use_id")):
        status = next((i["status"] for i in led.intents() if i["intent"] == iid), None)
        if status in L.FINAL_INTENT:
            continue
        r = led.intent_settle(iid, actor="cc-hook")
        if r["status"] == "settled":
            continue
        if failed and _try_cancel(led, iid, "工具报错，代码核实没有产生任何效果"):
            continue  # 工具如实报了错，世界也确实没变：账实一致，不必再提
        bad.append((iid, led._snapshot()[1].intents[iid]["open"].get("memo", ""), r))
    lines, hints = [], []
    for iid, memo, r in bad:
        lines.append(f"- {iid}（{memo}）：{r['status']}")
        lines += [f"    {fmt_result(x)}" for x in r["results"] if not x["match"]]
    if os.path.exists(state_path(root)):
        with state_lock(root):
            cs = load_state(root)
            shadow = (cs.get("shadow") or {}).pop(data.get("tool_use_id") or "", None)
            if shadow is not None:
                save_state(root, cs)
        for item in (shadow or []) if not failed else []:
            hard, soft = _check_shadow(led, item)
            lines += hard
            hints += soft
    if not lines and not hints:
        return
    if failed:
        head = "[复式记账] 工具报错，但下面这些效果已经部分发生，账上保持未结："
        tail = "这部分不是「什么都没做」，汇报时要说明哪些已经改动。"
    elif lines:
        head = "[复式记账] 工具返回成功，但代码核对实际效果与这次调用不符："
        tail = ("✗ 的项目实际没有发生，不能当作已完成；补做之后账本会在下一次调用或 Stop 时重新核对。"
                "如果确实不需要了，在汇报里说明即可。")
    else:
        head, tail = "[复式记账] 代码核对了这次调用的效果：", ""
    text = "\n".join([head] + lines + hints + ([tail] if tail else []))[:MAX_REASON]
    log(root, f"{'fail' if failed else 'post'}-{'flag' if lines else 'hint'} {data.get('tool_use_id')}: "
        + " / ".join(lines + hints)[:500])
    if failed:
        out.update({"hookSpecificOutput": {"hookEventName": "PostToolUseFailure", "additionalContext": text}})
    elif lines:
        out.update({"decision": "block", "reason": text})
    else:
        out.update({"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": text}})


def on_stop(root, data, out):
    led = open_ledger(root, create=False)
    if led is None:
        return
    sess = data.get("session_id")
    _, st = led._snapshot()

    def mine(iid):  # 工具意图只管本会话的；agent 开立的任务意图在项目里共享
        return not iid.startswith("cc-") or _meta_of(st, iid).get("session_id") in (None, sess)

    # 1. 重新结算全部未结意图：工具意图、agent 自己开的任务意图都由代码取数
    for it in led.intents(status="unresolved"):
        iid, status = it["intent"], it["status"]
        if status == "then_rejected" or not mine(iid):
            continue
        r = _try_settle(led, iid)
        if r and r["status"] != "settled" and iid.startswith("cc-") and status == "open":
            # 只开了单、从没收到 PostToolUse：调用被拒绝或中断。代码证明没产生效果就撤销
            _try_cancel(led, iid, "工具调用没有执行（被拒绝或中断），代码核实没有产生任何效果")
    # 2. 校验与汇总
    v = led.verify(strict=False)
    _, st = led._snapshot()
    unresolved = led.intents(status="unresolved")
    items, fp = [], []
    for it in unresolved:
        if not mine(it["intent"]):
            continue
        rec = st.intents[it["intent"]]
        last = rec.get("last_settle") or {}
        who = "工具调用" if it["intent"].startswith("cc-") else "你开立的任务意图"
        items.append(f"- {it['intent']}（{who}：{rec['open'].get('memo') or ', '.join(it['expects'])}）{it['status']}")
        items += [f"    {fmt_result(x)}" for x in last.get("results", []) if not x["match"]]
        fp.append([it["intent"], it["status"]])
    review = [m for m in v["warnings"] if m.startswith("本期有")]
    others = [m for m in v["warnings"] if not m.startswith(("intent ", "本期有"))]
    with state_lock(root):
        cs = load_state(root)
        cs.pop("shadow", None)  # 收不到 PostToolUse 的残留（被拒绝或中断的调用）
        save_state(root, cs)
    user_lines = []
    if v["violations"]:
        user_lines.append("✗ 账本校验失败：" + "；".join(v["violations"][:3]))
    if unresolved:
        user_lines.append(f"账本：{len(unresolved)} 笔意图未兑现（{', '.join(i['intent'] for i in unresolved[:6])}"
                          f"{'…' if len(unresolved) > 6 else ''}）。查看：ledger.py intent list --status unresolved")
    if others:
        user_lines.append("账本提示：" + "；".join(others[:3]))
    if review:
        user_lines.append("账本：" + review[0][:400] + "。复核人请在自己的终端执行 close <标签> --reviewed-by <姓名>")
    # 3. 有未兑现的意图：同一组问题只提醒 Claude 一次（按意图和状态判断），已经是钩子让它继续的这一轮不再提醒，
    #    避免来回打转；之后只给用户看。工具调用当时提示过的问题也再提一次：长任务里早先的 ✗ 很容易在最终汇报时被忘掉
    fp_s = json.dumps(sorted(fp), ensure_ascii=False)
    if fp and fp_s != cs.get("stop_fp") and not data.get("stop_hook_active"):
        with state_lock(root):
            cs = load_state(root)
            cs["stop_fp"] = fp_s
            save_state(root, cs)
        text = "\n".join(
            ["[复式记账] Stop 时代码重新取数，下面这些意图在账面上还没有兑现："] + items +
            ["这些事项按账本看并没有发生。若刚才的回复把它们说成已完成，需要更正；能补做的可以补做，"
             "做完后再停下时账本会重新核对；做不到或需要用户决定的，如实说明并保持未结（不要撤销、不要核销来让账变平）。"])
        out.update({"hookSpecificOutput": {"hookEventName": "Stop", "additionalContext": text[:MAX_REASON]}})
        log(root, "stop-prompt: " + ", ".join(x[0] for x in fp))
        return
    if not unresolved and cs.get("stop_fp"):
        with state_lock(root):
            cs = load_state(root)
            cs.pop("stop_fp", None)
            save_state(root, cs)
    # 4. 账目干净（无未结、无挂账/标红/门槛、无待复核）且本期有发生额：自动结账
    if not unresolved and not v["violations"] and not others and not review \
            and os.environ.get("LEDGER_CC_AUTOCLOSE", "1") != "0":
        last_close = st.closes[-1]["seq"] if st.closes else 0
        if st.last_posting_seq > last_close:
            n = sum(1 for i in led.intents() if i["opened_seq"] > last_close)
            label = "cc-" + datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            try:
                rec = led.close(label, actor="cc-hook")
                with open(os.path.join(ledger_dir(root), "anchors.txt"), "a", encoding="utf-8") as f:
                    f.write(f"{rec['ts']} {label} {rec['hash']}\n")
                user_lines.append(f"账本 ✓ 本期 {n} 笔意图全部由代码核实，已结账 {label}（链头 {rec['hash'][:12]}）")
            except L.CheckFailed as e:
                user_lines.append("账本：结账被拒——" + str(e)[:300])
    if user_lines:
        out["systemMessage"] = "\n".join(user_lines)[:MAX_REASON]
        log(root, "stop-notice: " + " | ".join(user_lines)[:500])


def main(argv):
    event = argv[1] if len(argv) > 1 else ""
    if os.environ.get("LEDGER_CC_HOOKS", "").lower() in ("off", "0", "false"):
        return 0
    raw = sys.stdin.read()
    try:
        data = json.loads(raw) if raw.strip() else {}
    except ValueError:
        return 0
    root = project_root(data)
    out = {}
    try:
        if event == "pre":
            on_pre(root, data, out)
        elif event == "post":
            on_post(root, data, out)
        elif event == "fail":
            on_post(root, data, out, failed=True)
        elif event == "stop":
            on_stop(root, data, out)
    except Exception as e:  # noqa: BLE001  钩子出错不能打断用户的会话：放行并留日志
        log(root, f"{event} 出错：{e!r}\n" + traceback.format_exc(limit=5))
        if event == "stop":
            out = {"systemMessage": f"账本钩子出错，本轮未对账：{e!r}（详见 .ledger/cc-hook.log）"}
        elif "hookSpecificOutput" not in out:
            out = {}
    if out:
        sys.stdout.write(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
