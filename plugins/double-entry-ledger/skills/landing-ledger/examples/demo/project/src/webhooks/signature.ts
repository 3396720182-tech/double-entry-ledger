import { createHmac, timingSafeEqual } from 'crypto';

export function verifyWebhookSignature(body: string, header: string, secret: string): boolean {
  const expected = createHmac('sha256', secret).update(body).digest('hex');
  return header.length === expected.length && timingSafeEqual(Buffer.from(header), Buffer.from(expected));
}
