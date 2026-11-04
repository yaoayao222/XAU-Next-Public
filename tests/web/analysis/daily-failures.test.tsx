import React from 'react';
import { afterEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { api, type Schema } from '../../../apps/web/src/api/client';
import { DailyResults } from '../../../apps/web/src/features/analysis/DailyResults';

const clients: QueryClient[] = [];
afterEach(() => { cleanup(); clients.forEach(c => c.clear()); clients.length = 0; vi.restoreAllMocks(); });

function group(kind: 'validation' | 'rate' | 'unknown' | 'success'): Schema['DailyGroup'] {
  const stamp = '2026-10-28T07:09:04.739Z';
  const ref = { id: 'SYNTHETIC-raw', sha256: 'a'.repeat(64), size: 20 };
  const call = { state: 'failed', call_id: 'SYNTHETIC-call', job_id: 'SYNTHETIC-job',
    transport_succeeded: kind === 'validation', answer_valid: kind === 'validation' ? false : null,
    raw_response: ref, answer: kind === 'validation' ? ref : null,
    error_code: kind === 'validation' ? 'ENTRY_WINDOW_INSUFFICIENT' : kind === 'rate' ? 'MODEL_RATE_LIMITED' : 'MODEL_OUTCOME_UNKNOWN',
    detail: 'SYNTHETIC 原始错误摘要' } as Schema['ModelCall'];
  return { group_id: 'SYNTHETIC-round', kind: 'new', base_group_id: null, created_at: stamp, market_cutoff: stamp,
    window_end: '2026-10-28T10:09:04.739Z', horizon_minutes: [30, 60, 180], source_scope: 'SYNTHETIC-scope',
    data_kind: 'SYNTHETIC', package_kind: 'candidate24', template_version: 'manual-analysis/24', bundle_id: null,
    context_sha256: 'a'.repeat(64), same_question: true, models: ['A'], state: 'finished', run_registered: true,
    arms: [{ model_key: 'A', model_id: 'SYNTHETIC-model', request_id: 'SYNTHETIC-request', forecast_id: 'SYNTHETIC-forecast',
      packet_sha256: 'a'.repeat(64), state: kind === 'unknown' ? 'outcome_unknown' : kind === 'success' ? 'succeeded' : 'failed',
      version_id: null, model_call: kind === 'success' ? null : call, available_at: null, summary: null,
      horizons: [], plans: [], guidance: {},
      entry_timing_diagnostics: kind === 'validation' ? [{ plan_index: 0, trading_style: 'scalp',
        entry_deadline: '2026-10-28T07:29:04.739Z', minimum_entry_deadline: '2026-10-28T07:30:00Z',
        max_entry_deadline: '2026-10-28T07:39:04.739Z', shortfall_seconds: '55.261', exceeds_seconds: '0' }] : [] }] };
}

function show(kind: Parameters<typeof group>[0]) {
  const c = new QueryClient({ defaultOptions: { queries: { retry: false } } }); clients.push(c);
  return render(<QueryClientProvider client={c}><MemoryRouter><DailyResults group={group(kind)} /></MemoryRouter></QueryClientProvider>);
}

it('完整回答校验失败显示精确缺口，主动查看原文才只读加载，不新增版本或发送', async () => {
  const raw = vi.spyOn(api, 'modelRaw').mockResolvedValue({ call_id: 'SYNTHETIC-call', raw_response: 'SYNTHETIC raw', answer: 'SYNTHETIC preserved answer' });
  show('validation');
  expect(screen.getByText('回答未通过检查，未重发')).toBeTruthy();
  expect(screen.getByText(/少留了 55.261 秒/)).toBeTruthy();
  expect(screen.getByText(/最低观察所需截止/)).toBeTruthy();
  expect(screen.getByText(/原回答和期限未修改/)).toBeTruthy();
  expect(screen.queryByText('回答已保存')).toBeNull(); expect(raw).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: '查看未通过的原回答' }));
  expect(await screen.findByText('SYNTHETIC preserved answer')).toBeTruthy();
  await waitFor(() => expect(raw).toHaveBeenCalledTimes(1));
  expect(screen.queryByRole('button', { name: /重试|重发|切换/ })).toBeNull();
});

it('429显示渠道限流及无分析回答，不声称已经完成分析', () => {
  const raw = vi.spyOn(api, 'modelRaw'); show('rate');
  expect(screen.getByText('渠道限流，未重发')).toBeTruthy();
  expect(screen.getByText(/未取得分析回答/)).toBeTruthy();
  expect(screen.queryByLabelText('入场时间检查详情')).toBeNull();
  expect(screen.queryByText('回答未通过检查，未重发')).toBeNull();
  expect(raw).not.toHaveBeenCalled();
});

it('结果未知继续显示未知，成功回答维持已保存状态', () => {
  const view = show('unknown'); expect(screen.getByText('结果未知，未重发')).toBeTruthy();
  expect(screen.queryByText('渠道限流，未重发')).toBeNull();
  view.unmount(); show('success'); expect(screen.getByText('回答已保存')).toBeTruthy();
});

it('重复字段说明相同或冲突，点击位置才读取原文且原失败不变', async () => {
  const preserved = '{\n  "analysis_basis":{},\n  "analysis_basis":{}\n}';
  const raw = vi.spyOn(api, 'modelRaw').mockResolvedValue({ call_id: 'SYNTHETIC-call', raw_response: 'SYNTHETIC stream', answer: preserved });
  const send = vi.spyOn(api, 'startModelAnalysis'); const imported = vi.spyOn(api, 'importAnswer');
  const g = group('validation');
  g.arms[0].model_call!.error_code = 'SCHEMA_INVALID'; g.arms[0].entry_timing_diagnostics = [];
  g.arms[0].duplicate_field_diagnostics = { version: 'json-duplicates/1', total: 1, truncated: false, items: [{ path: '/analysis_basis', field: 'analysis_basis', label: '分析依据', first: { line: 2, column: 3, character: 5 }, duplicate: { line: 3, column: 3, character: 28 }, values_relation: 'identical' }] };
  const before = JSON.stringify(g);const c = new QueryClient({ defaultOptions: { queries: { retry: false } } }); clients.push(c);
  render(<QueryClientProvider client={c}><MemoryRouter><DailyResults group={g} /></MemoryRouter></QueryClientProvider>);
  expect(screen.getByText('回答出现重复字段，未通过检查')).toBeTruthy();
  expect(screen.getByText(/两处内容相同；仍不能自动删除/)).toBeTruthy();
  expect(raw).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: '查看重复位置 · 第3行' }));
  expect(await screen.findByText(/已定位第3行/)).toBeTruthy();
  const selected = document.querySelector('.selected-line');expect(selected?.id.endsWith('-line-3')).toBe(true);
  expect(document.querySelectorAll('.duplicate-mark')).toHaveLength(2);
  fireEvent.click(screen.getByRole('button', { name: '查看首次位置 · 第2行' }));
  await waitFor(() => expect(document.querySelector('.selected-line')?.id.endsWith('-line-2')).toBe(true));
  expect(raw).toHaveBeenCalledTimes(1);expect(send).not.toHaveBeenCalled();expect(imported).not.toHaveBeenCalled();
  expect(JSON.stringify(g)).toBe(before);expect(screen.queryByText('回答已保存')).toBeNull();
});


it('上游失败直接说明渠道错误；旧失败点击后核查且不覆盖原记录', async () => {
  const g = group('validation'); const call = g.arms[0].model_call!;
  g.arms[0].entry_timing_diagnostics = []; call.answer = null; call.error_code = 'MODEL_INCOMPLETE'; call.detail = '原缺少结束标记';
  const before = JSON.stringify(g);
  const diagnosis = { code: 'MODEL_UPSTREAM_FAILED' as const, answer_text_received: false, detail: '渠道报告上游生成失败。本次未收到答案正文，未保存可用预测；原响应已保留，不会自动重发。具体上游原因尚不明确。' };
  const raw = vi.spyOn(api, 'modelRaw').mockResolvedValue({ call_id: call.call_id, raw_response: 'SYNTHETIC saved stream', answer: null, failure_diagnosis: diagnosis });
  const send = vi.spyOn(api, 'startModelAnalysis'); const imported = vi.spyOn(api, 'importAnswer');
  const c = new QueryClient({ defaultOptions: { queries: { retry: false } } }); clients.push(c);
  render(<QueryClientProvider client={c}><MemoryRouter><DailyResults group={g} /></MemoryRouter></QueryClientProvider>);
  expect(raw).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: '查看渠道原响应' }));
  expect(await screen.findByLabelText('原响应只读核查')).toBeTruthy();
  expect(screen.getByText('渠道生成失败，未重发')).toBeTruthy();
  expect(screen.getByText(/本次未收到答案正文/)).toBeTruthy();
  expect(screen.getByText(/原错误、失败记录及原文未改写/)).toBeTruthy();
  expect(screen.getByText(/原缺少结束标记/)).toBeTruthy();
  fireEvent.click(screen.getByRole('button', { name: '收起失败原文' }));
  fireEvent.click(screen.getByRole('button', { name: '查看渠道原响应' }));
  expect(raw).toHaveBeenCalledTimes(1); expect(send).not.toHaveBeenCalled(); expect(imported).not.toHaveBeenCalled();
  expect(JSON.stringify(g)).toBe(before); expect(screen.queryByText('回答已保存')).toBeNull();
});

it('新上游错误显示专用状态，不混淆回答格式失败', () => {
  const g = group('validation'); g.arms[0].entry_timing_diagnostics=[];
  g.arms[0].model_call!.error_code='MODEL_UPSTREAM_FAILED'; g.arms[0].model_call!.answer=null;
  const c=new QueryClient();clients.push(c);
  render(<QueryClientProvider client={c}><MemoryRouter><DailyResults group={g} /></MemoryRouter></QueryClientProvider>);
  expect(screen.getByText('渠道生成失败，未重发')).toBeTruthy();
  expect(screen.queryByText('回答未通过检查，未重发')).toBeNull();
});


it('经验漏关联明确显示7/4/3和第2、5、7条，点击才读取且不写回', async () => {
  const g=group('validation');g.arms[0].entry_timing_diagnostics=[];
  const call=g.arms[0].model_call!;call.error_code='EXPERIENCE_EFFECT_MISSING';call.detail='原通用经验错误';
  const before=JSON.stringify(g);
  const raw=vi.spyOn(api,'modelRaw').mockResolvedValue({call_id:call.call_id,raw_response:'SYNTHETIC channel',answer:'SYNTHETIC original answer',experience_effect_diagnostics:{version:'experience-effect-diagnostic/1',adopted_count:7,linked_count:4,missing_count:3,detail:'采用7条，缺3条',missing_cases:[2,5,7].map(n=>({case_number:n,review_ref:`SYNTHETIC-review-${n}`,reason:`原采用理由${n}`}))}});
  const send=vi.spyOn(api,'startModelAnalysis');const imported=vi.spyOn(api,'importAnswer');
  const c=new QueryClient({defaultOptions:{queries:{retry:false}}});clients.push(c);
  render(<QueryClientProvider client={c}><MemoryRouter><DailyResults group={g}/></MemoryRouter></QueryClientProvider>);
  expect(screen.getByText('经验关联不完整，未重发')).toBeTruthy();expect(raw).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button',{name:'查看未通过的原回答'}));
  expect(await screen.findByText('采用7条经验，漏关联3条')).toBeTruthy();
  expect(screen.getByText(/已关联4条/)).toBeTruthy();
  for(const n of [2,5,7])expect(screen.getByText(`第${n}条经验尚未关联影响说明`)).toBeTruthy();
  expect(screen.getByText(/原通用经验错误/)).toBeTruthy();
  expect(screen.getByText(/按保存原文只读核查/)).toBeTruthy();
  expect(JSON.stringify(g)).toBe(before);expect(raw).toHaveBeenCalledTimes(1);expect(send).not.toHaveBeenCalled();expect(imported).not.toHaveBeenCalled();
});
