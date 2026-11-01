import { db } from '../db';

export function maskToken(token: string): string {
  return token.length <= 4 ? '****' : '****' + token.slice(-4);
}

export async function listIntegrationTokens(userId: string) {
  const rows = await db.tokens.findMany({ userId });
  return rows.map((r: any) => ({ name: r.name, token: maskToken(r.token) }));
}
