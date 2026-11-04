import React from 'react';
import { cleanup, render, screen, within } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { PlanCard } from '../../../apps/web/src/features/plans/PlanCard';
import { GuidancePanel } from '../../../apps/web/src/features/workspace/GuidancePanel';
import { AnswerView } from '../../../apps/web/src/features/analysis/VersionHistory';
import type { Schema } from '../../../apps/web/src/api/client';
import { plan, settings, version, workspace } from '../fixtures';

const preferences = { version: 'scalp-preferences/1', price_unit: 'USD_per_ounce', target_min: '2.00', target_max: '5.00', max_entry_distance: '2.00', max_trigger_distance: '2.00', preferred_holding_minutes: 10, max_holding_minutes: 30 };
const suitable = { state: 'compatible', reasons: [], entry_distance: '1.20', effective_entry_range: { low: '4000.00', high: '4000.20' }, target_space: { low: '3.00', high: '3.20' }, stop_space: { low: '1.00', high: '1.20' }, trigger_distances: [], required_path: '等待新的 M1 闭柱确认，再等待 Ask 回踩到有效入场范围。', execution_side: 'ask', basis_quote_at: '2026-10-15T02:00:00Z' };
let current: Schema['Workspace'];
let candidate: Schema['PlanRecord'];

vi.mock('../../../apps/web/src/api/queries', () => ({
  useSettings: () => ({ data: settings }),
  useWorkspace: () => ({ data: current, isError: false }),
}));
beforeEach(() => {
  candidate = { ...plan, definition: { ...plan.definition, trading_style: 'scalp', entry_deadline: '2026-10-15T02:30:00Z' } };
  Object.assign(candidate, { scalp_preferences: preferences, scalp_suitability: { ...suitable, state: 'incompatible', entry_distance: '5.70', reasons: ['需要先反弹5.7美元，超出本次超短线偏好。'] } });
  current = { ...workspace, as_of: '2026-10-15T02:00:00Z', plans: [candidate],
    analysis_refresh: [{ trading_style: 'scalp', state: 'FRESH', forecast_id: 'f', version_id: 'v', market_cutoff: '2026-10-15T01:45:00Z', fresh_until: '2026-10-15T02:15:00Z', reasons: [], request_kind: 'new', pending_request_id: null }],
    action_advice: [{ plan_id: candidate.plan_id, version_id: 'v', trading_style: 'scalp', state: 'WAITING', side: 'long', title: '超短线 · 已触发，等待回踩区间', reasons: ['触发已确认，等待合法的回踩报价。'], evaluated_at: '2026-10-15T02:00:00Z', market_snapshot_id: 'm', quote_at: '2026-10-15T02:00:00Z', valid_until: null, entry_price: '4001.40', allowed_zone: { low: '4000.00', high: '4000.20' }, stop_loss: '3999.00', take_profit: '4003.20', volume_lots: '0.01', entry_deadline: candidate.definition.entry_deadline, max_holding_minutes: 10, evaluation_id: null }] };
  Object.assign(current.action_advice![0], { scalp_suitability: suitable });
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

function showCard() {
  return render(<QueryClientProvider client={new QueryClient()}><MemoryRouter><PlanCard plan={candidate} /></MemoryRouter></QueryClientProvider>);
}

it('方案卡优先展示当前执行侧距离，导入诊断在原依据中保留', () => {
  showCard();
  expect(screen.getByText('距离入场区间1.20美元')).toBeTruthy();
  const latest = within(screen.getByRole('region', { name: '当前超短线尺度检查' }));
  expect(latest.getByText('目标价差3.00—3.20美元')).toBeTruthy();
  expect((latest.getByText('距离依据与必要路径').closest('details') as HTMLDetailsElement).open).toBe(false);
  expect(latest.getByText(/当前买入侧 Ask/)).toBeTruthy();
  expect(screen.getByText('已触发，等待回踩区间')).toBeTruthy();
  const original = screen.getByText('需要先反弹5.7美元，超出本次超短线偏好。').closest('details')!;
  expect(original.open).toBe(false);
  expect(within(original).getByText('导入时超短线尺度检查')).toBeTruthy();
});

it('未知距离明确显示未知，符合尺度本身不伪装成可以入场', () => {
  Object.assign(current.action_advice![0], { state: 'BLOCKED', title: '超短线 · 等待报价资料', scalp_suitability: { ...suitable, state: 'unknown', entry_distance: null, target_space: null, stop_space: null, effective_entry_range: null, basis_quote_at: null, reasons: ['缺少有效 Ask 报价。'] } });
  render(<MemoryRouter><GuidancePanel workspace={current} /></MemoryRouter>);
  expect(screen.getByText('距离入场区间未知')).toBeTruthy();
  expect(screen.getByText('目标价差未知')).toBeTruthy();
  expect(screen.getByText('资料不足，尺度待核验')).toBeTruthy();
  expect(screen.queryByText(/距离入场区间0/)).toBeNull();
  expect(screen.queryByText('当前可入场', { exact: true })).toBeNull();
});

it('当前尺度合格仍保留闭柱或回踩等待，不显示策略错误或已可入场', () => {
  render(<MemoryRouter><GuidancePanel workspace={current} /></MemoryRouter>);
  expect(screen.getByText('符合本次偏好')).toBeTruthy();
  expect(screen.getByText('等待新的 M1 闭柱确认，再等待 Ask 回踩到有效入场范围。')).toBeTruthy();
  expect(screen.getByText('候选 · 等待条件')).toBeTruthy();
  expect(screen.queryByText('当前可入场', { exact: true })).toBeNull();
});

it('旧方案缺冻结偏好时保留原规则，不追溯宣称符合新标准', () => {
  candidate = { ...plan, definition: { ...plan.definition, trading_style: 'scalp' } };
  current.action_advice = [];
  showCard();
  expect(screen.getByText('该方案未提供冻结的超短线偏好，保留原规则。')).toBeTruthy();
  expect(screen.queryByText('符合本次偏好')).toBeNull();
});

it('超短线不适配不改变日内信息和已有持仓管理', () => {
  Object.assign(current.action_advice![0], { state: 'BLOCKED', title: '超短线 · 距离不适合', scalp_suitability: { ...suitable, state: 'incompatible', reasons: ['目标价差11.69美元，超出本次超短线偏好。'] } });
  current.analysis_refresh!.push({ ...current.analysis_refresh![0], trading_style: 'intraday' });
  current.candidate_assessments = [{ trading_style: 'intraday', outcome: 'no_candidate', plan_key: null, rationale: '日内当前等待区间突破。', missing_conditions: ['日内方向尚待确认。'] }];
  current.position_advice = [{ order: { account_scope_id: 'a', kind: 'position', broker_id: 'ticket' }, plan_id: 'old-plan', state: 'EXIT', reason_code: 'STOP_REACHED', title: '已到原止损条件，建议人工退出', reasons: ['沿用持仓原方案。'], evaluated_at: current.as_of, valid_until: '2026-10-15T02:01:00Z' }];
  render(<MemoryRouter><GuidancePanel workspace={current} /></MemoryRouter>);
  expect(screen.getByText('目标价差11.69美元，超出本次超短线偏好。')).toBeTruthy();
  expect(screen.getByText('日内当前等待区间突破。')).toBeTruthy();
  expect(screen.getByText('沿用持仓原方案。')).toBeTruthy();
});

it('版本详情展示保存的导入诊断而非工作台当前结果', () => {
  render(<AnswerView version={{ ...version, plans: [candidate] }} />);
  expect(screen.getByText('导入时超短线尺度检查')).toBeTruthy();
  expect(screen.getByText('距离入场区间5.70美元')).toBeTruthy();
  expect(screen.queryByText('距离入场区间1.20美元')).toBeNull();
});

it('价格已到闭柱阈值时仍说明条件须核验，价格位移未知不填零', () => {
  Object.assign(current.action_advice![0], { scalp_suitability: { ...suitable, execution_side: 'bid', trigger_distances: [
    { role: 'activation', kind: 'bar_close', price_basis: 'bid', threshold: '4002.00', distance: '0.00', state: 'price_satisfied', note: '尚须等待导入后新的合法 M5 闭柱。' },
    { role: 'entry_check', kind: 'bar_close', price_basis: null, threshold: '4001.00', distance: null, state: 'unknown', note: '缺少该闭柱价格基准。' },
  ] } });
  render(<MemoryRouter><GuidancePanel workspace={current} /></MemoryRouter>);
  expect(screen.getByText(/触发条件 · BID 4002.00：价格已到阈值，条件仍须核验 0.00美元/)).toBeTruthy();
  expect(screen.getByText('尚须等待导入后新的合法 M5 闭柱。')).toBeTruthy();
  expect(screen.getByText('入场检查 · 价格基准未知 4001.00：所需价格位移未知')).toBeTruthy();
  expect(screen.getByText(/当前卖出侧 Bid · 报价时间/)).toBeTruthy();
  expect(screen.queryByText('当前可入场', { exact: true })).toBeNull();
});

it('持仓依据和整个入场范围的净金额缺口按后端原结果展示', () => {
  Object.assign(candidate.definition, { max_holding_minutes: 20, holding_time_basis: '局部 M5 结构需要两个完整回踩阶段。' });
  Object.assign(current.action_advice![0], { state: 'BLOCKED', scalp_suitability: { ...suitable, range_net_check: { state: 'unknown', reasons: ['范围上界缺少完整费用。'], boundary_results: [] } } });
  render(<MemoryRouter><GuidancePanel workspace={current} /></MemoryRouter>);
  expect(screen.getByText('持仓时长依据：局部 M5 结构需要两个完整回踩阶段。')).toBeTruthy();
  expect(screen.getByText('全入场范围金额检查：待补资料。范围上界缺少完整费用。')).toBeTruthy();
  expect(screen.queryByText('当前可入场', { exact: true })).toBeNull();
});

it('持续背景分别显示成立和等待，不伪造距离或要求另等新M5', () => {
  Object.assign(current.action_advice![0], { scalp_suitability: { ...suitable, required_path: '持续背景入场时复核，不锁存。', trigger_distances: [
    { role: 'entry_checks[0]', kind: 'bar_close', price_basis: 'bid', threshold: '3997.90', distance: null, state: 'background_satisfied', note: '最新合法M5闭柱已满足持续背景。' },
    { role: 'entry_checks[1]', kind: 'bar_close', price_basis: 'bid', threshold: '3999.00', distance: null, state: 'background_waiting', note: '背景当前不成立，不允许入场。' },
  ] } });
  render(<MemoryRouter><GuidancePanel workspace={current} /></MemoryRouter>);
  expect(screen.getByText(/持续背景当前成立，入场时复核/)).toBeTruthy();
  expect(screen.getByText(/持续背景尚未成立，继续等待/)).toBeTruthy();
  expect(screen.queryByText(/0.00美元/)).toBeNull();
  expect(screen.queryByText(/等待合法的新M5/)).toBeNull();
  expect(screen.queryByText('当前可入场', { exact: true })).toBeNull();
});
