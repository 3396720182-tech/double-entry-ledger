import { saveNote } from './store';

export async function importNotes(userId: string, bodies: string[]) {
  const out = [];
  for (const body of bodies) {
    out.push(await saveNote(userId, { id: `${Date.now()}-${out.length}`, body }));
  }
  return out;
}
