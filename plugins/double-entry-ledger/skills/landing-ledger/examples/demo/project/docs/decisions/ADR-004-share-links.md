# ADR-004 分享链接

Status: Accepted

## 决定

- [x] 1. 分享链接默认 7 天过期：创建时写 `expiresAt`，打开已过期的链接一律返回 410，统一走 `assertShareAlive`。
