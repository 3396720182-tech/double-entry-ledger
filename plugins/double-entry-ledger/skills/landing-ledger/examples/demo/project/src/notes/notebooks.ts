import { recordAudit } from '../audit/log';
import { db } from '../db';

export async function renameNotebook(userId: string, id: string, name: string) {
  await recordAudit(userId, 'RENAME', id);
  return db.notebooks.update(id, { name });
}

export async function deleteNotebook(userId: string, id: string) {
  await db.notebooks.delete(id);
}
