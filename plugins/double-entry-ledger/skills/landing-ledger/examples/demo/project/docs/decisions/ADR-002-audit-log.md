# ADR-002 审计日志

Status: Accepted

## 决定

1. 删除笔记本之前，由 notebooks 模块调用 `recordAudit` 记一条审计日志，action 为 `"DELETE"`；写失败就不删（fail-closed）。
2. 每条审计日志带 `requestId`，`queryAudit` 按 `requestId` 去重，避免重试写出重复记录。
3. [x] 套餐限额检查 `checkPlanLimit` 挂在 API 入口的中间件上。
4. 每季度由安全负责人人工抽查一次审计日志。
