import { fetchWithTimeout } from '../util/net';

export async function deliverWebhook(url: string, payload: object): Promise<number> {
  const res = await fetchWithTimeout(url, { method: 'POST', body: JSON.stringify(payload) }, 10_000);
  return res.status;
}

export async function receiveWebhook(req: any) {
  return { ok: true, event: JSON.parse(req.body).type };
}
