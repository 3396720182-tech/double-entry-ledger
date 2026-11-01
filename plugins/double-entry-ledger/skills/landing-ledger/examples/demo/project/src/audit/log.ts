import { db } from '../db';

export type AuditAction = 'DELETE' | 'RENAME';

export async function recordAudit(userId: string, action: AuditAction, target: string): Promise<void> {
  const ok = await db.audit.insert({ userId, action, target, at: Date.now() });
  if (!ok) throw new Error('audit log unavailable');
}

export async function queryAudit(userId: string) {
  const rows = await db.audit.findMany({ userId });
  const seen = new Set<string>();
  // 去重：同一个 requestId 只保留一条
  return rows.filter((r: any) => !r.requestId || (!seen.has(r.requestId) && seen.add(r.requestId)));
}
