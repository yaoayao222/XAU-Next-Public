import React from 'react';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { MemoryRouter } from 'react-router-dom';
import { ActionSummary, GuidancePanel } from '../../../apps/web/src/features/workspace/GuidancePanel';
import type { Schema } from '../../../apps/web/src/api/client';
import { plan, workspace } from '../fixtures';

afterEach(() => { cleanup(); vi.useRealTimers(); });

function snapshot(): Schema['Workspace'] {
  return { ...workspace, as_of: '2026-10-12T02:00:00Z', analysis_refresh: [{ trading_style: 'scalp', state: 'DUE',
    forecast_id: 'f', version_id: 'v', market_cutoff: '2026-10-12T01:31:00Z', remind_at: '2026-10-12T02:00:00Z',
    fresh_until: '2026-10-12T02:01:00Z', reasons: ['准备下一份分析'], request_kind: 'new', pending_request_id: 'packet-new' }],
    action_advice: [{ plan_id: 'p', version_id: 'v', trading_style: 'scalp', state: 'READY', side: 'long',
      title: '超短线 · 当前可以买入', reasons: ['全部明确条件已满足'], evaluated_at: '2026-10-12T02:00:00Z',
      market_snapshot_id: 'm', quote_at: '2026-10-12T02:00:00Z', valid_until: '2026-10-12T02:00:05Z',
      entry_price: '100', allowed_zone: { low: '99.9', high: '100.1' }, stop_loss: '99', take_profit: '103',
      volume_lots: '0.01', entry_deadline: '2026-10-12T02:30:00Z', max_holding_minutes: 20, evaluation_id: 'e' }],
    notifications: [{ notification_id: 'n', kind: 'entry', resource_id: 'p', title: '历史买入通知', body: '合成历史内容',
      created_at: '2026-10-12T01:59:00Z', expires_at: '2026-10-12T01:59:05Z', state: 'failed', submitted_at: null, detail: 'Windows 通知已禁用' }],
  };
}

describe('当前行动与历史通知分开', () => {
  it('行动摘要前置显示当前状态和下一步', () => {
    render(<MemoryRouter><ActionSummary workspace={snapshot()} /></MemoryRouter>);
    expect(screen.getByRole('region', { name: '当前行动摘要' })).toBeTruthy();
    expect(screen.getByText('当前可入场')).toBeTruthy();
    expect(screen.getByText('下一步：在 MT5 确认后手工执行，同时准备下一份分析')).toBeTruthy();
    expect(screen.getByText(/不会自动下单/)).toBeTruthy();
  });

  it('没有行情时提供立即刷新入口', () => {
    const value = { ...snapshot(), analysis_refresh: undefined, action_advice: [] };
    value.market = null;
    const refresh = vi.fn();
    render(<MemoryRouter><ActionSummary workspace={value} onRefresh={refresh} /></MemoryRouter>);
    fireEvent.click(screen.getByRole('button', { name: '立即刷新事实' }));
    expect(refresh).toHaveBeenCalledOnce();
  });

  it('已有行情但没有分析时引导新建分析', () => {
    const value = { ...snapshot(), analysis_refresh: undefined, action_advice: [] };
    render(<MemoryRouter><ActionSummary workspace={value} /></MemoryRouter>);
    expect(screen.getByText('还没有分析')).toBeTruthy();
    expect(screen.getByRole('link', { name: '新建一轮分析' }).getAttribute('href')).toBe('/analysis?mode=manual');
  });

  it('离线和阻断状态不伪装成尚无分析', () => {
    const offline = snapshot();
    render(<MemoryRouter><ActionSummary workspace={offline} offline /></MemoryRouter>);
    expect(screen.getByText('报价待更新，暂停入场提示')).toBeTruthy();
    cleanup();
    const blocked = snapshot();
    Object.assign(blocked.action_advice![0], { state: 'BLOCKED', title: '超短线 · 当前不可入场', reasons: ['费用资料不足。'] });
    render(<MemoryRouter><ActionSummary workspace={blocked} /></MemoryRouter>);
    expect(screen.getByText('当前不可入场')).toBeTruthy();
    expect(screen.queryByText('尚无分析')).toBeNull();
  });

  it('分别展示两阶段失效和导入后窗口，倒计时不重算旧行情时效', () => {
    vi.useFakeTimers();
    const value = snapshot();
    Object.assign(value.action_advice![0], { state: 'WAITING', title: '等待回踩', valid_until: null,
      entry_timing: { policy_version: 'entry-observation/1', enforced: true, state: 'SUFFICIENT',
        market_cutoff: '2026-10-12T01:40:00Z', available_at: '2026-10-12T01:47:17Z', effective_from: '2026-10-12T01:47:17Z',
        analysis_fresh_until: '2026-10-12T02:10:00Z', effective_entry_deadline: '2026-10-12T02:01:00Z', minimum_entry_deadline: '2026-10-12T02:00:00Z',
        import_delay_seconds: 437, remaining_at_import_seconds: 823, activation_closes_at_import: 3,
        required_activation_closes: 2, post_trigger_reserve_seconds: 300, reasons: [] } });
    value.plans = [{ ...plan, plan_id: 'p', definition: { ...plan.definition,
      post_activation_invalidation: { kind: 'quote_compare', quote_side: 'bid', operator: 'lt', price: '99.95' } } }];
    render(<MemoryRouter><GuidancePanel workspace={value} /></MemoryRouter>);
    expect(screen.getByText(/激活后结构失效：.*99.95/)).toBeTruthy();
    expect(screen.getByText('资料截止至导入共 7分17秒 · 导入时可等 13分43秒')).toBeTruthy();
    expect(screen.getByText(/最多还能等待入场 1分0秒/)).toBeTruthy();
    act(() => { vi.advanceTimersByTime(6000); });
    expect(screen.getByText(/最多还能等待入场 0分54秒/)).toBeTruthy();
    expect(screen.getByText(/退出截止/)).toBeTruthy();
  });
  it('无合格候选时分别显示两个模式的具体缺口', () => {
    const value = snapshot();
    value.action_advice = [];
    value.analysis_refresh!.push({ ...value.analysis_refresh![0], trading_style: 'intraday' });
    value.candidate_assessments = [
      { trading_style: 'scalp', outcome: 'no_candidate', plan_key: null, rationale: '短线结构尚不完整', missing_conditions: ['短线缺少可定义的结构止损。'] },
      { trading_style: 'intraday', outcome: 'no_candidate', plan_key: null, rationale: '日内空间依据不足', missing_conditions: ['日内缺少有依据的目标空间。'] },
    ];
    render(<MemoryRouter><GuidancePanel workspace={value} /></MemoryRouter>);
    expect(screen.getAllByRole('status').map(item => item.textContent)).toEqual(['暂无合格候选，继续观察', '暂无合格候选，继续观察']);
    expect(screen.getByText('短线缺少可定义的结构止损。')).toBeTruthy();
    expect(screen.getByText('日内缺少有依据的目标空间。')).toBeTruthy();
    expect(screen.queryByText('当前可入场', { exact: true })).toBeNull();
  });
  it('等待中的候选与当前可入场明确区分，展示实际触发条件', () => {
    const value = snapshot();
    Object.assign(value.action_advice![0], { state: 'WAITING', title: '超短线 · 等待入场条件', reasons: ['结构化触发条件尚未确认。'] });
    value.plans = [{ ...plan, plan_id: 'p', definition: { ...plan.definition,
      activation: { kind: 'quote_compare', quote_side: 'bid', operator: 'gte', price: '101' } } }];
    const view = render(<MemoryRouter><GuidancePanel workspace={value} /></MemoryRouter>);
    expect(screen.getByText('候选 · 等待条件')).toBeTruthy();
    expect(screen.getByText(/触发条件：.*101/)).toBeTruthy();
    expect(screen.getByText('等待触发及触发后全程检查。')).toBeTruthy();
    expect(view.container.querySelector('.guidance-card.ready')).toBeNull();
    expect(screen.queryByText('当前可入场', { exact: true })).toBeNull();
  });
  it('资料缺失时等待行情恢复，不让用户重复取得分析', () => {
    const value = snapshot();
    value.action_advice = [];
    Object.assign(value.analysis_refresh![0], { state: 'WAITING_DATA', reasons: ['M5：最近闭柱缺失'], pending_request_id: null });
    render(<MemoryRouter><GuidancePanel workspace={value} /></MemoryRouter>);
    expect(screen.getByText('等待行情资料')).toBeTruthy();
    expect(screen.getByText('系统会随每次事实读取重新核验，无需重复导入。')).toBeTruthy();
    expect(screen.queryByRole('link', { name: '取得新的分析' })).toBeNull();
    expect(screen.queryByRole('link', { name: '打开已准备的分析包' })).toBeNull();
    expect(screen.getByText('此回答未提供该周期的独立候选说明；请查看原文。系统不补写历史原因。')).toBeTruthy();
  });
  it('摘要按 DUE 和 USED 的真实下一步区分准备新分析与继续当前候选', () => {
    const dueWithoutAdvice = snapshot();
    dueWithoutAdvice.action_advice = [];
    render(<MemoryRouter><ActionSummary workspace={dueWithoutAdvice} /></MemoryRouter>);
    expect(screen.getByText('当前分析仍有效 · 建议准备下一份')).toBeTruthy();
    expect(screen.getByText('下一步：准备下一份分析')).toBeTruthy();
    cleanup();

    const dueWaiting = snapshot();
    Object.assign(dueWaiting.action_advice![0], { state: 'WAITING', title: '超短线 · 等待入场条件' });
    render(<MemoryRouter><ActionSummary workspace={dueWaiting} /></MemoryRouter>);
    expect(screen.getByText('下一步：继续等待条件，同时准备下一份分析')).toBeTruthy();
    cleanup();

    const dueReady = snapshot();
    Object.assign(dueReady.analysis_refresh![0], { state: 'DUE' });
    render(<MemoryRouter><ActionSummary workspace={dueReady} /></MemoryRouter>);
    expect(screen.getByText('下一步：在 MT5 确认后手工执行，同时准备下一份分析')).toBeTruthy();
    cleanup();

    const used = snapshot();
    Object.assign(used.action_advice![0], { state: 'USED', title: '超短线 · 方案已使用，不再入场' });
    render(<MemoryRouter><ActionSummary workspace={used} /></MemoryRouter>);
    expect(screen.getByText('下一步：新建一轮分析')).toBeTruthy();
    expect(screen.queryByText('打开已准备的分析包 →')).toBeNull();
  });
  it('离线等待态明确标记判断待更新，不承诺离线时自动核验', () => {
    const value = snapshot();
    Object.assign(value.action_advice![0], { state: 'WAITING', valid_until: null });
    render(<MemoryRouter><ActionSummary workspace={value} offline /><GuidancePanel workspace={value} offline /></MemoryRouter>);
    expect(screen.getAllByText('离线 · 判断待更新').length).toBeGreaterThanOrEqual(1);
    expect(screen.queryByText(/随事实读取核验/)).toBeNull();
    expect(screen.getAllByText(/恢复本地服务后重新核验/).length).toBeGreaterThanOrEqual(1);
  });
  it('只有判断没有刷新记录时保留判断并提示刷新事实核验', () => {
    const value = snapshot();
    value.analysis_refresh = undefined;
    render(<MemoryRouter><ActionSummary workspace={value} /></MemoryRouter>);
    expect(screen.getByText('已有判断，当前资料状态待核验')).toBeTruthy();
    expect(screen.getByText('下一步：刷新事实后重新核验')).toBeTruthy();
    expect(screen.queryByText('还没有分析')).toBeNull();
  });
  it('方案已使用后保留终止入场显示，重绘和离线均不恢复历史买入通知', () => {
    vi.useFakeTimers();
    const value = snapshot();
    Object.assign(value.action_advice![0], { state: 'USED', title: '超短线 · 方案已使用，不再入场',
      reasons: ['已确认成交，平仓及重启不恢复入场资格。'], valid_until: null });
    const view = render(<MemoryRouter><GuidancePanel workspace={value} /></MemoryRouter>);
    act(() => { vi.advanceTimersByTime(6000); });
    view.rerender(<MemoryRouter><GuidancePanel workspace={value} offline /></MemoryRouter>);
    expect(screen.getByRole('status').textContent).toBe('超短线 · 方案已使用，不再入场');
    expect(screen.queryByText('超短线 · 当前可以买入')).toBeNull();
    expect(view.container.querySelector('.guidance-card.ready')).toBeNull();
    expect(screen.getByText('历史买入通知')).toBeTruthy();
  });
  it('显示后端结论、价格范围和已准备的更新包', () => {
    render(<MemoryRouter><GuidancePanel workspace={snapshot()} /></MemoryRouter>);
    expect(screen.getByRole('status').textContent).toContain('当前可以买入');
    expect(screen.getByText('允许入场范围：99.9—100.1 · 手数：0.01')).toBeTruthy();
    expect(screen.getByRole('link', { name: '准备下一份分析' }).getAttribute('href')).toBe('/analysis?request=packet-new');
    fireEvent.click(screen.getByText('最近通知与投递状态（1）'));
    expect(screen.getByText('Windows 通知已禁用')).toBeTruthy();
    expect(screen.getByText('历史通知，已过有效期', { exact: false })).toBeTruthy();
  });
  it('后台或连接不再更新时，已到期的可入场显示自动撤销', () => {
    vi.useFakeTimers();
    render(<MemoryRouter><GuidancePanel workspace={snapshot()} /></MemoryRouter>);
    act(() => { vi.advanceTimersByTime(6000); });
    expect(screen.getByRole('status').textContent).toBe('报价待更新，暂停入场提示');
    expect(screen.queryByText('超短线 · 当前可以买入')).toBeNull();
  });
  it('正常等待时不催促重新分析，DUE只显示准备下一份', () => {
    const value = snapshot();
    Object.assign(value.action_advice![0], { state: 'WAITING', title: '触发已确认，等待回踩' });
    Object.assign(value.analysis_refresh![0], { state: 'FRESH', blocks_entry: false });
    const view = render(<MemoryRouter><GuidancePanel workspace={value} /></MemoryRouter>);
    expect(screen.getByText('候选继续按原条件等待，无需仅因尚未触发或回踩而重新分析。')).toBeTruthy();
    expect(screen.queryByRole('link', { name: '取得新的分析' })).toBeNull();
    Object.assign(value.analysis_refresh![0], { state: 'DUE' });
    view.rerender(<MemoryRouter><GuidancePanel workspace={value} /></MemoryRouter>);
    expect(screen.getByRole('link', { name: '准备下一份分析' })).toBeTruthy();
    expect(screen.getByRole('status').textContent).toBe('触发已确认，等待回踩');
    expect(view.container.querySelector('.guidance-card.attention')).toBeNull();
  });
  it('后端暂未刷新时也按原截止停止等待并显示准确时间', () => {
    vi.useFakeTimers();
    const value = snapshot();
    Object.assign(value.action_advice![0], { state: 'WAITING', title: '等待条件', entry_deadline: '2026-10-12T02:00:03Z', valid_until: null });
    render(<MemoryRouter><GuidancePanel workspace={value} zone="Asia/Shanghai" /></MemoryRouter>);
    act(() => { vi.advanceTimersByTime(4000); });
    expect(screen.getByRole('status').textContent).toBe('候选入场期限已到');
    expect(screen.getByText('停止依据：候选入场到期')).toBeTruthy();
    expect(screen.getByText(/依据时间：.*10:00:03/)).toBeTruthy();
    expect(screen.queryByText('候选 · 等待条件')).toBeNull();
  });
  it('离线立即撤销可入场显示', () => {
    render(<MemoryRouter><GuidancePanel workspace={snapshot()} offline /></MemoryRouter>);
    expect(screen.getByRole('status').textContent).toBe('报价待更新，暂停入场提示');
  });
  it('持仓退出建议在资料过期后也不继续显示为当前指令', () => {
    vi.useFakeTimers();
    const value = snapshot();
    value.position_advice = [{ order: { account_scope_id: 'a', kind: 'position', broker_id: 'ticket' }, plan_id: 'p',
      state: 'EXIT', reason_code: 'STOP_REACHED', title: '已到原止损条件，建议人工退出', reasons: ['合成已关联持仓'],
      evaluated_at: value.as_of, valid_until: '2026-10-12T02:00:05Z' }];
    render(<MemoryRouter><GuidancePanel workspace={value} /></MemoryRouter>);
    expect(screen.getByText('已到原止损条件，建议人工退出')).toBeTruthy();
    act(() => { vi.advanceTimersByTime(6000); });
    expect(screen.queryByText('已到原止损条件，建议人工退出')).toBeNull();
    expect(screen.getByText('持仓管理资料已过时，等待重新核验')).toBeTruthy();
  });
});
