import { db } from '../db';
import { gone, notFound } from '../errors';

const WEEK = 7 * 24 * 3600 * 1000;

function assertShareAlive(link: any) {
  if (!link) throw notFound();
  if (link.expiresAt < Date.now()) throw gone();
}

export async function createShareLink(noteId: string) {
  return db.links.insert({ noteId, expiresAt: Date.now() + WEEK });
}

export async function openShareLink(token: string) {
  const link = await db.links.find(token);
  if (!link) throw notFound();
  return db.notes.find(link.noteId);
}

export async function deleteShareLink(token: string) {
  const link = await db.links.find(token);
  assertShareAlive(link);
  await db.links.delete(token);
}
