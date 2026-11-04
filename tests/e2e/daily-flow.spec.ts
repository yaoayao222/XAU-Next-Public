import { test, expect } from '@playwright/test';
import { readFileSync } from 'node:fs';
const api = 'http://127.0.0.1:18765';
const headers = { Authorization: 'Bearer e2e-isolated-session' };

function makeAnswer(req: any) {
  const value = JSON.parse(readFileSync('fixtures/analysis-answer.experience.json','utf8'));
  value.schema_version = req.answer_schema_version;
  if (['1.5.0','1.6.0'].includes(req.answer_schema_version)) value.plans.forEach((plan: any) => { plan.holding_time_basis = null; });
  for (const key of ['request_id','forecast_id','base_version_id','market_snapshot_id','market_cutoff']) value[key] = req[key];
  const add = (minutes: number) => new Date(Date.parse(req.window_start) + minutes * 60000).toISOString();
  value.horizons = req.horizon_minutes.filter((n: number) => Date.parse(add(n)) > Date.parse(req.market_cutoff)).map((n: number) => ({
    horizon_minutes: n, end_at: add(n), direction:'uncertain', expected_range:{low:'98.00',high:'104.00'},
    phases:[{start_at:req.market_cutoff,end_at:add(n),direction:'uncertain',note:'合成浏览器验收'}],turning_points:[],confidence:null
  }));
  if (req.answer_schema_version === '1.6.0') {
    value.horizons.forEach((h: any) => { h.forecast_basis = { direction_basis: '合成端点方向判断', range_basis: '合成完整窗口范围',
      supporting_evidence: ['合成当前结构'], opposing_evidence: ['合成反向路径'], evidence_refs: [req.market_snapshot_id], limitations: ['未验证真实质量'] }; });
    value.plans.forEach((p: any) => p.entry_checks.forEach((r: any) => Object.assign(r, { observation: 'new_since_available', purpose: '合成独立入场检查' })));
  }
  value.plans[0].valid_from = req.market_cutoff;
  value.plans[0].entry_deadline = add(30);
  value.plans[0].exit_deadline = add(60);
  value.evidence_refs = [req.market_snapshot_id];
  return JSON.stringify(value, null, 2);
}

test('AC02–09 real SPA + HTTP + DB import, update and reopen', async ({ page, request }) => {
  const errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.goto('/?api='+encodeURIComponent(api)+'#token=e2e-isolated-session');
  await expect(page.getByRole('heading',{name:'工作台',exact:true})).toBeVisible();
  await expect(page.getByText('合成样例数据 · 用于功能验收，不是实际市场报价')).toBeVisible();
  await page.getByRole('link',{name:'新分析',exact:true}).click();
  await page.getByRole('button',{name:'新建分析请求',exact:true}).click();
  await expect(page.getByRole('button',{name:'复制完整分析包'})).toBeEnabled();
  let id = new URL(page.url()).searchParams.get('request')!;
  let req = await (await request.get(api+'/v1/analysis/requests/'+id,{headers})).json();
  const raw = makeAnswer(req);
  await page.getByLabel('结构化回答原文').fill(raw);
  await page.getByRole('button',{name:'校验并保存回答'}).click();
  await expect(page.getByText('回答已处理，可查看保存的版本及方案。',{exact:false})).toBeVisible();
  let saved = await (await request.get(api+'/v1/forecasts/'+req.forecast_id,{headers})).json();
  expect(saved.current_version.raw_text).toBe(raw);
  await page.getByRole('link',{name:'返回工作台 →'}).click();
  await expect(page.getByRole('heading',{name:'候选方案'})).toBeVisible();
  await expect(page.locator('.plan-card').getByText('等待入场条件', { exact: true })).toBeVisible();
  await expect(page.locator('.plan-card').getByText('入场方式：回踩限价', { exact: true })).toBeVisible();
  const guidance = page.getByRole('region', { name: '系统行动建议' });
  await expect(guidance.getByText('候选 · 等待条件')).toBeVisible();
  await expect(guidance.getByText('等待触发及触发后全程检查。')).toBeVisible();
  await expect(guidance.getByText('缺少短线结构止损和目标空间的依据。')).toBeVisible();
  await page.screenshot({path:`${process.env.XAU_NEXT_E2E_SCREENSHOTS ?? 'test-results'}/workspace.png`,fullPage:true});
  await page.reload();
  await expect(page.locator('.plan-card').getByText('入场方式：回踩限价', { exact: true })).toBeVisible();
  await expect(page.locator('.plan-card details.plan-basis')).not.toHaveAttribute('open', '');
  const accepted = page.waitForResponse(r => new URL(r.url()).pathname === '/v1/market/refresh' && r.request().method() === 'POST');
  await page.getByRole('button',{name:'刷新事实',exact:true}).click();
  const refreshed = await (await accepted).json();
  await expect.poll(async () => (await (await request.get(api+'/v1/jobs/'+refreshed.job_id,{headers})).json()).state).toBe('succeeded');
  await expect(page.getByText('上次读取已完成',{exact:true})).toBeVisible();
  await page.getByRole('link',{name:'查看版本与更新 ↗'}).click();
  await page.getByRole('button',{name:'更新分析',exact:true}).click();
  await expect(page.getByRole('button',{name:'复制完整分析包'})).toBeEnabled();
  id = new URL(page.url()).searchParams.get('request')!;
  req = await (await request.get(api+'/v1/analysis/requests/'+id,{headers})).json();
  await page.getByLabel('结构化回答原文').fill(makeAnswer(req));
  await page.getByRole('button',{name:'校验并保存回答'}).click();
  await expect(page.getByText('回答已处理，可查看保存的版本及方案。',{exact:false})).toBeVisible();
  saved = await (await request.get(api+'/v1/forecasts/'+req.forecast_id,{headers})).json();
  expect(saved.current_version.sequence).toBe(2);
  expect(saved.forecast.window_end).toBe(req.window_end);
  expect(errors).toEqual([]);
});
