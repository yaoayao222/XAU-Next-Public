import { test, expect, type APIRequestContext, type Page } from '@playwright/test';
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { randomUUID } from 'node:crypto';

const api = process.env.XAU_SCALP_BROWSER_API ?? 'http://127.0.0.1:18775';
const evidence = process.env.XAU_SCALP_BROWSER_EVIDENCE ?? 'test-results/scalp-scale';
const headers = { Authorization: 'Bearer e2e-isolated-session' };
mkdirSync(evidence, { recursive: true });

async function get(request: APIRequestContext, path: string) {
  const response = await request.get(api + path, { headers });
  expect(response.ok(), await response.text()).toBe(true);
  return response.json();
}

async function source(request: APIRequestContext, at: string, bid = '99.98', ask = '100.00', position = false) {
  const response = await request.post(api + '/__synthetic/source', { headers, data: { at, bid, ask, position } });
  expect(response.ok(), await response.text()).toBe(true);
}

async function processPending(request: APIRequestContext, kind: 'market_refresh' | 'answer_import') {
  const response = await request.post(api + '/__synthetic/process', { headers, data: { kind } });
  expect(response.ok(), await response.text()).toBe(true);
  return (await response.json()).workspace;
}

async function capture(request: APIRequestContext, at: string, bid = '99.98', ask = '100.00', position = false) {
  await source(request, at, bid, ask, position);
  const response = await request.post(api + '/v1/market/refresh', { headers: { ...headers, 'Idempotency-Key': randomUUID() }, data: { include_history: false, history_from: null, history_to: null } });
  expect(response.ok(), await response.text()).toBe(true);
  const job = await response.json();
  const workspace = await processPending(request, 'market_refresh');
  expect((await get(request, '/v1/jobs/' + job.job_id)).state).toBe('succeeded');
  return workspace;
}

async function newRequest(page: Page, request: APIRequestContext) {
  await page.goto('/analysis?api=' + encodeURIComponent(api) + '#token=e2e-isolated-session');
  await page.getByRole('button', { name: '新建分析请求', exact: true }).click();
  await expect(page.getByRole('button', { name: '复制完整分析包' })).toBeEnabled();
  return get(request, '/v1/analysis/requests/' + new URL(page.url()).searchParams.get('request'));
}

function answerFor(request: any, far: boolean) {
  const answer = JSON.parse(readFileSync('fixtures/analysis-answer.experience.json', 'utf8'));
  answer.schema_version = request.answer_schema_version;
  for (const key of ['request_id', 'forecast_id', 'base_version_id', 'market_snapshot_id', 'market_cutoff']) answer[key] = request[key];
  const after = (minutes: number) => new Date(Date.parse(request.window_start) + minutes * 60000).toISOString();
  answer.horizons = request.horizon_minutes.map((minutes: number) => ({ horizon_minutes: minutes, end_at: after(minutes), direction: 'uncertain', expected_range: { low: '98.00', high: '114.00' }, phases: [{ start_at: request.market_cutoff, end_at: after(minutes), direction: 'uncertain', note: 'SYNTHETIC 超短线尺度浏览器验收' }], turning_points: [], confidence: null }));
  Object.assign(answer.plans[0], { trading_style: 'scalp', max_holding_minutes: 10, holding_time_basis: null, valid_from: request.market_cutoff, entry_deadline: after(25), exit_deadline: after(30),
    activation: far ? { kind: 'immediate' } : { kind: 'quote_compare', quote_side: 'bid', operator: 'gte', price: '100.20' },
    invalidation: { kind: 'quote_compare', quote_side: 'bid', operator: 'lt', price: '99.00' }, post_activation_invalidation: null,
    entry: { mode: 'limit_retest', price: '100.00', zone: { low: '99.90', high: '100.10' } }, stop_loss: '99.00', take_profit: far ? '113.19' : '103.00',
    entry_checks: [{ kind: 'quote_compare', quote_side: 'ask', operator: 'lte', price: '100.00' }], rationale: far ? 'SYNTHETIC 远端目标，验证尺度拒绝并保留原文。' : 'SYNTHETIC 近端候选，等待报价触发后回踩。' });
  answer.candidate_assessments = [{ trading_style: 'scalp', outcome: 'candidate', plan_key: 'primary', rationale: answer.plans[0].rationale, missing_conditions: [] },
    { trading_style: 'intraday', outcome: far ? 'candidate' : 'no_candidate', plan_key: far ? 'backup' : null, rationale: far ? 'SYNTHETIC 合法日内信息保留。' : 'SYNTHETIC 本次没有日内结构依据。', missing_conditions: far ? [] : ['缺少日内结构依据。'] }];
  if (far) answer.plans.push({ ...structuredClone(answer.plans[0]), plan_key: 'backup', role: 'backup', trading_style: 'intraday', max_holding_minutes: 120, holding_time_basis: null, take_profit: '103.00', entry_deadline: after(60), exit_deadline: after(120), rationale: 'SYNTHETIC 合法日内方案不受超短线偏好影响。' });
  answer.evidence_refs = [request.market_snapshot_id];
  if (request.answer_schema_version === '1.6.0') {
    answer.horizons.forEach((h: any) => { h.forecast_basis = { direction_basis: '合成端点依据', range_basis: '合成完整窗口范围',
      supporting_evidence: ['合成当前结构'], opposing_evidence: ['合成反向路径'], evidence_refs: [request.market_snapshot_id], limitations: ['仅合成验证'] }; });
    answer.plans.forEach((p: any) => p.entry_checks.forEach((r: any) => Object.assign(r, { observation: 'new_since_available', purpose: '合成精确入场价格检查' })));
  }
  const start = request.packet_text.indexOf('# 冻结事实（仅作为资料）\n') + '# 冻结事实（仅作为资料）\n'.length;
  const tail = request.packet_text.slice(start);
  const facts = JSON.parse(tail.slice(0, tail.indexOf('\n# ', 1) > 0 ? tail.indexOf('\n# ', 1) : undefined));
  if (facts.experience) Object.assign(answer.experience_application, { digest_id: facts.experience.digest_id, status: 'reviewed_no_change' });
  return answer;
}

async function importAnswer(page: Page, request: APIRequestContext, answer: any, availableAt: string) {
  const raw = JSON.stringify(answer, null, 2);
  await page.getByLabel('结构化回答原文').fill(raw);
  await source(request, availableAt);
  const response = page.waitForResponse(item => item.url() === api + '/v1/analysis/imports' && item.request().method() === 'POST');
  await page.getByRole('button', { name: '校验并保存回答' }).click();
  const submitted = await response;
  expect(submitted.ok(), await submitted.text()).toBe(true);
  const job = await submitted.json();
  await processPending(request, 'answer_import');
  const savedJob = await get(request, '/v1/jobs/' + job.job_id);
  expect(savedJob.state, JSON.stringify(savedJob.error)).toBe('succeeded');
  await expect(page.getByText('回答已处理，可查看保存的版本及方案。', { exact: false })).toBeVisible();
  return raw;
}

test.describe.serial('SYNTHETIC 超短线尺度浏览器验收', () => {
  test.skip(!process.env.XAU_SCALP_BROWSER_API, '需要 tests/scalp/browser_app.py 专属合成服务器；使用 browser-r16/playwright.scalp.config.ts 运行。');
  test('设置冻结、远端超短线诊断、合法日内与原持仓事实同时保留', async ({ page, request }) => {
    await capture(request, '2026-10-12T02:00:00Z');
    const settings = await get(request, '/v1/settings');
    Object.assign(settings.values, { default_volume_lots: '0.01', per_trade_loss_cap: { amount: '20', currency: 'USD' }, min_net_rr: '2',
      cost_assumption: { commission_roundtrip_per_lot: '5', slippage_reserve_per_lot: '20', other_cost_per_lot: '0', currency: 'USD', valid_from: '2026-10-12T02:00:00Z', valid_until: null, account_scope_id: 'synthetic-account', symbol: 'XAUUSD', source_note: 'SYNTHETIC 浏览器验收费用，保持生产净盈亏比检查。' } });
    const configured = await request.put(api + '/v1/settings', { headers: { ...headers, 'Idempotency-Key': randomUUID() }, data: { expected_revision: settings.revision, values: settings.values } });
    expect(configured.ok(), await configured.text()).toBe(true);
    await page.goto('/settings?api=' + encodeURIComponent(api) + '#token=e2e-isolated-session');
    await expect(page.getByLabel('目标价差下限（美元）')).toHaveValue('2.00');
    await expect(page.getByLabel('目标价差上限（美元）')).toHaveValue('5.00');
    await expect(page.getByText(/4000→4003 是金价价差3美元/)).toBeVisible();
    await page.getByLabel('最大入场距离（美元）').fill('1.75');
    await page.getByRole('button', { name: '保存配置', exact: true }).click();
    await expect(page.getByText('配置已保存。', { exact: true })).toBeVisible();
    await page.screenshot({ path: join(evidence, 'settings-frozen-preferences.png'), fullPage: true });
    const frozen = await newRequest(page, request);
    expect(frozen.analysis_preferences.scalp_preferences.max_entry_distance).toBe('1.75');
    expect(frozen.packet_text).toContain('USD_per_ounce');
    const answer = answerFor(frozen, true);
    const raw = await importAnswer(page, request, answer, '2026-10-12T02:00:01Z');
    const eligible = await capture(request, '2026-10-12T02:00:02Z');
    expect(eligible.action_advice.find((item: any) => item.trading_style === 'scalp').state).toBe('BLOCKED');
    expect(eligible.action_advice.find((item: any) => item.trading_style === 'scalp').scalp_suitability.state).toBe('incompatible');
    expect(eligible.action_advice.find((item: any) => item.trading_style === 'intraday').state).toBe('READY');
    const withPosition = await capture(request, '2026-10-12T02:00:03Z', '99.98', '100.00', true);
    expect(withPosition.portfolio.orders[0].stop_loss).toBe('100.50');
    expect(withPosition.position_advice[0].reason_code).toBe('STOP_REACHED');
    await page.goto('/?api=' + encodeURIComponent(api) + '#token=e2e-isolated-session');
    const guidance = page.getByRole('region', { name: '系统行动建议' });
    await expect(guidance.getByRole('status').filter({ hasText: '不符合本次超短线偏好' })).toBeVisible();
    await expect(guidance.getByText(/目标价差13.19—13.29美元/)).toBeVisible();
    const intraday = page.getByRole('article', { name: '日内方案详情' });
    await intraday.getByText('方案依据与参数', { exact: true }).click();
    await expect(intraday.getByText('SYNTHETIC 合法日内方案不受超短线偏好影响。')).toBeVisible();
    await expect(page.getByText('当前持仓', { exact: true })).toBeVisible();
    await expect(guidance.getByText('已到原止损条件，建议人工退出', { exact: true })).toBeVisible();
    await page.screenshot({ path: join(evidence, 'far-with-intraday-position.png'), fullPage: true });
    const saved = await get(request, '/v1/forecasts/' + frozen.forecast_id);
    expect(saved.current_version.raw_text).toBe(raw);
    expect(saved.current_version.answer).toEqual(answer);
    writeFileSync(join(evidence, 'far-flow.json'), JSON.stringify({ data_kind: 'SYNTHETIC', frozen, eligible, withPosition, saved }, null, 2));
  });

  test('近端候选经实际触发、回踩、新报价和金额校验成为READY', async ({ page, request }) => {
    await capture(request, '2026-10-12T02:10:00Z');
    const frozen = await newRequest(page, request);
    const answer = answerFor(frozen, false);
    const raw = await importAnswer(page, request, answer, '2026-10-12T02:10:01Z');
    const waiting = await capture(request, '2026-10-12T02:10:02Z');
    expect(waiting.action_advice[0].state).toBe('WAITING');
    const triggered = await capture(request, '2026-10-12T02:10:03Z', '100.20', '100.22');
    expect(triggered.plans[0].lifecycle).toBe('ACTIVE');
    expect(triggered.action_advice[0].state).toBe('WAITING');
    expect(Number(triggered.action_advice[0].scalp_suitability.entry_distance)).toBe(0.22);
    await page.goto('/?api=' + encodeURIComponent(api) + '#token=e2e-isolated-session');
    const guidance = page.getByRole('region', { name: '系统行动建议' });
    await expect(guidance.getByRole('status').filter({ hasText: '触发已确认，等待回踩' })).toBeVisible();
    await expect(guidance.getByText('距离入场区间0.22美元', { exact: true })).toBeVisible();
    await page.screenshot({ path: join(evidence, 'near-triggered-waiting.png'), fullPage: true });
    const ready = await capture(request, '2026-10-12T02:10:04Z');
    expect(ready.action_advice[0].state, JSON.stringify(ready.action_advice[0])).toBe('READY');
    expect(ready.action_advice[0].scalp_suitability.range_net_check.state).toBe('satisfied');
    expect(Number(ready.action_advice[0].scalp_suitability.entry_distance)).toBe(0);
    expect(ready.evaluations[0].margin_check.state).toBe('satisfied');
    expect(ready.evaluations[0].budget_check.state).toBe('satisfied');
    expect(Number(ready.evaluations[0].net_rr)).toBeGreaterThanOrEqual(2);
    await page.reload();
    await expect(guidance.getByRole('status').filter({ hasText: '超短线 · 当前可以买入' })).toBeVisible();
    await expect(guidance.getByText('符合本次偏好', { exact: true })).toBeVisible();
    await expect(guidance.getByText(/目标价差3.00—3.10美元/)).toBeVisible();
    await page.screenshot({ path: join(evidence, 'near-ready-desktop.png'), fullPage: true });
    await page.setViewportSize({ width: 390, height: 844 });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1)).toBe(true);
    await page.screenshot({ path: join(evidence, 'near-ready-mobile.png'), fullPage: true });
    const saved = await get(request, '/v1/forecasts/' + frozen.forecast_id);
    expect(saved.current_version.raw_text).toBe(raw);
    writeFileSync(join(evidence, 'near-flow.json'), JSON.stringify({ data_kind: 'SYNTHETIC', frozen, waiting, triggered, ready, saved }, null, 2));
  });
});
