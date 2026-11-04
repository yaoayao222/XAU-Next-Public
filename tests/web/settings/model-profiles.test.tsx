import React from 'react';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, expect, it, vi } from 'vitest';
import { api } from '../../../apps/web/src/api/client';
import { ModelSettingsPanel } from '../../../apps/web/src/features/settings/ModelSettingsPanel';
import { modelProfiles, modelSettings } from '../fixtures';

const clients: QueryClient[] = [];
afterEach(() => { cleanup(); clients.forEach(c => c.clear()); clients.length = 0; vi.restoreAllMocks(); localStorage.clear(); });
function setup(two = false, overrides: Partial<typeof modelSettings> = {}) {
  const settings = { ...structuredClone(modelSettings), ...overrides };
  const profiles = structuredClone(modelProfiles);
  if (two) profiles.items.push({ profile_id: 'cpa', name: '本地 CPA', base_url: 'http://127.0.0.1:8317/v1', model_id: 'gpt-6.1-sol' });
  vi.spyOn(api, 'modelSettings').mockImplementation(async () => structuredClone(settings));
  vi.spyOn(api, 'modelProfiles').mockImplementation(async () => structuredClone(profiles));
  const probe = vi.spyOn(api, 'probeModel');
  const check = vi.spyOn(api, 'checkModel');
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  clients.push(client);
  render(<QueryClientProvider client={client}><ModelSettingsPanel /></QueryClientProvider>);
  return { settings, profiles, probe, check };
}

it('新增CPA预填参数，保存为独立配置；不会测试、读取或传送旧密钥', async () => {
  const { settings, profiles, probe, check } = setup();
  const create = vi.spyOn(api, 'createModelProfile').mockImplementation(async body => {
    settings.revision++; settings.values = body.values;
    profiles.active_profile_id = 'cpa'; profiles.settings_revision = settings.revision;
    profiles.items.push({ profile_id: 'cpa', name: body.name, base_url: body.values.base_url, model_id: body.values.model_id });
    return structuredClone(settings);
  });
  fireEvent.click(await screen.findByRole('button', { name: '新增配置' }));
  expect((screen.getByLabelText(/^API Base URL/) as HTMLInputElement).value).toBe('http://127.0.0.1:8317/v1');
  expect((screen.getByLabelText('调用协议') as HTMLSelectElement).value).toBe('responses');
  expect((screen.getByLabelText('回答格式') as HTMLSelectElement).value).toBe('json_schema');
  expect((screen.getByLabelText(/^本机 API Key/) as HTMLInputElement).disabled).toBe(true);
  fireEvent.click(screen.getByRole('button', { name: '保存为新配置并切换' }));
  await waitFor(() => expect(create).toHaveBeenCalledOnce());
  expect(create.mock.calls[0][0]).toMatchObject({ name: '本地 CPA', values: { stream: true, max_output_tokens: 25000, timeout_seconds: 600 } });
  expect(JSON.stringify(create.mock.calls[0][0])).not.toContain('credential');
  await screen.findByText(/当前使用：本地 CPA/);
  expect(probe).not.toHaveBeenCalled(); expect(check).not.toHaveBeenCalled();
});

it('下拉选择后点击切换，不自动生成；未保存草稿和密钥阻止误切换', async () => {
  const { settings, profiles, probe } = setup(true);
  const activate = vi.spyOn(api, 'activateModelProfile').mockImplementation(async id => {
    settings.revision++; settings.values.base_url = 'http://127.0.0.1:8317/v1';
    profiles.active_profile_id = id; profiles.settings_revision = settings.revision;
    return structuredClone(settings);
  });
  const select = await screen.findByLabelText('已保存配置');
  fireEvent.change(screen.getByLabelText(/^本机 API Key/), { target: { value: 'SYNTHETIC-UNSAVED' } });
  expect((select as HTMLSelectElement).disabled).toBe(true);
  fireEvent.change(screen.getByLabelText(/^本机 API Key/), { target: { value: '' } });
  fireEvent.change(select, { target: { value: 'cpa' } });
  expect(activate).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: '切换到所选配置' }));
  await waitFor(() => expect(activate).toHaveBeenCalledWith('cpa', 1));
  await screen.findByText(/当前使用：本地 CPA/);
  await waitFor(() => expect((screen.getByLabelText('已保存配置') as HTMLSelectElement).value).toBe('cpa'));
  expect(probe).not.toHaveBeenCalled();
  fireEvent.change(screen.getByLabelText(/^模型 ID/), { target: { value: '' } });
  fireEvent.change(screen.getByLabelText(/^自定义模型 ID/), { target: { value: 'SYNTHETIC-edited' } });
  expect((screen.getByLabelText('已保存配置') as HTMLSelectElement).disabled).toBe(true);
  expect((screen.getByRole('button', { name: '切换到所选配置' }) as HTMLButtonElement).disabled).toBe(true);
});

it('已有待选择配置能下拉选择两模型并保留同一连接，不自动查询或生成', async () => {
  const ids = ['deepseek-v4-pro', 'deepseek-v4-flash-0731'];
  const { settings, check, probe } = setup(false, {
    values: { ...modelSettings.values, provider: 'SYNTHETIC-relay', model_id: '待选择模型', model_ids: ids },
  });
  const save = vi.spyOn(api, 'saveModelSettings').mockImplementation(async body => {
    settings.revision++; settings.values = body.values; return structuredClone(settings);
  });
  const credential = vi.spyOn(api, 'setModelCredential');
  const create = vi.spyOn(api, 'createModelProfile');
  const select = await screen.findByRole('combobox', { name: /^模型 ID/ });
  for (const id of ids) expect(screen.getByRole('option', { name: id })).toBeTruthy();
  fireEvent.change(select, { target: { value: ids[0] } });
  fireEvent.change(select, { target: { value: ids[1] } });
  expect(save).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('checkbox', { name: '启用 API 分析入口' }));
  fireEvent.click(screen.getByRole('button', { name: '保存模型配置' }));
  await waitFor(() => expect(save).toHaveBeenCalledOnce());
  expect(save.mock.calls[0][0]).toMatchObject({ values: { model_id: ids[1], model_ids: ids, enabled: true, base_url: modelSettings.values.base_url } });
  await waitFor(() => expect((screen.getByRole('combobox', { name: /^模型 ID/ }) as HTMLSelectElement).value).toBe(ids[1]));
  expect(check).not.toHaveBeenCalled(); expect(probe).not.toHaveBeenCalled();
  expect(credential).not.toHaveBeenCalled(); expect(create).not.toHaveBeenCalled();
});

it('目录勾选立即进入默认模型下拉框；新增供应商不沿用旧目录', async () => {
  const { check } = setup(false, { checks: [{ kind: 'models', status: 'passed', checked_at: '2026-10-27T00:00:00Z', config_revision: 1,
    model_ids: ['SYNTHETIC-catalogue-pro', 'SYNTHETIC-catalogue-flash'], detail: '', remaining: null, unit: null, is_active: null, call_id: null }] });
  await screen.findByRole('combobox', { name: /^模型 ID/ });
  fireEvent.click(screen.getByRole('checkbox', { name: 'SYNTHETIC-catalogue-flash' }));
  fireEvent.change(screen.getByLabelText(/^模型 ID/), { target: { value: 'SYNTHETIC-catalogue-flash' } });
  expect((screen.getByLabelText(/^模型 ID/) as HTMLSelectElement).value).toBe('SYNTHETIC-catalogue-flash');
  expect(check).not.toHaveBeenCalled();
  cleanup(); clients.forEach(c => c.clear());
  setup(false, { checks: [{ kind: 'models', status: 'passed', checked_at: '2026-10-27T00:00:00Z', config_revision: 1,
    model_ids: ['SYNTHETIC-old-only'], detail: '', remaining: null, unit: null, is_active: null, call_id: null }] });
  fireEvent.click(await screen.findByRole('button', { name: '新增鸡出没连接' }));
  expect(screen.queryByRole('option', { name: 'SYNTHETIC-old-only' })).toBeNull();
  expect(screen.queryByRole('option', { name: 'gpt-6.1-sol' })).toBeNull();
});

it('鸡出没预设先停用、不猜模型ID；独立保存多模型与输出参数，不进行测试', async () => {
  const {settings,probe,check}=setup();
  const create=vi.spyOn(api,'createModelProfile').mockResolvedValue(settings);
  fireEvent.click(await screen.findByRole('button',{name:'新增鸡出没连接'}));
  expect((screen.getByLabelText(/^API Base URL/) as HTMLInputElement).value).toBe('https://xcmapi.org/v1');
  expect((screen.getByLabelText('调用协议') as HTMLSelectElement).value).toBe('chat_completions');
  expect((screen.getByRole('checkbox',{name:'启用 API 分析入口'}) as HTMLInputElement).checked).toBe(false);
  expect((screen.getByLabelText(/^模型 ID/) as HTMLInputElement).value).toBe('待选择模型');
  fireEvent.change(screen.getByLabelText(/^可选模型编号/),{target:{value:'wire-flash\nwire-pro'}});
  fireEvent.change(screen.getByLabelText(/^模型 ID/),{target:{value:'wire-pro'}});
  fireEvent.change(screen.getByLabelText(/^Chat输出上限参数/),{target:{value:'max_tokens'}});
  fireEvent.click(screen.getByRole('button',{name:'保存为新配置并切换'}));
  await waitFor(()=>expect(create).toHaveBeenCalledOnce());
  expect(create.mock.calls[0][0]).toMatchObject({values:{model_id:'wire-pro',model_ids:['wire-flash','wire-pro'],chat_token_parameter:'max_tokens',max_output_tokens:32000,enabled:false}});
  expect(probe).not.toHaveBeenCalled();expect(check).not.toHaveBeenCalled();
});

it('调用协议列表包含 Anthropic Messages，切换时不触发生成或测试', async () => {
  const { probe, check } = setup();
  const select = await screen.findByLabelText('调用协议') as HTMLSelectElement;
  expect(Array.from(select.options).map(option => option.value)).toContain('anthropic_messages');
  fireEvent.change(select, { target: { value: 'anthropic_messages' } });
  expect(select.value).toBe('anthropic_messages');
  expect(await screen.findByText(/Anthropic Messages 不发送 OpenAI response_format/)).toBeTruthy();
  expect(probe).not.toHaveBeenCalled();
  expect(check).not.toHaveBeenCalled();
});
