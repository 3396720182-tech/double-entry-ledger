import { db } from '../db';

export async function listUnusedTags() {
  return db.tags.findMany({ notes: 0 });
}
