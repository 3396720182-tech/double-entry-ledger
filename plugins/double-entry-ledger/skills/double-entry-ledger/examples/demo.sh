#!/usr/bin/env bash
# 三本账端到端演示：在临时目录里跑，不碰你的项目。用法：bash examples/demo.sh
set -u
HERE="$(cd "$(dirname "$0")/.." && pwd)"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/ledger-demo.XXXXXX")"
cd "$WORK" || exit 2
L() { echo "\$ ledger $*"; python3 "$HERE/scripts/ledger.py" "$@"; echo "  → exit $?"; }

mkdir -p tasks/todo tasks/done && echo "# 登录页" > tasks/todo/login.md
L init >/dev/null

echo; echo "== 1. 状态账：有借必有贷 =="
L post --id new-login 'world:created -1' 'state:todo 1'
L post --id oops 'state:todo -1' 'state:done 2'                  # 借贷不平 → 拒绝

echo; echo "== 2. 意图清账：把计划当结果会被当场抓住 =="
L intent open mv-login --tool mv \
  --expect 'glob-count:tasks/done/*.md +1' --expect 'glob-count:tasks/todo/*.md -1' \
  --then 'state:todo -1' --then 'state:done 1'
L intent settle mv-login                                          # 还没动手 → 不符，记忆不写
L balance 'state:*'
mv tasks/todo/login.md tasks/done/login.md                        # 真正执行
L intent settle mv-login                                          # 相符 → then 过账
L balance 'state:*'

echo; echo "== 3. 资源账：超支照记标红、子 agent 失联 =="
L post --id fund 'equity:funding -2000 tokens' 'budget:pool 2000 tokens'
L transfer --id grant-A --from budget:pool --to budget:agent:A --amount 400 --unit tokens
L transfer --id grant-B --from budget:pool --to budget:agent:B --amount 400 --unit tokens
L transfer --id spend-A --from budget:agent:A --to expense:tokens:A --amount 380 --unit tokens
L transfer --id refund-A --from budget:agent:A --to budget:pool --amount 20 --unit tokens
L transfer --id spend-B --from budget:agent:B --to expense:tokens:B --amount 900 --unit tokens   # 钱已花掉：照记，标红
L verify
L close run-1                                                     # B 超支未处理 → 结账被拒

echo; echo "== 4. 外部对账：供应商计量 vs 账面 =="
echo '{"expense:tokens:A": {"tokens": 380}, "expense:tokens:B": {"tokens": 920}}' > usage.json
L reconcile --snapshot usage.json --adjust                        # B 还漏报了 20 → 差额进挂账
L suspense
L post --id fix-sus 'suspense:reconcile:expense:tokens:B 20 tokens' 'budget:agent:B -20 tokens'   # 普通 post 不能碰挂账 → 拒绝
L clear-suspense suspense:reconcile:expense:tokens:B --to budget:agent:B --reason 'B 有 20 已花未报，计入其预算'
L transfer --id topup-B --from budget:pool --to budget:agent:B --amount 520 --unit tokens --memo '补批 B 的超支'
L verify --strict                                                  # 账已平，但对账调整、清挂账属于人工干预 → 列出待复核
L close run-1                                                      # 人复核后结账，即确认
L verify --strict

echo; echo "== 5. 篡改检测 =="
python3 - <<'PY'
import re; p=".ledger/journal.jsonl"; s=open(p).read(); open(p,"w").write(s.replace('"amount":"380"','"amount":"38"',1))
PY
L verify

echo; echo "演示目录：$WORK"
