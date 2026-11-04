import React from 'react';
import { afterEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { HistoryPage } from '../../../apps/web/src/features/history/HistoryPage';
import { api } from '../../../apps/web/src/api/client';
import { workspace, job } from '../fixtures';

afterEach(() => { cleanup(); vi.restoreAllMocks(); });

it('restores the running review after remount and prevents duplicate submission', async () => {
  const active = { ...job, kind: 'review_refresh' as const, state: 'running' as const,
    progress: 0.25, message: '补算 1/4 · 已保存检查点', result: null };
  const queued = { ...active, job_id: 'queued-review', state: 'queued' as const, progress: null };
  vi.spyOn(api, 'workspace').mockResolvedValue({ ...workspace, active_jobs: [queued, active] });
  vi.spyOn(api, 'forecasts').mockResolvedValue({ items: [], next_cursor: null });
  vi.spyOn(api, 'reviews').mockResolvedValue({ items: [], next_cursor: null });
  vi.spyOn(api, 'job').mockResolvedValue(active);
  const submit = vi.spyOn(api, 'refreshReviews');
  const mount = () => render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><HistoryPage /></QueryClientProvider>);
  const first = mount();
  expect(await screen.findByText(/补算 1\/4/)).toBeTruthy();
  first.unmount();
  mount();
  expect(await screen.findByText(/补算 1\/4/)).toBeTruthy();
  expect((screen.getByRole('button', { name: '正在补算…' }) as HTMLButtonElement).disabled).toBe(true);
  expect(screen.getByRole('progressbar').getAttribute('value')).toBe('0.25');
  expect(screen.getByText(/另有 1 个补算任务等待处理/)).toBeTruthy();
  fireEvent.click(screen.getByText('什么时候需要补算？'));
  expect(screen.getByText(/资料不足项保留未知并等待新证据，不再定时循环补算/)).toBeTruthy();
  expect(submit).not.toHaveBeenCalled();
});
