import React from 'react';
import { afterEach, expect, it, vi } from 'vitest';
import { cleanup, render, screen, within } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { ReviewList } from '../../../apps/web/src/features/review/ReviewList';
import { api, type Schema } from '../../../apps/web/src/api/client';

afterEach(() => { cleanup(); vi.restoreAllMocks(); });

function sample(kind: Schema['Review']['target_kind'], outcome: Schema['Review']['outcome'], amount: string | null): Schema['Review'] {
  return {
    review_id: 'synthetic-review', revision: 1, target_kind: kind, target_id: 'synthetic-target',
    method_version: kind === 'forecast' ? 'prediction_review/2' : 'synthetic/1',
    window_start: '2026-10-16T13:45:49Z', window_end: '2026-10-17T01:45:49Z',
    available_at: '2026-10-17T16:33:59Z', coverage: 'complete', outcome,
    net_pnl: amount == null ? null : { amount, currency: 'USD' }, metrics: [], reasons: [],
    source_refs: [], evaluated_start: null, evaluated_end: null, source_kind: 'synthetic',
    inputs: { forecast_version_id: 'version', plan_id: null, trade_id: null, horizon_minutes: 720,
      evaluation_scope: 'version_descriptive', market_snapshot_ids: [], cost_settings_revision: null,
      method_parameters: { neutral_band: '0.00', price_basis: 'bid', bar_timeframe: 'M1',
        turning_left_bars: 2, turning_right_bars: 2, execution_model_version: null,
        simulation_delay_ms: null, execution_quote_wait_ms: null } },
  };
}

async function show(row: Schema['Review']) {
  const before = JSON.stringify(row);
  vi.spyOn(api, 'reviews').mockResolvedValue({ items: [row], next_cursor: null });
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <ReviewList kind={row.target_kind} />
  </QueryClientProvider>);
  const table = await screen.findByRole('table');
  expect(JSON.stringify(row)).toBe(before);
  return within(table);
}

it('does not imply a missing profit result for a descriptive forecast', async () => {
  const table = await show(sample('forecast', 'EVALUATED', null));
  expect(table.queryByRole('columnheader', { name: /净结果/ })).toBeNull();
  expect(table.queryByText(/未知.*不适用/)).toBeNull();
  expect(table.getByText('已评价')).toBeTruthy();
  expect(screen.getByText('行情预测评价方向、区间、阶段与转折，不计算交易盈亏。')).toBeTruthy();
});

it.each([
  ['TIME_EXIT', '0', '0.00 USD'],
  ['SL', '-1.25', '-1.25 USD'],
  ['DATA_INCOMPLETE', null, '未知'],
  ['NOT_TRIGGERED', null, '不适用（未入场）'],
  ['EXPIRED_WITHOUT_ENTRY', null, '不适用（未入场）'],
] as const)('keeps reference money and missing-entry semantics distinct: %s', async (outcome, amount, text) => {
  const table = await show(sample('reference_plan', outcome, amount));
  expect(table.getByRole('columnheader', { name: '参考净结果' })).toBeTruthy();
  expect(table.getByText(text, { exact: true })).toBeTruthy();
});

it.each([
  ['ACTUAL_CLOSED', '0', '0.00 USD'],
  ['ACTUAL_CLOSED', '-1.25', '-1.25 USD'],
  ['ACTUAL_OPEN', null, '未知'],
] as const)('keeps actual net results independent from simulations: %s / %s', async (outcome, amount, text) => {
  const table = await show(sample('actual_trade', outcome, amount));
  expect(table.getByRole('columnheader', { name: '实际净结果' })).toBeTruthy();
  expect(table.getByText(text, { exact: true })).toBeTruthy();
});
