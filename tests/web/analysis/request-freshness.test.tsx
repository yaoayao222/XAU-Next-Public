import React from 'react';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { App, queryClient } from '../../../apps/web/src/app/App';
import { api, type Schema } from '../../../apps/web/src/api/client';
import { subscribeEvents } from '../../../apps/web/src/api/events';
import { market, modelSettings, request, settings, workspace } from '../fixtures';

vi.mock('../../../apps/web/src/api/events', () => ({ subscribeEvents: vi.fn(() => () => undefined) }));
const freshMarket = { ...market, snapshot_id: 'fresh-market', captured_at: '2026-10-12T04:22:00Z', market_cutoff: '2026-10-12T04:22:00Z' };
const freshWorkspace = { ...workspace, market: freshMarket, revision: 20, event_cursor: 20, as_of: freshMarket.captured_at };
const freshRequest = { ...request, market_snapshot_id: freshMarket.snapshot_id, market_cutoff: freshMarket.market_cutoff };

beforeEach(() => {
  queryClient.clear(); window.history.replaceState(null, '', '/analysis'); sessionStorage.setItem('xau-next-session-token', 'SYNTHETIC');
  vi.spyOn(api, 'workspace').mockResolvedValue(workspace);
  vi.spyOn(api, 'settings').mockResolvedValue(settings);
  vi.spyOn(api, 'modelSettings').mockResolvedValue({ ...modelSettings, key_configured: true, capability_verified: true, values: { ...modelSettings.values, enabled: true } });
  vi.spyOn(api, 'requests').mockResolvedValue({ items: [], next_cursor: null });
  vi.spyOn(api, 'request').mockResolvedValue(freshRequest);
  vi.spyOn(api, 'createRequest').mockResolvedValue(freshRequest);
  vi.spyOn(api, 'requestModelCall').mockResolvedValue({ call: null, version_id: null });
  vi.spyOn(api, 'startModelAnalysis').mockResolvedValue({ call: null, version_id: null });
});
afterEach(() => { cleanup(); queryClient.clear(); vi.restoreAllMocks(); vi.mocked(subscribeEvents).mockClear(); });

it.each(['新建一轮分析', '用 API 生成新分析'])('%s 在页面快照落后两小时时仍先读取服务端最新快照', async name => {
  render(<App />);
  const button = await screen.findByRole('button', { name, exact: true });
  await waitFor(() => expect((button as HTMLButtonElement).disabled).toBe(false));
  vi.mocked(api.workspace).mockResolvedValue(freshWorkspace);
  fireEvent.click(button);
  await waitFor(() => expect(api.createRequest).toHaveBeenCalledOnce());
  expect(vi.mocked(api.createRequest).mock.calls[0][0].market_snapshot_id).toBe(freshMarket.snapshot_id);
  expect(vi.mocked(api.createRequest).mock.calls[0][0]).not.toHaveProperty('horizon_minutes');
  expect(vi.mocked(api.workspace).mock.invocationCallOrder[1]).toBeLessThan(vi.mocked(api.createRequest).mock.invocationCallOrder[0]);
  if (name === '用 API 生成新分析') await waitFor(() => expect(api.startModelAnalysis).toHaveBeenCalledOnce());
  else expect(api.startModelAnalysis).not.toHaveBeenCalled();
});

it('读取最新事实失败时不退回旧快照，也不创建请求或收费调用', async () => {
  render(<App />);
  const button = await screen.findByRole('button', { name: '用 API 生成新分析', exact: true });
  await waitFor(() => expect((button as HTMLButtonElement).disabled).toBe(false));
  vi.mocked(api.workspace).mockRejectedValue(new Error('SYNTHETIC 最新事实读取失败'));
  fireEvent.click(button);
  expect(await screen.findByText('SYNTHETIC 最新事实读取失败')).toBeTruthy();
  expect(api.createRequest).not.toHaveBeenCalled();
  expect(api.startModelAnalysis).not.toHaveBeenCalled();
});

it('API 与手工创建后都保留对应路径并写入 URL mode', async () => {
  render(<App />);
  const apiButton = await screen.findByRole('button', { name: '用 API 生成新分析', exact: true });
  await waitFor(() => expect((apiButton as HTMLButtonElement).disabled).toBe(false));
  fireEvent.click(apiButton);
  await screen.findByText('当前路径：API 自动生成');
  expect(window.location.search).toContain('mode=api');

  cleanup();
  queryClient.clear();
  window.history.replaceState(null, '', '/analysis');
  render(<App />);
  const manualButton = await screen.findByRole('button', { name: '新建一轮分析', exact: true });
  await waitFor(() => expect((manualButton as HTMLButtonElement).disabled).toBe(false));
  fireEvent.click(manualButton);
  await screen.findByText('当前路径：手工复制 / 导入');
  expect(window.location.search).toContain('mode=manual');
  expect(api.startModelAnalysis).toHaveBeenCalledOnce();
});

it('连续状态通知合并刷新，不反复取消正在读取的新快照', async () => {
  render(<App />);
  await waitFor(() => expect(vi.mocked(subscribeEvents).mock.calls.length).toBeGreaterThan(0));
  const onChange = vi.mocked(subscribeEvents).mock.calls.at(-1)![1];
  let finish!: (value: Schema['Workspace']) => void;
  const pending = new Promise<Schema['Workspace']>(resolve => { finish = resolve; });
  vi.mocked(api.workspace).mockReturnValue(pending);
  const event = (id: number) => ({ id, type: 'resource.changed', occurred_at: freshMarket.captured_at, resource: { kind: 'market_snapshot', id: String(id) }, job_id: null, revision: 1 } as Schema['Event']);
  act(() => onChange(event(2)));
  await waitFor(() => expect(api.workspace).toHaveBeenCalledTimes(2));
  act(() => { for (let id = 3; id < 12; id++) onChange(event(id)); });
  expect(api.workspace).toHaveBeenCalledTimes(2);
  await act(async () => { finish(freshWorkspace); await pending; });
  await waitFor(() => expect(queryClient.getQueryData<Schema['Workspace']>(['workspace'])?.market?.snapshot_id).toBe('fresh-market'));
});
