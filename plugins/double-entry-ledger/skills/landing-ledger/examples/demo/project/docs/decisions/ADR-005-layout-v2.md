# ADR-005 文档版式 v2

Status: Accepted

## 决定

1. 打开 v1 文档时由 `loader` 调用 `upgradeLegacyLayout` 转成 v2 版式。
2. 升级完成后把文档的 `formatVersion` 写回为 2，避免下次重复升级。
