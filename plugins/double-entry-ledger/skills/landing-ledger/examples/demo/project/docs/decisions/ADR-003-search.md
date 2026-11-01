# ADR-003 搜索索引

Status: Accepted

## 决定

1. 笔记保存成功后，在 `notes/store.ts` 的 `saveNote` 里调用 `indexNote` 更新搜索索引，保证每条笔记都能被搜到。
