import { test, expect } from '@playwright/test';
import { readFileSync } from 'node:fs';
import { randomUUID } from 'node:crypto';

const api = 'http://127.0.0.1:18765';
const headers = { Authorization: 'Bearer e2e-isolated-session' };

test('新版分析到当前入场建议、后台通知和重新打开网页', async ({ page, request, context }) => {
  const errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  await expect.poll(async () => Boolean((await (await request.get(api + '/v1/workspace', { headers })).json()).market)).toBe(true);
  const market = (await (await request.get(api + '/v1/workspace', { headers })).json()).market;
  const settings = await (await request.get(api + '/v1/settings', { headers })).json();
  Object.assign(settings.values, { default_volume_lots: '0.01', per_trade_loss_cap: { amount: '20', currency: 'USD' }, min_net_rr: '2',
    cost_assumption: { commission_roundtrip_per_lot: '5', slippage_reserve_per_lot: '20', other_cost_per_lot: '0', currency: 'USD',
      valid_from: market.market_cutoff, valid_until: null, account_scope_id: market.account_scope_id, symbol: market.symbol, source_note: '合成浏览器验收' } });
  const configured = await request.put(api + '/v1/settings', { headers: { ...headers, 'Idempotency-Key': randomUUID() }, data: { expected_revision: settings.revision, values: settings.values } });
  expect(configured.ok()).toBe(true);
  await page.goto('/analysis?api=' + encodeURIComponent(api) + '#token=e2e-isolated-session');
  await page.getByRole('button', { name: '新建一轮分析', exact: true }).click();
  await expect(page.getByRole('button', { name: '复制完整分析包' })).toBeEnabled();
  const id = new URL(page.url()).searchParams.get('request')!;
  const req = await (await request.get(api + '/v1/analysis/requests/' + id, { headers })).json();
  expect(req.packet_text).toContain('statistics');
  const answer = JSON.parse(readFileSync('fixtures/analysis-answer.experience.json', 'utf8'));
  answer.schema_version = req.answer_schema_version;
  if (['1.5.0','1.6.0','1.7.0'].includes(req.answer_schema_version)) answer.plans.forEach((plan: any) => { plan.holding_time_basis = '合成：局部结构支持20分钟内观察，实际结果未知。'; });
  for (const key of ['request_id', 'forecast_id', 'base_version_id', 'market_snapshot_id', 'market_cutoff']) answer[key] = req[key];
  const add = (minutes: number) => new Date(Date.parse(req.window_start) + minutes * 60000).toISOString();
  answer.horizons = req.horizon_minutes.map((n: number) => ({ horizon_minutes: n, end_at: add(n), direction: 'uncertain',
    expected_range: { low: '98', high: '104' }, phases: [{ start_at: req.market_cutoff, end_at: add(n), direction: 'uncertain', note: '合成闭环测试' }], turning_points: [], confidence: null }));
  if (['1.6.0', '1.7.0'].includes(req.answer_schema_version)) {
    answer.horizons.forEach((h: any) => { h.forecast_basis = { direction_basis: '合成端点方向判断', range_basis: '合成完整窗口范围',
      supporting_evidence: ['合成当前结构'], opposing_evidence: ['合成反向路径'], evidence_refs: [req.market_snapshot_id], limitations: ['未验证真实质量'] }; });
    answer.plans.forEach((p: any) => p.entry_checks.forEach((r: any) => Object.assign(r, { observation: 'new_since_available', purpose: '合成独立入场检查' })));
  }
  if (req.answer_schema_version === '1.7.0') {
    answer.plans.forEach((plan: any) => { plan.holding_exit = {
      condition: null, reason: 'SYNTHETIC：本例没有额外结构退出依据。'
    }; });
  }
  Object.assign(answer.plans[0], { trading_style: 'scalp', max_holding_minutes: 20, valid_from: req.market_cutoff, entry_deadline: add(20), exit_deadline: add(30),
    activation: { kind: 'immediate' }, invalidation: { kind: 'quote_compare', quote_side: 'bid', operator: 'lt', price: '99' }, stop_loss: '99', take_profit: '103' });
  answer.candidate_assessments[0] = { trading_style: 'scalp', outcome: 'candidate', plan_key: 'primary', rationale: '合成候选', missing_conditions: [] };
  answer.candidate_assessments[1] = { trading_style: 'intraday', outcome: 'no_candidate', plan_key: null, rationale: '合成：日内空间不足', missing_conditions: ['缺少足够日内目标空间依据。'] };
  answer.evidence_refs = [req.market_snapshot_id];
  const raw = JSON.stringify(answer, null, 2);
  await page.getByLabel('结构化回答原文').fill(raw);
  await page.getByLabel('模型标识（选填）').fill('synthetic-fixture');
  await page.getByLabel('实际推理强度').selectOption('high');
  await page.getByLabel('使用平台（选填）').fill('浏览器合成验收');
  await page.getByRole('button', { name: '校验并保存回答' }).click();
  await expect(page.getByText('回答已处理，可查看保存的版本及方案。', { exact: false })).toBeVisible();
  const experience = page.locator('details.experience-application');
  await expect(experience).not.toHaveAttribute('open', '');
  await experience.locator('summary').first().click();
  await expect(experience.getByText(/字段与引用校验通过不证明解释正确或预测更准/)).toBeVisible();
  await experience.screenshot({ path: `${process.env.XAU_NEXT_E2E_SCREENSHOTS ?? 'test-results'}/experience-application.png` });
  await page.getByRole('link', { name: '返回工作台 →' }).click();
  const guidance = page.getByRole('region', { name: '系统行动建议' });
  await expect(guidance.getByRole('status').filter({ hasText: '超短线 · 当前可以买入' })).toBeVisible({ timeout: 20000 });
  const candidate = page.getByRole('article', { name: '超短线方案详情' });
  await expect(candidate.getByText('入场方式：回踩限价', { exact: true })).toBeVisible();
  await expect(candidate.getByText('当前可以买入', { exact: true })).toBeVisible();
  const basis = candidate.locator('details.plan-basis');
  await expect(basis).not.toHaveAttribute('open', '');
  await expect(candidate.getByText(answer.plans[0].rationale, { exact: true })).not.toBeVisible();
  await candidate.getByText('方案依据与参数', { exact: true }).click();
  await expect(candidate.getByText(answer.plans[0].rationale, { exact: true })).toBeVisible();
  await candidate.screenshot({ path: `${process.env.XAU_NEXT_E2E_SCREENSHOTS ?? 'test-results'}/plan-basis-expanded-desktop.png` });
  await candidate.getByText('方案依据与参数', { exact: true }).click();
  await expect(basis).not.toHaveAttribute('open', '');
  await candidate.screenshot({ path: `${process.env.XAU_NEXT_E2E_SCREENSHOTS ?? 'test-results'}/plan-compact-desktop.png` });
  await expect(guidance.getByText('缺少足够日内目标空间依据。')).toBeVisible();
  await expect.poll(async () => {
    const ws = await (await request.get(api + '/v1/workspace', { headers })).json();
    return ws.notifications.some((n: any) => n.kind === 'entry' && n.state === 'suppressed' && n.title.includes('合成验收'));
  }).toBe(true);
  await page.screenshot({ path: `${process.env.XAU_NEXT_E2E_SCREENSHOTS ?? 'test-results'}/guidance-desktop.png`, fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(guidance.getByRole('status').filter({ hasText: '超短线 · 当前可以买入' })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1)).toBe(true);
  await expect(candidate.getByText('入场方式：回踩限价', { exact: true })).toBeVisible();
  await candidate.screenshot({ path: `${process.env.XAU_NEXT_E2E_SCREENSHOTS ?? 'test-results'}/plan-compact-mobile.png` });
  await page.screenshot({ path: `${process.env.XAU_NEXT_E2E_SCREENSHOTS ?? 'test-results'}/guidance-mobile.png`, fullPage: true });
  const before = await (await request.get(api + '/v1/workspace', { headers })).json();
  await page.close();
  await expect.poll(async () => (await (await request.get(api + '/v1/workspace', { headers })).json()).market.snapshot_id,
    { timeout: 15000 }).not.toBe(before.market.snapshot_id);
  const after = await (await request.get(api + '/v1/workspace', { headers })).json();
  expect(after.notifications.filter((n: any) => n.kind === 'entry')).toHaveLength(1);
  const reopened = await context.newPage();
  await reopened.goto('/?api=' + encodeURIComponent(api) + '#token=e2e-isolated-session');
  await expect(reopened.getByRole('region', { name: '系统行动建议' })).toBeVisible();
  const saved = await (await request.get(api + '/v1/forecasts/' + req.forecast_id, { headers })).json();
  expect(saved.current_version.raw_text).toBe(raw);
  expect(saved.current_version.origin.reasoning_effort).toBe('high');

  // A later valid observation-only answer must not manufacture plans or entry notices.
  await reopened.getByRole('link', { name: '新分析', exact: true }).click();
  await reopened.getByRole('button', { name: '新建一轮分析', exact: true }).click();
  await expect(reopened.getByRole('button', { name: '复制完整分析包' })).toBeEnabled();
  const nextId = new URL(reopened.url()).searchParams.get('request')!;
  const next = await (await request.get(api + '/v1/analysis/requests/' + nextId, { headers })).json();
  const empty = structuredClone(answer);
  for (const key of ['request_id', 'forecast_id', 'base_version_id', 'market_snapshot_id', 'market_cutoff']) empty[key] = next[key];
  const nextEnd = (n: number) => new Date(Date.parse(next.window_start) + n * 60000).toISOString();
  empty.horizons = next.horizon_minutes.map((n: number) => ({ ...answer.horizons[0], horizon_minutes: n, end_at: nextEnd(n),
    phases: [{ start_at: next.market_cutoff, end_at: nextEnd(n), direction: 'uncertain', note: '合成观望回答' }] }));
  empty.plans = [];
  empty.horizons.forEach((h: any) => { if (h.forecast_basis) h.forecast_basis.evidence_refs = [next.market_snapshot_id]; });
  empty.evidence_refs = [next.market_snapshot_id];
  empty.candidate_assessments = [
    { trading_style: 'scalp', outcome: 'no_candidate', plan_key: null, rationale: '短线结构不足', missing_conditions: ['短线缺少结构止损依据。'] },
    { trading_style: 'intraday', outcome: 'no_candidate', plan_key: null, rationale: '日内空间不足', missing_conditions: ['日内缺少目标空间依据。'] },
  ];
  const emptyRaw = JSON.stringify(empty, null, 2);
  await reopened.getByLabel('结构化回答原文').fill(emptyRaw);
  await reopened.getByRole('button', { name: '校验并保存回答' }).click();
  await expect(reopened.getByText('回答已处理，可查看保存的版本及方案。', { exact: false })).toBeVisible();
  await reopened.getByRole('link', { name: '返回工作台 →' }).click();
  const observation = reopened.getByRole('region', { name: '系统行动建议' });
  await expect(observation.getByText('短线缺少结构止损依据。')).toBeVisible();
  await expect(observation.getByText('日内缺少目标空间依据。')).toBeVisible();
  await expect(observation.getByText('当前可入场', { exact: true })).toHaveCount(0);
  await reopened.setViewportSize({ width: 390, height: 844 });
  expect(await reopened.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1)).toBe(true);
  await reopened.screenshot({ path: `${process.env.XAU_NEXT_E2E_SCREENSHOTS ?? 'test-results'}/no-candidates-mobile.png`, fullPage: true });
  await reopened.setViewportSize({ width: 1440, height: 1050 });
  await reopened.screenshot({ path: `${process.env.XAU_NEXT_E2E_SCREENSHOTS ?? 'test-results'}/no-candidates-desktop.png`, fullPage: true });
  const observed = await (await request.get(api + '/v1/workspace', { headers })).json();
  expect(observed.plans).toEqual([]);
  expect(observed.action_advice).toEqual([]);
  expect(observed.notifications.filter((n: any) => n.kind === 'entry')).toHaveLength(1);
  expect((await (await request.get(api + '/v1/forecasts/' + next.forecast_id, { headers })).json()).current_version.raw_text).toBe(emptyRaw);
  expect(errors).toEqual([]);
});
