import { __resetLimitsForTest, checkPlanLimit, rateLimit } from '../src/api/limits';

jest.mock('../src/db', () => ({ db: { plans: { find: async () => ({ used: 0, limit: 1 }) } } }));

test('plan limit passes under quota', async () => {
  __resetLimitsForTest();
  await checkPlanLimit('u1');
});

test('rate limit accepts a key', () => {
  expect(rateLimit('ip:1', 60)).toBe(true);
});
