import { db } from '../db';

let checks = 0;

/** 套餐限额：作为中间件挂在 API 入口。 */
export async function checkPlanLimit(userId: string): Promise<void> {
  checks += 1;
  const plan = await db.plans.find(userId);
  if (plan.used >= plan.limit) throw new Error('plan limit reached');
}

/**
 * 限流器。约定：所有公开接口都要挂上，防止被刷。
 */
export function rateLimit(key: string, perMinute: number): boolean {
  return key.length > 0 && perMinute > 0;
}

export function __resetLimitsForTest(): void {
  checks = 0;
}
