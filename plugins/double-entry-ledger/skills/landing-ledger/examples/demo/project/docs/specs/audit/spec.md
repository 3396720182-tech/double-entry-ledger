# Spec: audit

## Requirement: 审计日志由网关统一写入（方案 B）

审计日志 MUST 由网关统一生成和写入，应用内不再生成 requestId。
因此 ADR-002 第 2 条的 requestId 去重不再需要。

## Requirement: 保留期

审计日志 MUST 保留 180 天。
