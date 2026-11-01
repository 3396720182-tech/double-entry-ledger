#!/usr/bin/env bash
# 落地对账端到端演示：对 examples/demo/project（一个虚构的小服务）跑一遍 lint → 试算与门禁 → 对抗凭证 → 反向扫描。
# 借方和凭证是手写的，代替真实流程里「规划会话」和「实现会话」的产出；不需要模型，也不写任何文件。
# 用法：bash examples/demo.sh
set -u
cd "$(dirname "$0")/demo" || exit 2

LD() { echo "\$ landing $*"; PYTHONDONTWRITEBYTECODE=1 python3 ../../scripts/landing.py "$@"; echo "  → exit $?"; }

echo "== 1. 格式检查：借方要带接线约束，凭证要对得上借方 =="
LD lint --debits debits.json --vouchers vouchers.json

echo; echo "== 2. 试算与门禁：只拦高精度信号，机械驳回进复核清单 =="
LD reconcile project --debits debits.json --vouchers vouchers.json

echo; echo "== 3. 对抗凭证：拿「改名时写的审计日志」冒充「删除前写审计日志」 =="
echo "-- 借方只写到「函数存在、被调用」：lint 先提示，核验被骗过"
LD lint --debits "adversarial/debit-weak.json"
LD reconcile project --debits "adversarial/debit-weak.json" --vouchers "adversarial/voucher.json"
echo "-- 同一条决定，借方带上 scope_hint + must_include：核验驳回"
LD reconcile project --debits "adversarial/debit-strong.json" --vouchers "adversarial/voucher.json"

echo; echo "== 4. 反向对账：有定义、生产零调用（正向对账看不见的承诺） =="
LD orphans project
