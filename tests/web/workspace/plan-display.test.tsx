import React from 'react';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { PlanCard } from '../../../apps/web/src/features/plans/PlanCard';
import { GuidancePanel } from '../../../apps/web/src/features/workspace/GuidancePanel';
import type { Schema } from '../../../apps/web/src/api/client';
import { plan, settings, workspace } from '../fixtures';

let current: Schema['Workspace'];
let candidate: Schema['PlanRecord'];
vi.mock('../../../apps/web/src/api/queries', () => ({
  useSettings: () => ({ data: settings }),
  useWorkspace: () => ({ data: current, isError: false }),
}));
beforeEach(() => {
  candidate = { ...plan, lifecycle: 'WAITING_TRIGGER', definition: { ...plan.definition,
    trading_style: 'scalp', entry: { mode: 'limit_retest', price: '100', zone: { low: '99.9', high: '100' } },
    rationale: 'SYNTHETIC 保留的完整原方案依据', entry_deadline: '2026-10-12T02:01:00Z' } };
  current = { ...workspace, as_of: '2026-10-12T02:00:00Z', plans: [candidate],
    analysis_refresh: [{ trading_style: 'scalp', state: 'FRESH', forecast_id: 'f', version_id: 'v',
      market_cutoff: '2026-10-12T01:31:00Z', remind_at: null, fresh_until: '2026-10-12T02:01:00Z',
      reasons: [], request_kind: 'new', pending_request_id: null }],
    action_advice: [{ plan_id: plan.plan_id, version_id: 'v', trading_style: 'scalp', state: 'WAITING', side: 'long',
      title: '超短线 · 等待入场条件', reasons: ['结构化触发条件尚未确认。'], evaluated_at: '2026-10-12T02:00:00Z',
      market_snapshot_id: 'm', quote_at: '2026-10-12T02:00:00Z', valid_until: '2026-10-12T02:00:05Z',
      entry_price: '101', allowed_zone: { low: '99.9', high: '100' }, stop_loss: '99', take_profit: '103',
      volume_lots: '0.01', entry_deadline: candidate.definition.entry_deadline, max_holding_minutes: 20, evaluation_id: 'e' }] };
});
afterEach(() => { cleanup(); vi.useRealTimers(); });
function show(both = false) {
  return render(<QueryClientProvider client={new QueryClient()}><MemoryRouter>
    {both && <GuidancePanel workspace={current} />}<PlanCard plan={candidate} />
  </MemoryRouter></QueryClientProvider>);
}

it('未触发的回踩方案把方式与当前状态分开，原依据默认折叠并可展开', () => {
  show();
  expect(screen.getByText('入场方式：回踩限价')).toBeTruthy();
  expect(screen.getByText('等待入场条件', { exact: true })).toBeTruthy();
  expect(screen.queryByText(/触发后等待回踩/)).toBeNull();
  const details = screen.getByText('SYNTHETIC 保留的完整原方案依据').closest('details')!;
  expect(details).not.toBeNull();
  expect(details.open).toBe(false);
  fireEvent.click(screen.getByText('方案依据与参数'));
  expect(details.open).toBe(true);
  expect(screen.getByText('观察区间 99.9 – 100')).toBeTruthy();
});

it('触发已确认但未到价格区间时，显示后台等待状态而不是仅显示触发已确认', () => {
  candidate.lifecycle = 'ACTIVE';
  Object.assign(current.action_advice![0], { title: '超短线 · 已触发，等待回踩区间' });
  show();
  expect(screen.getByText('已触发，等待回踩区间', { exact: true })).toBeTruthy();
  expect(screen.getByText('入场方式：回踩限价')).toBeTruthy();
});

it('行情停止刷新后，候选卡片撤销过时的当前可入场显示', () => {
  vi.useFakeTimers();
  candidate.lifecycle = 'ACTIVE';
  Object.assign(current.action_advice![0], { state: 'READY', title: '超短线 · 当前可以买入' });
  show();
  expect(screen.getByText('当前可以买入', { exact: true })).toBeTruthy();
  act(() => vi.advanceTimersByTime(6000));
  expect(screen.queryByText('当前可以买入', { exact: true })).toBeNull();
  expect(screen.getByText('报价待更新，暂停入场提示')).toBeTruthy();
});

it('到期时上方判断和候选卡片同步停止等待，不因原触发状态而继续显示有效', () => {
  vi.useFakeTimers();
  candidate.lifecycle = 'ACTIVE';
  show(true);
  act(() => vi.advanceTimersByTime(61000));
  expect(screen.getAllByText('候选入场期限已到', { exact: true })).toHaveLength(2);
});

it('快捷指标区分当前核验值与方案原值', () => {
  show();
  expect(screen.getByText('当前执行价')).toBeTruthy();
  expect(screen.getByText('当前止损')).toBeTruthy();
  expect(screen.getByText('当前目标')).toBeTruthy();
  expect(screen.getByText('当前允许入场范围：99.9—100 · 当前最晚入场 10/12 10:01:00')).toBeTruthy();

  Object.assign(current.action_advice![0], { entry_price: null, allowed_zone: null });
  cleanup();
  show();
  expect(screen.getByText('方案入场价')).toBeTruthy();
  expect(screen.getByText('当前执行侧尚未核验')).toBeTruthy();
  expect(screen.getByText('当前允许入场范围：尚未核验 · 当前最晚入场 10/12 10:01:00')).toBeTruthy();
});

it('GuidancePanel 与 PlanCard 使用同一有效入场截止，优先后端 entry_timing', () => {
  Object.assign(current.action_advice![0], {
    entry_timing: {
      policy_version: 'entry-observation/1', enforced: true, state: 'SUFFICIENT',
      market_cutoff: '2026-10-12T01:31:00Z', available_at: '2026-10-12T01:40:00Z', effective_from: '2026-10-12T01:40:00Z',
      analysis_fresh_until: '2026-10-12T02:00:50Z', effective_entry_deadline: '2026-10-12T02:00:30Z', minimum_entry_deadline: '2026-10-12T02:00:00Z',
      import_delay_seconds: 0, remaining_at_import_seconds: 30, required_activation_closes: 1, post_trigger_reserve_seconds: 10,
      activation_closes_at_import: 1, reasons: [],
    },
  });
  show(true);
  expect(screen.getAllByText(/当前最晚入场 10\/12 10:00:30/)).toHaveLength(1);
  expect(screen.getAllByText(/最晚入场 10\/12 10:00:30/)).toHaveLength(2);
});
