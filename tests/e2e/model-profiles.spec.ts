import { test, expect } from '@playwright/test';

test('保存CPA与原连接，来回切换和刷新不丢参数、不生成', async ({ page, request }) => {
  const api = 'http://127.0.0.1:18765';
  const headers = { Authorization: 'Bearer e2e-isolated-session' };
  const initial = await (await request.get(api + '/v1/model/profiles', { headers })).json();
  const originalId = initial.active_profile_id;
  const originalName = initial.items.find((item: { profile_id: string }) => item.profile_id === originalId).name;
  const original = await (await request.get(api + '/v1/model/settings', { headers })).json();
  const generationRequests: string[] = [];
  page.on('request', req => {
    if (/\/v1\/model\/probe$|\/model-call$/.test(new URL(req.url()).pathname) && req.method() === 'POST') generationRequests.push(req.url());
  });
  await page.goto('/settings?api=' + encodeURIComponent(api) + '#token=e2e-isolated-session');
  const panel = page.getByRole('region', { name: '模型 API 设置' });
  if (!original.key_configured) {
    await panel.getByLabel(/^本机 API Key/).fill('SYNTHETIC-browser-key');
    await panel.getByRole('button', { name: '保存到 Windows 凭据' }).click();
    await expect(panel.getByText('密钥状态：已配置', { exact: true })).toBeVisible();
  }
  await panel.getByRole('button', { name: '新增配置', exact: true }).click();
  const name = '离线 CPA ' + Date.now();
  await panel.getByLabel('新配置名称', { exact: true }).fill(name);
  await expect(panel.getByLabel(/^API Base URL/)).toHaveValue('http://127.0.0.1:8317/v1');
  await panel.getByRole('button', { name: '保存为新配置并切换', exact: true }).click();
  await expect(panel.getByText('密钥状态：未配置 / 本机不存在', { exact: true })).toBeVisible();
  await panel.getByLabel(/^本机 API Key/).fill('SYNTHETIC-browser-key');
  await panel.getByRole('button', { name: '保存到 Windows 凭据' }).click();
  await expect(panel.getByText('密钥状态：已配置', { exact: true })).toBeVisible();
  const saved = await (await request.get(api + '/v1/model/profiles', { headers })).json();
  const cpaId = saved.active_profile_id;
  await panel.getByRole('combobox', { name: '已保存配置', exact: true }).selectOption(originalId);
  await panel.getByRole('button', { name: '切换到所选配置' }).click();
  await expect(panel.getByText(`当前使用：${originalName}。`, { exact: false })).toBeVisible();
  await expect(panel.getByLabel(/^API Base URL/)).toHaveValue(original.values.base_url);
  await expect(panel.getByText('密钥状态：已配置', { exact: true })).toBeVisible();
  await panel.getByRole('combobox', { name: '已保存配置', exact: true }).selectOption(cpaId);
  await panel.getByRole('button', { name: '切换到所选配置' }).click();
  await expect(panel.getByLabel(/^API Base URL/)).toHaveValue('http://127.0.0.1:8317/v1');
  await page.reload();
  await expect(panel.getByText(`当前使用：${name}。`, { exact: false })).toBeVisible();
  await expect(panel.getByText('密钥状态：已配置', { exact: true })).toBeVisible();
  expect(generationRequests).toEqual([]);
  await page.screenshot({ path: `${process.env.XAU_NEXT_E2E_SCREENSHOTS ?? 'test-results'}/model-profiles-desktop.png`, fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1)).toBe(true);
  await page.screenshot({ path: `${process.env.XAU_NEXT_E2E_SCREENSHOTS ?? 'test-results'}/model-profiles-mobile.png`, fullPage: true });
});
