#!/usr/bin/env bash
# 崩溃恢复场景：在 setup_env.sh 的项目上，预置一份「上一个 agent 跑到一半崩了」的账本
# 用法：bash setup_crash.sh <目标目录> <skill 目录>
set -eu
D="$1"; SK="$2"
bash "$(dirname "$0")/setup_env.sh" "$D"
cd "$D"
L() { python3 "$SK/scripts/ledger.py" --actor worker-0 "$@" >/dev/null; }
L init
L post --id create-tasks 'world:created -3' 'state:todo 3' --memo '任务 a b c 建卡'
L post --id fund 'equity:funding -20000 tokens' 'budget:pool 20000 tokens'
L transfer --id grant-worker --from budget:pool --to budget:agent:worker-0 --amount 8000 --unit tokens
# a：页面已写并核实
L intent open write-a --expect 'exists:site/a.html =1'
printf '<h1>关于我们</h1>\n<p>我们是一家小公司。</p>\n' > site/a.html
L intent settle write-a
# a：任务卡已移动，但 settle 前崩了
L intent open mv-a --expect 'exists:tasks/done/a.md =1' --expect 'exists:tasks/todo/a.md =0' \
  --then 'state:todo -1' --then 'state:done 1'
mv tasks/todo/a.md tasks/done/a.md
# b：开了意图，还没写就崩了
L intent open write-b --expect 'exists:site/b.html =1'
# 通知客户上线的邮件：开了意图，发没发不知道（没有观测）
L intent open notify-client --tool send_email --expect 'claim:sent =1' --memo '给客户发上线通知'
# worker 记过一笔花费，之后失联
L transfer --id spend-worker-1 --from budget:agent:worker-0 --to expense:tokens:worker-0 --amount 3100 --unit tokens
