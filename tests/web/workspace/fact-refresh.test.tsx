import React from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { api, type Schema } from '../../../apps/web/src/api/client';
import { WorkspacePage } from '../../../apps/web/src/features/workspace/WorkspacePage';
import * as fixtures from '../fixtures';

const caches: QueryClient[] = [];
afterEach(() => { cleanup(); caches.forEach(cache => cache.clear()); caches.length = 0; vi.restoreAllMocks(); });

function refreshJob(state: Schema['Job']['state'], revision = 2): Schema['Job'] {
  return {
    ...fixtures.job,
    job_id: 'automatic-refresh-job',
    kind: 'market_refresh',
    state,
    revision,
    message: state === 'failed' ? '终端读取未完成' : '只读事实刷新',
    progress: state === 'succeeded' ? 1 : null,
    result: state === 'succeeded' ? { kind: 'market_snapshot', id: fixtures.market.snapshot_id } : null,
    error: state === 'failed' ? {
      type: 'urn:xau-next:problem:bridge-timeout', title: '终端暂不可读取', status: 503,
      code: 'BRIDGE_TIMEOUT', detail: 'MT5 读取超时，请检查终端连接。', trace_id: 'fixture-refresh-error',
      field_errors: [], retryable: true,
    } : null,
  };
}

function snapshot(job: Schema['Job'] | null): Schema['Workspace'] {
  return {
    ...fixtures.workspace,
    market: { ...fixtures.market, source_kind: 'mt5' },
    portfolio: {
      ...fixtures.portfolio,
      orders_status: fixtures.source,
      orders: [{
        order: { account_scope_id: fixtures.portfolio.account_scope_id, kind: 'position', broker_id: 'position-fixture' },
        symbol: 'XAUUSD', side: 'long', volume_lots: '0.02', entry_price: '99.50',
        stop_loss: '98.50', take_profit: '103.50', event_time: fixtures.now, captured_at: fixtures.now, plan_id: null,
      }],
    },
    fact_refresh: { enabled: true, interval_seconds: 5, retry_seconds: 30, latest_job: job },
    active_jobs: job && ['queued', 'running', 'cancelling'].includes(job.state) ? [job] : [],
  };
}

function mount(initial: Schema['Workspace'], jobs: Record<string, Schema['Job']> = {}) {
  let current = initial;
  const cache = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } });
  caches.push(cache);
  vi.spyOn(api, 'workspace').mockImplementation(async () => current);
  vi.spyOn(api, 'settings').mockResolvedValue(fixtures.settings);
  const getJob = vi.spyOn(api, 'job').mockImplementation(async id => {
    if (!jobs[id]) throw new Error(`Unexpected fixture job ${id}`);
    return jobs[id];
  });
  render(<QueryClientProvider client={cache}><MemoryRouter><WorkspacePage /></MemoryRouter></QueryClientProvider>);
  return {
    getJob,
    update(next: Schema['Workspace']) {
      current = { ...next, revision: current.revision + 1, event_cursor: current.event_cursor + 1 };
      act(() => { cache.setQueryData(['workspace'], current); });
    },
  };
}

function bidValue() {
  return within(screen.getByText('BID · 卖出侧').closest('.metric') as HTMLElement).getByText('99.98');
}

describe('自动读取状态：契约 mock 组件交互', () => {
  it('独立显示读取状态与间隔，后台进行时仍能手动刷新且不额外轮询自动job', async () => {
    const running = refreshJob('running');
    const { getJob } = mount(snapshot(running));
    const status = await screen.findByRole('region', { name: '事实自动读取状态' });
    expect(within(status).getByText('自动刷新事实已开启')).toBeTruthy();
    expect(within(status).getByText('MT5 只读采集')).toBeTruthy();
    expect(within(status).getByText('正在读取')).toBeTruthy();
    expect(within(status).getByText(/成功后每 5 秒读取一次/)).toBeTruthy();
    expect(screen.queryByText('状态同步中')).toBeNull();
    expect((screen.getByRole('button', { name: '刷新事实' }) as HTMLButtonElement).disabled).toBe(false);
    expect(bidValue()).toBeTruthy();
    expect(getJob).not.toHaveBeenCalled();
  });

  it('失败说明原因与30秒重试，保留行情、持仓和正在编辑的人工备注', async () => {
    const { update } = mount(snapshot(refreshJob('succeeded')));
    await screen.findByText('上次读取已完成');
    const priorBid = bidValue();
    fireEvent.click(screen.getByText('记录人工决定与方案关联'));
    const note = screen.getByLabelText('决定说明') as HTMLInputElement;
    fireEvent.change(note, { target: { value: '我正在核对该持仓' } });
    update(snapshot(refreshJob('running', 3)));
    await screen.findByText('正在读取');
    expect(bidValue()).toBe(priorBid);
    update(snapshot(refreshJob('failed', 4)));
    const alert = await screen.findByRole('alert');
    expect(within(alert).getByText('MT5 读取超时，请检查终端连接。', { exact: false })).toBeTruthy();
    expect(within(alert).getByText(/失败后 30 秒会再次尝试/)).toBeTruthy();
    expect(within(alert).getByText(/已保留上次读取的事实/)).toBeTruthy();
    expect(bidValue()).toBe(priorBid);
    expect(screen.getByText('当前持仓')).toBeTruthy();
    expect(note.value).toBe('我正在核对该持仓');
    expect(screen.queryByText('还没有行情快照')).toBeNull();
  });

  it('手动返回同一个刷新任务只集中显示一次，并接受服务端较新的终态', async () => {
    const running = refreshJob('running', 2);
    const manual = vi.spyOn(api, 'refreshMarket').mockResolvedValue({ ...running, state: 'queued', revision: 1 });
    const { update, getJob } = mount(snapshot(running));
    await screen.findByText('正在读取');
    fireEvent.click(screen.getByRole('button', { name: '刷新事实' }));
    await waitFor(() => expect(manual).toHaveBeenCalledOnce());
    await waitFor(() => expect((screen.getByRole('button', { name: '刷新事实' }) as HTMLButtonElement).disabled).toBe(false));
    expect(screen.getAllByRole('region', { name: '事实自动读取状态' })).toHaveLength(1);
    expect(screen.getAllByText('正在读取')).toHaveLength(1);
    expect(screen.queryByText('取消任务')).toBeNull();
    expect(getJob).not.toHaveBeenCalled();
    update(snapshot(refreshJob('succeeded', 3)));
    await screen.findByText('上次读取已完成');
    expect(screen.queryByText('等待读取')).toBeNull();
    expect(screen.queryByText('正在读取')).toBeNull();
  });

  it('其它后台任务仍独立呈现，合成来源不显示为MT5', async () => {
    const reading = snapshot(refreshJob('running'));
    reading.market = fixtures.market;
    const importJob: Schema['Job'] = { ...fixtures.job, state: 'running', message: '正在校验合成回答', progress: null };
    reading.active_jobs.push(importJob);
    mount(reading, { [importJob.job_id]: importJob });
    expect(await screen.findByText('合成样例采集')).toBeTruthy();
    expect(await screen.findByText(/正在校验合成回答/)).toBeTruthy();
    expect(screen.queryByText('MT5 只读采集')).toBeNull();
    expect(screen.getAllByText('取消任务')).toHaveLength(1);
  });

  it('worker未开启或旧契约未给状态时，不承诺自动刷新', async () => {
    const stopped = snapshot(null);
    stopped.fact_refresh!.enabled = false;
    const { update } = mount(stopped);
    expect(await screen.findByText('自动刷新事实未开启')).toBeTruthy();
    expect(screen.queryByText(/成功后每 5 秒/)).toBeNull();
    const older = { ...stopped };
    delete older.fact_refresh;
    update(older);
    await waitFor(() => expect(screen.queryByRole('region', { name: '事实自动读取状态' })).toBeNull());
    expect((screen.getByRole('button', { name: '刷新事实' }) as HTMLButtonElement).disabled).toBe(false);
  });
});
