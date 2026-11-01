import { createHmac } from 'crypto';
import { verifyWebhookSignature } from '../src/webhooks/signature';

test('accepts a valid signature', () => {
  const sig = createHmac('sha256', 's').update('{}').digest('hex');
  expect(verifyWebhookSignature('{}', sig, 's')).toBe(true);
});
