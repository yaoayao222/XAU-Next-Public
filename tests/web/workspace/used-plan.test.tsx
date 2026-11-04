import React from 'react';
import { afterEach, expect, it, vi } from 'vitest';
import { cleanup, render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { PlanCard } from '../../../apps/web/src/features/plans/PlanCard';
import { plan, settings, workspace } from '../fixtures';

vi.mock('../../../apps/web/src/api/queries', () => ({
  useSettings: () => ({ data: settings }),
  useWorkspace: () => ({ data: { ...workspace, action_advice: [{ plan_id: plan.plan_id, state: 'USED' }] } }),
}));
afterEach(cleanup);

it('方案卡片也展示已使用，不把原触发状态继续显示为可用', () => {
  render(<QueryClientProvider client={new QueryClient()}><PlanCard plan={{ ...plan, lifecycle: 'ACTIVE' }} /></QueryClientProvider>);
  expect(screen.getByText('已使用，不再入场')).toBeTruthy();
  expect(screen.queryByText('条件有效')).toBeNull();
  expect(screen.getByText('此方案已关联真实持仓或成交，平仓后不恢复入场资格。下方保留原方案与金额依据。')).toBeTruthy();
  expect(screen.getByText('等待触发期间及触发后均生效；不会等到突破后才检查。')).toBeTruthy();
});
