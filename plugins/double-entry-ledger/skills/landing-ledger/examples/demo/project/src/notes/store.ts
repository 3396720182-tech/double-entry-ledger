import { db } from '../db';

export async function saveNote(userId: string, note: { id: string; body: string }) {
  await db.notes.upsert({ ...note, userId, updatedAt: Date.now() });
  return note;
}
