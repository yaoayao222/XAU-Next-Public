import React from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { AnalysisLabPanel } from '../../../apps/web/src/features/analysis/AnalysisLabPanel';
import { workspace } from '../fixtures';

let client: QueryClient;
let bodies: Record<string, unknown>[];
let urls: string[];
const at = '2026-10-12T02:00:00Z';
const event = { event_id: 'e1', title: '消费者物价指数', country: '美国', currency: 'USD', scheduled_at: at,
  reference_period: '9月', kind: 'data', importance: 5, importance_scale: '金十原始评级', previous: '2.9', forecast: '3.0',
  actual: '3.1', revised: null, unit: '%', actual_available_at: at, source_id: 'jin10-data', status: 'released' };
const bundle = { bundle_id: 'bundle-test', captured_at: at, month: '2026-10', scope: '全部国家和星级', state: 'partial', data_kind: 'SYNTHETIC',
  sources: [{ source_id: 'jin10-data', url: 'https://rili.jin10.com/', captured_at: at, status: 'available', content_sha256: 'a'.repeat(64), message: '合成来源' }],
  events: [event, { ...event, event_id: 'e2', country: '日本', currency: 'JPY', importance: 1, title: '低星级事件' }],
  quotes: [{ symbol: 'DXY', value: '101.75', unit: '指数点', observed_at: '2026-10-09T20:00:00Z', captured_at: at,
    publisher: '合成报价', url: 'https://example.com', state: 'stale', provider_claims_realtime: false, real_time_verified: false, limitation: '实时未认证' }],
  limitations: ['资料缺口仍保留'], jin10_event_count: 2, rated_event_count: 2 };
function record(kind = 'improved', purpose = 'comparison') { return { comparison_id: 'cmp-test', created_at: at, market_cutoff: at,
  package_kind: kind, purpose, state: 'prepared', data_kind: 'SYNTHETIC', source_bundle_id: kind === 'improved' ? 'bundle-test' : null,
  packet_sha256: 'b'.repeat(64), model_calls_sent: 0, request_id: 'lab-request', packet_text: 'SYNTHETIC packet for inspection', message: '尚未发送模型',
  arms: purpose === 'comparison' ? ['gpt-6.1-sol', 'gpt-6-astra', 'deepseek-flash', 'deepseek-v4-pro'].map((model_id, i) => ({ arm: 'ABCD'[i], model_id, reasoning_effort: 'high', state: 'not_sent', packet_sha256: 'b'.repeat(64) })) : [] }; }
let saved: ReturnType<typeof record>[];
beforeEach(() => {
  client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  bodies = []; urls = []; saved = []; sessionStorage.setItem('xau-next-session-token', 'synthetic-lab-token');
  Object.assign(navigator, { clipboard: { writeText: vi.fn().mockResolvedValue(undefined) } });
  vi.stubGlobal('fetch', vi.fn(async (input: Request) => {
    const path = new URL(input.url).pathname; urls.push(`${input.method} ${path}`);
    const json = (data: unknown, status = 200) => new Response(JSON.stringify(data), { status, headers: { 'Content-Type': 'application/json' } });
    if (path === '/v1/analysis/lab') return json({ daily_package: 'manual-analysis/14', improved_package: 'manual-analysis/23',
      paid_execution_enabled: false, authorized_calls_remaining: 0, bundle, comparisons: saved,
      historical: { attempts: 58, qualified_answers: 34, failed: 22, unknown: 2, winner: null, note: 'frozen' }, message: '仅准备' });
    if (path === '/v1/workspace') return json(workspace);
    if (path === '/v1/analysis/lab/comparisons') { const body = await input.json(); bodies.push(body); const result = record(body.package_kind, body.purpose); saved = [result]; return json(result, 201); }
    if (path === '/v1/analysis/lab/comparisons/cmp-test') return json(saved[0]);
    throw new Error(`Unexpected route ${input.method} ${path}`);
  }));
});
afterEach(() => { cleanup(); client.clear(); vi.unstubAllGlobals(); });
function show() { render(<QueryClientProvider client={client}><AnalysisLabPanel /></QueryClientProvider>); }

describe('统一分析与比较准备（合成，不收费）', () => {
  it('默认折叠不额外请求，打开后显示缺口和旧行情，不写成实时', async () => {
    show(); expect(urls).toEqual([]);
    fireEvent.click(screen.getByRole('button', { name: '打开资料与比较' }));
    await screen.findByText('旧值，仅作背景');
    expect(screen.getByText('资料有缺口')).toBeTruthy();
    expect(screen.getByRole('button', { name: '开始收费比较（尚未授权）' }).hasAttribute('disabled')).toBe(true);
    expect(urls.every(url => url.startsWith('GET '))).toBe(true);
  });
  it('同一个界面准备改进包四模型资料并保存查看，没有调用run', async () => {
    show(); fireEvent.click(screen.getByRole('button', { name: '打开资料与比较' }));
    await screen.findByText('旧值，仅作背景');
    fireEvent.click(screen.getByRole('button', { name: '准备四模型同题比较（不收费）' }));
    await screen.findByText('尚未发送模型');
    expect(bodies[0]).toMatchObject({ package_kind: 'improved', purpose: 'comparison', bundle_id: 'bundle-test' });
    expect(await screen.findByText('gpt-6-astra · high · 未发送')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: '复制用于核对' }));
    await waitFor(() => expect(navigator.clipboard.writeText).toHaveBeenCalledWith('SYNTHETIC packet for inspection'));
    expect(urls.some(url => url.endsWith('/run'))).toBe(false);
  });
  it('可选择原14包，筛选星级仅改变显示，不删除资料', async () => {
    show(); fireEvent.click(screen.getByRole('button', { name: '打开资料与比较' }));
    await screen.findByText('低星级事件');
    fireEvent.change(screen.getByLabelText('金十评级筛选'), { target: { value: '5' } });
    expect(screen.queryByText('低星级事件')).toBeNull();
    fireEvent.change(screen.getByLabelText('金十评级筛选'), { target: { value: '0' } });
    expect(screen.getByText('低星级事件')).toBeTruthy();
    fireEvent.change(screen.getByLabelText('本次准备的分析包'), { target: { value: 'current' } });
    fireEvent.click(screen.getByRole('button', { name: '准备所选分析包（不收费）' }));
    await waitFor(() => expect(bodies[0]).toMatchObject({ package_kind: 'current', bundle_id: null, purpose: 'packet' }));
  });
});
