import { test, expect } from '@playwright/test';
import { settings } from '../web/fixtures';

test('SYNTHETIC review progress survives reload without submitting work', async ({ page }, testInfo) => {
  const now = '2026-10-16T07:00:00Z';
  const job = { job_id: 'synthetic-review', kind: 'review_refresh', state: 'running',
    created_at: now, updated_at: now, progress: 0.4, message: '补算 4/10 · 已保存检查点',
    result: null, error: null, revision: 2 };
  let writes = 0;
  await page.route('**/v1/**', async route => {
    const req = route.request();
    if (req.method() !== 'GET') writes++;
    const path = new URL(req.url()).pathname;
    if (path === '/v1/events') return route.fulfill({ contentType: 'text/event-stream', body: '' });
    const body = path === '/v1/settings' ? settings : path === '/v1/workspace' ? { revision: 1, event_cursor: 0, as_of: now, market: null,
      current_forecast: null, plans: [], evaluations: [], portfolio: null, active_jobs: [job],
      next_actions: [], pending_requests: [] }
      : path === '/v1/jobs/synthetic-review' ? job : { items: [], next_cursor: null };
    return route.fulfill({ contentType: 'application/json', body: JSON.stringify(body) });
  });
  await page.goto('/history?api=http%3A%2F%2F127.0.0.1%3A18765#token=e2e-isolated-session');
  await expect(page.getByText(/补算 4\/10/)).toBeVisible();
  await expect(page.getByRole('button', { name: '正在补算…' })).toBeDisabled();
  await page.getByText('什么时候需要补算？', { exact: true }).click();
  await expect(page.getByText(/资料不足项保留未知并等待新证据，不再定时循环补算/)).toBeVisible();
  await page.reload();
  await expect(page.getByText(/补算 4\/10/)).toBeVisible();
  await expect(page.getByRole('progressbar')).toHaveAttribute('value', '0.4');
  await page.screenshot({ path: testInfo.outputPath('review-desktop.png'), fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath('review-mobile.png'), fullPage: true });
  expect(writes).toBe(0);
});
