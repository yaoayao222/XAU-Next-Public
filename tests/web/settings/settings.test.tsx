import React from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { api, ApiProblem } from '../../../apps/web/src/api/client';
import { SettingsPage } from '../../../apps/web/src/features/settings/SettingsPage';
import { modelProfiles, modelSettings, settings } from '../fixtures';

beforeEach(() => { vi.spyOn(api, 'modelProfiles').mockResolvedValue(modelProfiles); });
afterEach(() => { cleanup(); vi.restoreAllMocks(); });
describe('AC-18 设置草稿和并发修订', () => {
  it('服务端修订更新后保留草稿，以原revision提交并明确冲突', async () => {
    const cache = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    vi.spyOn(api, 'settings').mockResolvedValue(settings);
    vi.spyOn(api, 'modelSettings').mockResolvedValue(modelSettings);
    const save = vi.spyOn(api, 'saveSettings').mockRejectedValue(new ApiProblem({ type: 'urn:problem', title: '配置版本已变化', status: 409, code: 'VERSION_CONFLICT', detail: '请读取当前配置后重新保存。', trace_id: 'test-conflict', field_errors: [], retryable: false }));
    render(<QueryClientProvider client={cache}><SettingsPage /></QueryClientProvider>);
    const input = await screen.findByLabelText(/^默认手数/);
    fireEvent.change(input, { target: { value: '0.02' } });
    const newer = { ...settings, revision: 2, values: { ...settings.values, default_volume_lots: '0.03' } };
    cache.setQueryData(['settings'], newer);
    await screen.findByText(/服务端已有新配置 #2/);
    expect((input as HTMLInputElement).value).toBe('0.02');
    fireEvent.click(screen.getByRole('button', { name: '保存配置' }));
    await screen.findByRole('alert');
    expect(save.mock.calls[0][0]).toMatchObject({ expected_revision: 1, values: { default_volume_lots: '0.02', per_trade_loss_cap: null, cost_assumption: null } });
    fireEvent.click(screen.getByRole('button', { name: '重新载入最新配置' }));
    await waitFor(() => expect((input as HTMLInputElement).value).toBe('0.03'));
    cache.clear();
  });
});
