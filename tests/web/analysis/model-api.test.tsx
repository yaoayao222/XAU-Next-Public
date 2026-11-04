import React from 'react';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { api, type Schema } from '../../../apps/web/src/api/client';
import { ModelCallPanel } from '../../../apps/web/src/features/analysis/ModelCallPanel';
import { ModelSettingsPanel } from '../../../apps/web/src/features/settings/ModelSettingsPanel';
import { job, modelProfiles, modelSettings, now } from '../fixtures';

const clients: QueryClient[] = [];
beforeEach(() => { vi.spyOn(api, 'modelProfiles').mockResolvedValue(modelProfiles); });
afterEach(() => { cleanup(); clients.forEach(c => c.clear()); clients.length = 0; vi.restoreAllMocks(); localStorage.clear(); });
function show(element: React.ReactNode) { const client = new QueryClient({ defaultOptions: { queries: { retry: false } } }); clients.push(client); return render(<MemoryRouter><QueryClientProvider client={client}>{element}</QueryClientProvider></MemoryRouter>); }
function call(state: Schema['ModelCall']['state']): Schema['ModelCall'] {
  return { call_id: 'call-synthetic', attempt_id: 'attempt-synthetic', job_id: 'job-synthetic', request_id: 'request-synthetic', purpose: 'analysis', stage: 'sent_waiting', state, config_revision: 1, provider: 'SYNTHETIC', protocol: 'responses', model_requested: 'gpt-6.1-sol', model_returned: null, reasoning_effort: 'high', prompt_version: 'manual-analysis/7', answer_schema_version: '1.3.0', created_at: now, started_at: now, completed_received_at: null, ended_at: now, provider_request_id: null, provider_response_id: null, input: { id: 'input-synthetic', sha256: 'a'.repeat(64), size: 12 }, raw_response: null, answer: null, input_tokens: null, output_tokens: null, estimated_cost: null, cost_currency: null, cost_source: null, error_code: 'MODEL_OUTCOME_UNKNOWN', detail: 'SYNTHETIC 响应前断线', version_id: null, retry_of: null, raw_redacted: false, original_response_sha256: null, transport_succeeded: false, answer_valid: null, candidate_count: null, output_format: 'json_schema', stream: false, timeout_seconds: 300, max_output_tokens: 12000, max_response_bytes: 2000000 };
}

it('结果未知保持未知，不展示版本已保存或入场成功', async () => {
  vi.spyOn(api, 'modelCall').mockResolvedValue(call('outcome_unknown'));
  show(<ModelCallPanel callId="call-synthetic" />);
  expect(await screen.findByText('结果未知')).toBeTruthy();
  expect(screen.getByText(/不会自动重发/)).toBeTruthy();
  expect(screen.getByText(/费用估算：未知/)).toBeTruthy();
  expect(screen.queryByText(/预测版本已保存/)).toBeNull();
  expect(screen.queryByRole('button', { name: '取消 API 调用' })).toBeNull();
});

it('完整响应但结构失败时直接显示原任务的字段错误，不加载原始流或重发', async () => {
  vi.spyOn(api, 'modelCall').mockResolvedValue({ ...call('failed'), stage: 'response_saved', transport_succeeded: true, answer_valid: false, error_code: 'SCHEMA_INVALID', detail: '回答结构不符合规范', raw_response: { id: 'raw-synthetic', sha256: 'b'.repeat(64), size: 1234 } });
  const errors = [{ path: '/market_cutoff', code: 'SCHEMA_INVALID', message: '时间必须符合 date-time' }, { path: '/evidence_refs/0', code: 'EVIDENCE_REF_INVALID', message: '疑似字符丢失；合法原值：SYNTHETIC-evidence-1。未自动替换。' }];
  vi.spyOn(api, 'job').mockResolvedValue({ ...job, job_id: 'job-synthetic', state: 'failed', result: null, error: { type: 'about:blank', title: '模型调用未完成', status: 422, code: 'SCHEMA_INVALID', detail: '回答结构不符合规范', trace_id: 'trace-synthetic', field_errors: errors, retryable: false } });
  const raw = vi.spyOn(api, 'modelRaw'); const generate = vi.spyOn(api, 'startModelAnalysis');
  show(<ModelCallPanel callId="call-synthetic" />);
  expect(await screen.findByText('回答字段错误（2 项）')).toBeTruthy();
  expect(screen.getByText('/market_cutoff')).toBeTruthy();
  expect(screen.getByText('/evidence_refs/0')).toBeTruthy();
  expect(screen.getByText(/疑似字符丢失；合法原值/)).toBeTruthy();
  expect(screen.getByText(/响应接收：传输已结束（不代表生成成功） · 回答校验：未通过/)).toBeTruthy();
  expect(screen.getByText('回答未导入，原文已保留')).toBeTruthy();
  expect(screen.queryByText(/预测版本已保存/)).toBeNull();
  expect(raw).not.toHaveBeenCalled(); expect(generate).not.toHaveBeenCalled();
});

it('隐藏输入不持久到浏览器，查询模型成功不冒充生成验证', async () => {
  const current = structuredClone(modelSettings);
  vi.spyOn(api, 'modelSettings').mockImplementation(async () => structuredClone(current));
  const saveKey = vi.spyOn(api, 'setModelCredential').mockImplementation(async () => { current.key_configured = true; current.revision = 2; return structuredClone(current); });
  const check = vi.spyOn(api, 'checkModel').mockImplementation(async () => { current.checks = [{ checked_at: now, config_revision: 2, kind: 'models', status: 'passed', detail: 'SYNTHETIC 模型列表查询成功，生成未验证', model_ids: ['gpt-6.1-sol'], remaining: null, unit: null, is_active: null, call_id: null }]; return structuredClone(current); });
  const probe = vi.spyOn(api, 'probeModel');
  show(<ModelSettingsPanel />);
  const input = await screen.findByLabelText(/^本机 API Key/);
  expect((input as HTMLInputElement).type).toBe('password');
  fireEvent.change(input, { target: { value: 'SYNTHETIC-secret' } });
  fireEvent.click(screen.getByRole('button', { name: '保存到 Windows 凭据' }));
  await screen.findByText('密钥状态：已配置');
  expect((screen.getByLabelText(/^本机 API Key/) as HTMLInputElement).value).toBe('');
  expect(saveKey.mock.calls[0][0].key).toBe('SYNTHETIC-secret');
  expect(Object.values(localStorage).join('')).not.toContain('SYNTHETIC-secret');
  fireEvent.click(screen.getByRole('button', { name: '鉴权并读取模型列表' }));
  await waitFor(() => expect(check).toHaveBeenCalledWith('models'));
  expect(await screen.findByText(/SYNTHETIC 模型列表查询成功/)).toBeTruthy();
  expect(screen.getByText('字符保真待验证')).toBeTruthy();
  expect((screen.getByRole('button', { name: '启动合成生成测试' }) as HTMLButtonElement).disabled).toBe(true);
  expect(probe).not.toHaveBeenCalled();
});

it('分别保存原始流和最终答案上限，保存预算不自动发起合成测试', async () => {
  vi.spyOn(api, 'modelSettings').mockResolvedValue(structuredClone(modelSettings));
  const save = vi.spyOn(api, 'saveModelSettings').mockImplementation(async command => ({ ...modelSettings, revision: 2, values: command.values }));
  const probe = vi.spyOn(api, 'probeModel');
  show(<ModelSettingsPanel />);
  const raw = await screen.findByLabelText(/^原始响应上限/);
  expect((raw as HTMLInputElement).max).toBe('16000000');
  fireEvent.change(raw, { target: { value: '16000000' } });
  fireEvent.change(screen.getByLabelText(/^最终答案上限/), { target: { value: '1000000' } });
  fireEvent.change(screen.getByLabelText(/^推理和回答总上限/), { target: { value: '25000' } });
  fireEvent.change(screen.getByLabelText('总超时（秒）'), { target: { value: '600' } });
  fireEvent.click(screen.getByRole('button', { name: '保存模型配置' }));
  await waitFor(() => expect(save).toHaveBeenCalledTimes(1));
  expect(save.mock.calls[0][0].values).toMatchObject({ max_response_bytes: 16000000, max_answer_bytes: 1000000, max_output_tokens: 25000, timeout_seconds: 600 });
  expect(probe).not.toHaveBeenCalled();
});
