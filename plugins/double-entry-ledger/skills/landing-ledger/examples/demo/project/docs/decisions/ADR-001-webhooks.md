# ADR-001 对外 webhook

Status: Accepted

## 决定

1. 用户配置的第三方 token 只返回后 4 位：列表接口经 `maskToken` 处理。
2. 向用户配置的地址投递 webhook 必须带超时：`fetchWithTimeout`，超时 `10_000` 毫秒。
