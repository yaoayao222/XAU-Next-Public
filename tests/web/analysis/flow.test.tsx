import React from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { App, queryClient } from '../../../apps/web/src/app/App';
import * as fixtures from '../fixtures';

let imported = false;
let created = false;
let failImport = false;
let importBodies: unknown[] = [];
let headers: Headers[] = [];
function json(value: unknown, status = 200) { return new Response(JSON.stringify(value), { status, headers: { 'Content-Type': 'application/json' } }); }

beforeEach(() => {
  imported = false; created = false; failImport = false; importBodies = []; headers = [];
  window.history.replaceState(null, '', '/analysis'); sessionStorage.setItem('xau-next-session-token', 'test-session'); queryClient.clear();
  vi.stubGlobal('fetch', vi.fn(async (input: Request) => {
    const url = new URL(input.url); headers.push(input.headers);
    if (url.pathname === '/v1/events') return new Response(new ReadableStream({ start(controller) { input.signal.addEventListener('abort', () => { try { controller.close(); } catch { /* closed */ } }); } }), { headers: { 'Content-Type': 'text/event-stream' } });
    if (url.pathname === '/v1/settings') return json(fixtures.settings);
    if (url.pathname === '/v1/model/settings') return json(fixtures.modelSettings);
    if (url.pathname === '/v1/analysis/requests/req-demo-1/model-call') return json({ call: null, version_id: imported ? fixtures.version.version_id : null });
    if (url.pathname === '/v1/workspace') return json({ ...fixtures.workspace, revision: imported ? 2 : 1, current_forecast: imported ? fixtures.forecast : null, plans: imported ? [fixtures.plan] : [], pending_requests: created && !imported ? [fixtures.request] : [] });
    if (url.pathname === '/v1/analysis/requests' && input.method === 'POST') { created = true; return json(fixtures.request, 201); }
    if (url.pathname === '/v1/analysis/requests') return json({ items: created && !imported ? [fixtures.request] : [], next_cursor: null });
    if (url.pathname === '/v1/analysis/requests/req-demo-1') return json({ ...fixtures.request, state: imported ? 'imported' : 'awaiting_answer' });
    if (url.pathname === '/v1/analysis/imports') { importBodies.push(await input.json()); imported = !failImport; return json({ ...fixtures.job, state: 'queued', progress: null, result: null }, 202); }
    if (url.pathname === '/v1/jobs/job-demo') return json(failImport ? { ...fixtures.job, state: 'failed', result: null, error: { type: 'urn:problem', title: '回答字段错误', status: 422, code: 'ANSWER_INVALID', detail: '请修正回答后重新导入。', trace_id: 'trace-fixture', field_errors: [{ path: '/plans/0/stop_loss', code: 'PRICE_ORDER', message: '多头止损须低于入场价' }], retryable: false } } : fixtures.job);
    if (url.pathname === '/v1/forecasts/fc-demo-1') return json({ forecast: fixtures.forecast, current_version: fixtures.version });
    if (url.pathname === '/v1/forecasts/fc-demo-1/versions') return json({ items: [{ version_id: fixtures.version.version_id, forecast_id: fixtures.forecast.forecast_id, sequence: 1, available_at: fixtures.now, summary: fixtures.answer.summary }], next_cursor: null });
    throw new Error(`Unexpected mock route ${input.method} ${url.pathname}`);
  }));
  Object.assign(navigator, { clipboard: { writeText: vi.fn().mockResolvedValue(undefined) } });
});
afterEach(() => { cleanup(); queryClient.clear(); vi.unstubAllGlobals(); });

describe('AC-03/04/05/06/09 前端公开契约流程（合成mock）', () => {
  it('创建请求、复制完整包、导入原文、显示版本，重开从服务端恢复', async () => {
    const view = render(<App />);
    await waitFor(() => expect((screen.getByRole('button', { name: '新建一轮分析' }) as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(screen.getByRole('button', { name: '新建一轮分析' }));
    fireEvent.click(await screen.findByRole('button', { name: '复制完整分析包' }));
    await waitFor(() => expect(navigator.clipboard.writeText).toHaveBeenCalledWith(fixtures.request.packet_text));
    const raw = JSON.stringify(fixtures.answer, null, 2);
    fireEvent.change(screen.getByLabelText('结构化回答原文'), { target: { value: raw } });
    fireEvent.click(screen.getByRole('button', { name: '校验并保存回答' }));
    await screen.findByText(/回答已处理，可查看保存的版本及方案/);
    expect(importBodies[0]).toMatchObject({ request_id: fixtures.request.request_id, raw_text: raw, source: 'manual_paste', provider: null, model_id: null });
    expect(headers.every(header => header.get('Authorization') === 'Bearer test-session')).toBe(true);
    expect(await screen.findByText('回答与版本记录')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: '校验并保存回答' }));
    await waitFor(() => expect(importBodies).toHaveLength(2));
    const importHeaders = headers.filter(header => header.has('Idempotency-Key'));
    expect(importHeaders.at(-1)!.get('Idempotency-Key')).toBe(importHeaders.at(-2)!.get('Idempotency-Key'));
    view.unmount(); queryClient.clear(); window.history.replaceState(null, '', '/'); render(<App />);
    expect(await screen.findByText('未取得实时判断')).toBeTruthy();
    expect(await screen.findByText('持仓情况未知')).toBeTruthy();
    expect(screen.queryByText('该快照没有持仓或挂单')).toBeNull();
  });
  it('后台校验失败展示字段原因并保留用户原文', async () => {
    created = true; failImport = true; window.history.replaceState(null, '', '/analysis?request=req-demo-1'); render(<App />);
    await screen.findByRole('button', { name: '复制完整分析包' });
    fireEvent.change(screen.getByLabelText('结构化回答原文'), { target: { value: '{bad answer}' } }); fireEvent.click(screen.getByRole('button', { name: '校验并保存回答' }));
    const alert = await screen.findByRole('alert');
    expect(within(alert).getByText('多头止损须低于入场价', { exact: false })).toBeTruthy();
    expect((screen.getByLabelText('结构化回答原文') as HTMLTextAreaElement).value).toBe('{bad answer}');
  });
  it('文件导入保存完整原文和manual_file来源', async () => {
    created = true; window.history.replaceState(null, '', '/analysis?request=req-demo-1'); render(<App />);
    await screen.findByRole('button', { name: '复制完整分析包' });
    const raw = JSON.stringify(fixtures.answer, null, 2);
    const file = { name: 'answer.json', size: raw.length, text: () => Promise.resolve(raw) };
    fireEvent.change(screen.getByLabelText('或选择回答文件'), { target: { files: [file] } });
    await waitFor(() => expect((screen.getByLabelText('结构化回答原文') as HTMLTextAreaElement).value).toBe(raw));
    fireEvent.click(screen.getByRole('button', { name: '校验并保存回答' }));
    await waitFor(() => expect(importBodies[0]).toMatchObject({ raw_text: raw, source: 'manual_file' }));
  });
});
