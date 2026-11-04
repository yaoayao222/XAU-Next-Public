import React from 'react';
import { afterEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { ReviewList, reviewResultText } from '../../../apps/web/src/features/review/ReviewList';
import { api, type Schema } from '../../../apps/web/src/api/client';

afterEach(() => { cleanup(); vi.restoreAllMocks(); });

function sample(state: Schema['ReviewSessionDiagnosis']['state'] = 'market_closed'): Schema['Review'] {
  return {
    review_id: 'review', revision: 1, target_kind: 'forecast', target_id: 'forecast',
    method_version: 'prediction_review/2', window_start: '2026-10-16T13:45:49Z',
    window_end: '2026-10-17T01:45:49Z', available_at: '2026-10-17T16:33:59Z',
    coverage: 'missing', outcome: 'DATA_INCOMPLETE', net_pnl: null, metrics: [], reasons: [],
    source_refs: [], evaluated_start: null, evaluated_end: null, source_kind: 'synthetic',
    inputs: { forecast_version_id: 'version', plan_id: null, trade_id: null, horizon_minutes: 720,
      evaluation_scope: 'version_descriptive', market_snapshot_ids: [], cost_settings_revision: null,
      method_parameters: { neutral_band: '0.00', price_basis: 'bid', bar_timeframe: 'M1',
        turning_left_bars: 2, turning_right_bars: 2, execution_model_version: null,
        simulation_delay_ms: null, execution_quote_wait_ms: null } },
    diagnostics: { method_version: 'review-diagnostics/1', computed_at: '2026-10-17T18:00:00Z',
      direction_basis: 'legacy_complete_m1', alignment_metrics: [], reasons: ['原评分不修改'],
      session: { state, expected_minutes: 719, observed_minutes: 429, closed_missing_minutes: 290,
        unexplained_missing_minutes: state === 'mixed' ? 1 : 0, schedule_version: 'synthetic/1',
        schedule_url: null, schedule_checked_at: null, reasons: ['合成时段证据'] } },
  };
}

it('shows closed-window diagnosis while retaining original outcome and score basis', async () => {
  const row = sample(); const original = JSON.stringify(row);
  const { diagnostics, ...stored } = row;
  vi.spyOn(api, 'reviews').mockResolvedValue({ items: [stored], next_cursor: null });
  const detail = vi.spyOn(api, 'review').mockResolvedValue(row);
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <ReviewList kind="forecast" />
  </QueryClientProvider>);
  expect(await screen.findByText('证据不完整')).toBeTruthy();
  expect(detail).not.toHaveBeenCalled();
  fireEvent.click(await screen.findByRole('button', { name: '详情' }));
  expect(await screen.findByText('跨休市，未作全窗评价')).toBeTruthy();
  expect(screen.getByText(/原评分采用完整M1裁剪起止价/)).toBeTruthy();
  expect(screen.getByText(/冻结Bid方向核对：资料不足，未评分/)).toBeTruthy();
  expect(screen.getByText(/完整分钟 429\/719/)).toBeTruthy();
  expect(screen.getByText(/证据不完整 · 修订/)).toBeTruthy();
  expect(JSON.stringify(row)).toBe(original);
});

it('does not label missing trading minutes or schedule conflicts as normal closed windows', () => {
  expect(reviewResultText(sample('mixed'))).toBe('休市及待核对缺数');
  expect(reviewResultText(sample('data_gap'))).toBe('证据不完整');
  expect(reviewResultText(sample('unverified'))).toBe('证据不完整');
  expect(reviewResultText(sample('schedule_conflict'))).toContain('时段证据冲突');
});
