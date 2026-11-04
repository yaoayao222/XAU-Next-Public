import React from 'react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { ComparisonRunPanel } from '../../../apps/web/src/features/analysis/ComparisonRunPanel';
import { api, type Schema } from '../../../apps/web/src/api/client';

let client: QueryClient;
beforeEach(() => { client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } }); });
afterEach(() => { cleanup(); client.clear(); vi.restoreAllMocks(); });
function prepared(kind = 'REAL') {
  return { comparison_id: 'cmp-test', purpose: 'comparison', data_kind: kind, execution: null,
    arms: ['A', 'B', 'C', 'D'].map(arm => ({ arm, state: 'not_sent', model_id: arm })) } as unknown as Schema['AnalysisComparison'];
}
function show(record = prepared()) { render(<QueryClientProvider client={client}><ComparisonRunPanel record={record} /></QueryClientProvider>); }

it('查看真实记录和打开授权表单均不发送；两个同意项必须主动勾选', async () => {
  const start = vi.spyOn(api, 'runComparison').mockResolvedValue({ job_id: 'job-test' } as never);
  const profiles = vi.spyOn(api, 'modelProfiles').mockResolvedValue({ items: [
    { profile_id: 'cpa', name: '本机CPA', base_url: 'http://127.0.0.1:8317/v1' },
    { profile_id: 'deep', name: '官方DeepSeek', base_url: 'https://api.deepseek.com/v1' },
  ] } as never);
  show(); expect(start).not.toHaveBeenCalled(); expect(profiles).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: '设置本组真实比较授权' }));
  await screen.findByRole('option', { name: '官方DeepSeek' });
  fireEvent.change(screen.getByLabelText('Sol与Astra共用的已有CPA配置'), { target: { value: 'cpa' } });
  fireEvent.change(screen.getByLabelText('Flash与Pro共用的已有DeepSeek官方配置'), { target: { value: 'deep' } });
  const submit = screen.getByRole('button', { name: '确认授权并开始本组4次比较（可能收费）' });
  expect(submit.hasAttribute('disabled')).toBe(true);
  const checks = screen.getAllByRole('checkbox');
  expect(checks.every(c => !(c as HTMLInputElement).checked)).toBe(true);
  fireEvent.click(checks[0]); expect(submit.hasAttribute('disabled')).toBe(true);
  fireEvent.click(checks[1]); expect(start).not.toHaveBeenCalled();
  fireEvent.click(submit);
  await waitFor(() => expect(start).toHaveBeenCalledTimes(1));
  expect(start.mock.calls[0][1]).toEqual({ mode: 'real', max_calls: 4, cpa_profile_id: 'cpa', deepseek_profile_id: 'deep',
    acknowledge_possible_charge: true, accept_unknown_cost: true, allow_readonly_history: true, authorization_text: '授权本组最多4次真实模型调用' });
});

it('合成比较只有主动点击才登记，提交内容不含真实授权', async () => {
  const start = vi.spyOn(api, 'runComparison').mockResolvedValue({ job_id: 'job-test' } as never);
  show(prepared('SYNTHETIC')); expect(start).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: '运行模拟比较（不收费）' }));
  await waitFor(() => expect(start).toHaveBeenCalledTimes(1));
  expect(start.mock.calls[0][1]).toEqual({ mode: 'simulation' });
  expect(screen.queryByRole('button', { name: '设置本组真实比较授权' })).toBeNull();
});

it('四份格式合格回答不等于四个方向命中；不明确和未知保留在分母', async () => {
  const record = prepared('SYNTHETIC');
  record.arms = record.arms.map(a => ({ ...a, state: 'succeeded', answer_available: true }));
  record.execution = { mode: 'simulation', state: 'completed', group_call_limit: 4, attempts_reserved: 4,
    real_calls_sent: 0, simulation_calls_sent: 4, message: '合成结果', simulation_advanced: true,
    scores: [30, 60, 180].flatMap(minutes => ['correct', 'incorrect', 'uncertain', 'unknown'].map((direction_class, i) => ({
      arm: 'ABCD'[i], minutes, scope: 'after_common_available', state: 'scored', direction: i === 2 ? 'uncertain' : 'long',
      direction_class, range_boundary_mae: null, range_contained: null, usable_primary: null,
    }))),
  } as Schema['ComparisonExecution'];
  const raw = vi.spyOn(api, 'comparisonAnswer').mockResolvedValue({ arm: 'A', answer_text: '{"SYNTHETIC":true}', business_valid: true } as never);
  show(record);
  expect(screen.getByText('30分钟：明确方向 3/4 · 方向已核验 2/4 · 命中 1/2个可核验判断（全部登记 1/4）')).toBeTruthy();
  expect(screen.getAllByText('判断不明确')).toHaveLength(3);
  expect(screen.getAllByText('资料不足，未知')).toHaveLength(3);
  expect(raw).not.toHaveBeenCalled(); fireEvent.click(screen.getByRole('button', { name: '查看A回答' }));
  expect(await screen.findByText('{"SYNTHETIC":true}')).toBeTruthy();
});
