import { indexNote } from '../search/index';
import { saveNote } from './store';

export async function createNote(userId: string, body: string) {
  const note = await saveNote(userId, { id: `${Date.now()}`, body });
  await indexNote(note);
  return note;
}
