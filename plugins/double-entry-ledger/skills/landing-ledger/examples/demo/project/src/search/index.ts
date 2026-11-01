import { db } from '../db';

export async function indexNote(note: { id: string; body: string }): Promise<void> {
  await db.search.put(note.id, note.body.toLowerCase());
}
