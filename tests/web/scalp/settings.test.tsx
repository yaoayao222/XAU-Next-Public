import React from 'react';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { api } from '../../../apps/web/src/api/client';
import { SettingsPage } from '../../../apps/web/src/features/settings/SettingsPage';
import { modelProfiles, modelSettings, settings } from '../fixtures';

beforeEach(() => { vi.spyOn(api, 'modelProfiles').mockResolvedValue(modelProfiles); });
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

it('金价价差设置采用后端值，保存独立偏好并保留金额和风险预算', async () => {
  const value = structuredClone(settings);
  Object.assign(value.values, { analysis_preferences: { enabled_styles: ['scalp', 'intraday'], scalp_refresh_minutes: 30, intraday_refresh_minutes: 120, desktop_notifications: true,
    scalp_preferences: { version: 'scalp-preferences/1', price_unit: 'USD_per_ounce', target_min: '2.25', target_max: '4.75', max_entry_distance: '1.75', max_trigger_distance: '1.50', preferred_holding_minutes: 8, max_holding_minutes: 25 } } });
  vi.spyOn(api, 'settings').mockResolvedValue(value);
  vi.spyOn(api, 'modelSettings').mockResolvedValue(modelSettings);
  const save = vi.spyOn(api, 'saveSettings').mockResolvedValue(value);
  render(<QueryClientProvider client={new QueryClient()}><SettingsPage /></QueryClientProvider>);
  const minimum = await screen.findByLabelText('目标价差下限（美元）');
  expect((minimum as HTMLInputElement).value).toBe('2.25');
  expect((screen.getByLabelText('目标价差上限（美元）') as HTMLInputElement).value).toBe('4.75');
  expect(screen.getByText(/4000→4003 是金价价差3美元/)).toBeTruthy();
  expect(screen.getByText(/修改只影响新请求/)).toBeTruthy();
  fireEvent.change(minimum, { target: { value: '2.50' } });
  fireEvent.click(screen.getByRole('button', { name: '保存配置' }));
  await waitFor(() => expect(save).toHaveBeenCalledOnce());
  expect(save.mock.calls[0][0]).toMatchObject({ expected_revision: value.revision, values: { per_trade_loss_cap: settings.values.per_trade_loss_cap, min_net_rr: settings.values.min_net_rr, analysis_preferences: { scalp_preferences: { target_min: '2.50', target_max: '4.75', max_entry_distance: '1.75', preferred_holding_minutes: 8 } } } });
  expect(value.values.analysis_preferences?.scalp_preferences?.target_min).toBe('2.25');
});

it('后端没有超短线偏好时不在页面补造默认价格参数', async () => {
  vi.spyOn(api, 'settings').mockResolvedValue(settings);
  vi.spyOn(api, 'modelSettings').mockResolvedValue(modelSettings);
  render(<QueryClientProvider client={new QueryClient()}><SettingsPage /></QueryClientProvider>);
  await screen.findByLabelText(/^默认手数/);
  expect(screen.getByText('尚未取得超短线偏好，请等待服务端配置。')).toBeTruthy();
  expect(screen.queryByLabelText('目标价差下限（美元）')).toBeNull();
});
