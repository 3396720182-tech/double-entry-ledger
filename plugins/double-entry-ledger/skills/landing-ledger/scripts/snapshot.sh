#!/usr/bin/env bash
# 只读导出某个提交的快照，用于历史回放（不 checkout、不建分支、不碰工作区）。
#   bash snapshot.sh <仓库路径> <提交> <输出目录> [只导出的路径...]
# 例：只给规划会话看设计文档
#   bash snapshot.sh ~/code/app v1.2.0 /tmp/snap/docs-only docs
#   bash snapshot.sh ~/code/app v1.2.0 /tmp/snap/full
set -euo pipefail
[ $# -ge 3 ] || { sed -n '2,7p' "$0"; exit 2; }
repo="$1"; rev="$2"; out="$3"; shift 3
[ -e "$out" ] && [ -n "$(ls -A "$out" 2>/dev/null)" ] && { echo "输出目录非空：${out}" >&2; exit 2; }
mkdir -p "$out"
git -C "$repo" archive --format=tar "$rev" -- "$@" | tar -x -C "$out"
echo "$(git -C "$repo" rev-parse "$rev") $(git -C "$repo" log -1 --format=%cI "$rev")" > "$out/.snapshot"
echo "已导出 $rev → ${out}（$(find "$out" -type f | wc -l | tr -d ' ') 个文件）"
