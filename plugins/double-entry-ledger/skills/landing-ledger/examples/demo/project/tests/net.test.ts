import { fetchWithTimeout } from '../src/util/net';

test('aborts after timeout', async () => {
  await expect(fetchWithTimeout('http://10.255.255.1', {}, 10)).rejects.toThrow();
});
