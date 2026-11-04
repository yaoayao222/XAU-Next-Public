import React from 'react';
import { afterEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { PortfolioPanel } from '../../../apps/web/src/features/portfolio/PortfolioPanel';
import { api, ApiProblem, type Schema } from '../../../apps/web/src/api/client';
import { plan, portfolio, source } from '../fixtures';

afterEach(cleanup);
const fact: Schema['OrderFact'] = { order: { account_scope_id: 'account-demo', kind: 'position', broker_id: 'synthetic-position' },
  symbol: 'XAUUSD', side: 'long', volume_lots: '0.01', entry_price: '100', stop_loss: '98', take_profit: '103',
  event_time: source.observed_at!, captured_at: source.captured_at, plan_id: null, association_token: 'a'.repeat(64) };
const state = (item = fact, revision = 1): Schema['Portfolio'] => ({ ...portfolio, orders_status: source, orders: [item], revision });
function mount() {
  const cache = new QueryClient({ defaultOptions: { mutations: { retry: false }, queries: { retry: false } } });
  const ui = (value: Schema['Portfolio']) => <QueryClientProvider client={cache}><PortfolioPanel portfolio={value} plans={[plan]} /></QueryClientProvider>;
  const view = render(ui(state()));
  fireEvent.change(screen.getByLabelText('关联方案'), { target: { value: plan.plan_id } });
  fireEvent.change(screen.getByLabelText('关联或撤销依据'), { target: { value: 'SYNTHETIC 我的依据' } });
  return { ...view, update: (value: Schema['Portfolio']) => view.rerender(ui(value)) };
}

it('背景刷新不打断保存，提交编辑起点令牌并明确显示成功', async () => {
  const save = vi.spyOn(api, 'associate').mockResolvedValue({ association_id: 'synth-link', created_at: source.captured_at,
    input: {} as Schema['AssociationCreate'] });
  const view = mount();
  view.update(state({ ...fact, captured_at: '2026-10-12T02:00:10Z' }, 9));
  fireEvent.click(screen.getByRole('button', { name: '保存关联' }));
  await waitFor(() => expect(save).toHaveBeenCalledTimes(1));
  expect(save.mock.calls[0][0]).toMatchObject({ expected_order_token: 'a'.repeat(64), expected_portfolio_revision: 1 });
  await screen.findByText('方案关联已保存，未执行任何交易。');
});

it('关键资料变化保留草稿并阻止保存，明确核对后才使用新令牌', async () => {
  const save = vi.spyOn(api, 'associate').mockResolvedValue({ association_id: 'synth-link', created_at: source.captured_at,
    input: {} as Schema['AssociationCreate'] });
  const view = mount();
  view.update(state({ ...fact, stop_loss: '99', association_token: 'b'.repeat(64) }, 2));
  expect((screen.getByLabelText('关联或撤销依据') as HTMLInputElement).value).toBe('SYNTHETIC 我的依据');
  expect((screen.getByLabelText('关联方案') as HTMLSelectElement).value).toBe(plan.plan_id);
  expect((screen.getByRole('button', { name: '保存关联' }) as HTMLButtonElement).disabled).toBe(true);
  expect(save).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: '已核对，使用最新资料' }));
  expect(save).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: '保存关联' }));
  await waitFor(() => expect(save).toHaveBeenCalledTimes(1));
  expect(save.mock.calls[0][0].expected_order_token).toBe('b'.repeat(64));
});

it('服务器冲突不自动补发，保留填写内容', async () => {
  const save = vi.spyOn(api, 'associate').mockRejectedValue(new ApiProblem({ type: 'about:blank', title: '冲突',
    status: 409, code: 'VERSION_CONFLICT', detail: 'SYNTHETIC 关键资料已变化', trace_id: 'synth', field_errors: [], retryable: false }));
  mount();
  fireEvent.click(screen.getByRole('button', { name: '保存关联' }));
  await screen.findByText('SYNTHETIC 关键资料已变化');
  expect(save).toHaveBeenCalledTimes(1);
  expect((screen.getByLabelText('关联或撤销依据') as HTMLInputElement).value).toBe('SYNTHETIC 我的依据');
});

it('同券商编号切换账户不沿用另一账户草稿', () => {
  const view = mount();
  view.update({ ...state({ ...fact, order: { ...fact.order, account_scope_id: 'other-account' } }), account_scope_id: 'other-account' });
  expect((screen.getByLabelText('关联或撤销依据') as HTMLInputElement).value).toBe('');
  expect((screen.getByLabelText('关联方案') as HTMLSelectElement).value).toBe('');
});

it('未开始编辑时展示最新关联，不能用旧下拉值覆盖新关联', () => {
  const cache = new QueryClient();
  const ui = (value: Schema['Portfolio']) => <QueryClientProvider client={cache}><PortfolioPanel portfolio={value} plans={[plan]} /></QueryClientProvider>;
  const view = render(ui(state()));
  view.rerender(ui(state({ ...fact, plan_id: plan.plan_id, association_token: 'b'.repeat(64) }, 2)));
  expect((screen.getByLabelText('关联方案') as HTMLSelectElement).value).toBe(plan.plan_id);
  fireEvent.change(screen.getByLabelText('关联或撤销依据'), { target: { value: 'SYNTHETIC 采用可见关联' } });
  expect((screen.getByLabelText('关联方案') as HTMLSelectElement).value).toBe(plan.plan_id);
});
