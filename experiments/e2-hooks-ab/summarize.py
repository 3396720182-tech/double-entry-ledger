#!/usr/bin/env python3
"""把各轮 summary.json 合并成报告用的对比表。用法：python3 summarize.py <runs 根目录>"""
import json
import os
import sys

ROOT = sys.argv[1]
ITEMS = ("versions", "changelog", "tasks", "docs")

# (表名, 条件说明, [(组名, [(轮次目录, 组)...])])
GROUPS = [
    ("常规条件（不施压）", [
        ("Opus · A 无 skill", [("exp2-opus", "A"), ("exp3-opus", "A")]),
        ("Opus · B v0.3.0（无钩子）", [("exp2-opus", "B"), ("exp3-opus", "B")]),
        ("Opus · C v0.4.0 钩子（早期版）", [("exp2-opus", "C"), ("exp3-opus", "C")]),
        ("Opus · C v0.4.0 钩子（最终版）", [("exp6-normal-opus", "C")]),
        ("Haiku · A 无 skill", [("exp2-haiku", "A"), ("exp3-haiku", "A")]),
        ("Haiku · B v0.3.0（无钩子）", [("exp2-haiku", "B"), ("exp3-haiku", "B")]),
        ("Haiku · C v0.4.0 钩子（早期版）", [("exp2-haiku", "C"), ("exp3-haiku", "C")]),
        ("Haiku · C v0.4.0 钩子（最终版）", [("exp6-normal-haiku", "C")]),
    ]),
    ("「赶时间，别打开文件检查」条件", [
        ("Opus · A 无 skill", [("exp4-opus", "A"), ("exp5-opus", "A")]),
        ("Opus · B v0.3.0（无钩子）", [("exp4-opus", "B")]),
        ("Opus · C v0.4.0 钩子（各版合计）", [("exp4-opus", "C"), ("exp5-opus", "C"), ("exp6-hurry-opus", "C")]),
        ("Haiku · A 无 skill", [("exp4-haiku", "A"), ("exp5-haiku", "A")]),
        ("Haiku · B v0.3.0（无钩子）", [("exp4-haiku", "B")]),
        ("Haiku · C 早期版（只有工具级钩子）", [("exp4-haiku", "C")]),
        ("Haiku · C 中期版（+首次提醒、后续命令屏障）", [("exp5-haiku", "C")]),
        ("Haiku · C 最终版（+审查修复、项目脚本提醒）", [("exp6-hurry-haiku", "C"), ("exp7-hurry-haiku", "C")]),
    ]),
]


def load(rd):
    p = os.path.join(ROOT, rd, "summary.json")
    return json.load(open(p, encoding="utf-8")) if os.path.exists(p) else []


def main():
    out = []
    for title, groups in GROUPS:
        out.append(f"### {title}\n")
        out.append("| 组 | n | 4 项平均完成 | 有虚报的运行 | 虚报项（自报 done、实际没做成） | 平均成本 $ | 平均轮数 | 平均耗时 s |")
        out.append("|---|---|---|---|---|---|---|---|")
        for name, parts in groups:
            rows = [r for rd, arm in parts for r in load(rd) if r["arm"] == arm]
            if not rows:
                continue
            n = len(rows)
            over_runs = sum(1 for r in rows if r["genuine_overclaims"])
            items = {}
            for r in rows:
                for k in r["genuine_overclaims"]:
                    items[k] = items.get(k, 0) + 1
            out.append(f"| {name} | {n} | {sum(r['done'] for r in rows) / n:.2f} | {over_runs}/{n} | "
                       f"{'、'.join(f'{k}×{v}' for k, v in sorted(items.items())) or '—'} | "
                       f"{sum(r['cost'] or 0 for r in rows) / n:.3f} | {sum(r['turns'] or 0 for r in rows) / n:.1f} | "
                       f"{sum(r['dur_s'] or 0 for r in rows) / n:.0f} |")
        out.append("")
    print("\n".join(out))


if __name__ == "__main__":
    main()
