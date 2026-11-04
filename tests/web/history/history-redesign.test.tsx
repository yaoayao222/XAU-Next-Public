import React from 'react';
import { afterEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { api, type Schema } from '../../../apps/web/src/api/client';
import { HistoryPage } from '../../../apps/web/src/features/history/HistoryPage';
import { DailyPerformancePanel } from '../../../apps/web/src/features/history/DailyPerformancePanel';
import { HistoryIssue, problemAdvice } from '../../../apps/web/src/features/history/HistoryIssues';
import { workspace, version } from '../fixtures';

afterEach(() => { cleanup(); vi.restoreAllMocks(); localStorage.clear(); });
const counts = { correct: 1, incorrect: 0, uncertain: 0, pending: 0, unknown: 0, failed: 1, cancelled: 0, call_unknown: 0, not_sent: 0, processing: 0 };
function item(state: Schema['DailyHistoryItem']['state'] = 'correct'): Schema['DailyHistoryItem'] {
  return { item_id: state, row_id: 'row1', request_id: 'request1', forecast_id: 'forecast1', version_id: state === 'failed' ? null : 'version1', group_id: 'group1', kind: 'new', same_question: true, market_cutoff: workspace.as_of, available_at: state === 'failed' ? null : workspace.as_of, end_at: workspace.as_of, model_id: 'gpt-6.1-sol', template_version: 'manual-analysis/26', minutes: 30, provider: 'SYNTHETIC', method_version: 'prediction_review/3', state, direction: 'long', predicted_low: '100', predicted_high: '110', actual_low: '102', actual_high: '108', range_error: '2', review_id: null, review_revision: null, review_outcome: null, review_reasons: [], call_id: state === 'failed' ? 'call1' : null, error_code: state === 'failed' ? 'SCHEMA_INVALID' : null, error_detail: state === 'failed' ? '原错误：括号不匹配，第1行第3058列' : null, call_state: state === 'failed' ? 'failed' : 'succeeded' };
}
function performance(): Schema['DailyPerformance'] {
  return { as_of: workspace.as_of, records_considered: 2, legacy_records_included: 0, message: 'SYNTHETIC', rows: [{ row_id: 'row1', model_id: 'gpt-6.1-sol', template_version: 'manual-analysis/26', minutes: 30, source_scope: 'SYNTHETIC', data_kind: 'SYNTHETIC', method_version: 'prediction_review/3', configuration: 'SYNTHETIC 配置', evaluation_scope: 'version_descriptive', records: 2, ended: 2, pending: 0, clear: 1, uncertain: 0, scored: 1, correct: 1, incorrect: 0, unknown: 1, failed: 1, not_sent: 0, range_scored: 1, range_covered: 1, range_mae: '2', range_width: '10', non_overlapping_records: 1, overlapping_records: 0, note: '同源演示', display_counts: counts }], history: { items: [{ round_key: 'group1', group_id: 'group1', market_cutoff: workspace.as_of, template_version: 'manual-analysis/26', kind: 'new', same_question: true, state_counts: counts, saved_items: 1, items: [item(), item('failed')] }], next_cursor: null, total_rounds: 1, matching_rounds: 1, evaluation_items: 2, saved_requests: 1, answer_versions: 1, issue_records: 1 } };
}
function mount(child: React.ReactNode) { return render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><MemoryRouter>{child}</MemoryRouter></QueryClientProvider>); }
function mocks() {
  vi.spyOn(api, 'workspace').mockResolvedValue({ ...workspace, daily_group: null });
  return vi.spyOn(api, 'dailyPerformance').mockImplementation(async query => {
    const value = performance();
    if (query.template === 'manual-analysis/25') { value.rows = []; value.history = { ...value.history!, items: [], evaluation_items: 0, issue_records: 0 }; }
    if (query.history_view === 'issues') value.history!.items[0].items = [item('failed')];
    if (query.history_status) value.history!.items[0].items = [item(query.history_status)];
    return value;
  });
}

it('loads a concise overview without raw answers, old research or review writes', async () => {
  const score = mocks(); const raw = vi.spyOn(api, 'modelRaw'); const answer = vi.spyOn(api, 'version');
  const old = vi.spyOn(api, 'forecasts'); const research = vi.spyOn(api, 'analysisLab'); const reviews = vi.spyOn(api, 'reviews'); const refresh = vi.spyOn(api, 'refreshReviews');
  mount(<HistoryPage />);
  expect(await screen.findByText(/能核验的1个明确判断中，1个方向正确/)).toBeTruthy();
  expect(score.mock.calls[0][0].template).toBe('manual-analysis/26');
  expect(score.mock.calls[0][0].include_history).toBe(true);
  expect(raw).not.toHaveBeenCalled(); expect(answer).not.toHaveBeenCalled(); expect(old).not.toHaveBeenCalled(); expect(research).not.toHaveBeenCalled(); expect(reviews).not.toHaveBeenCalled(); expect(refresh).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: '方向正确 1条，查看记录' }));
  await waitFor(() => expect(score.mock.lastCall?.[0].history_status).toBe('correct'));
  expect(score.mock.lastCall?.[0].history_row).toBe('row1');
});

it('reads a saved answer only after opening that model, retaining exact version identity', async () => {
  mocks(); const answer = vi.spyOn(api, 'version').mockResolvedValue(version); const raw = vi.spyOn(api, 'modelRaw');
  mount(<DailyPerformancePanel />);
  fireEvent.click(await screen.findByRole('button', { name: '展开本轮' }));
  fireEvent.click(screen.getAllByRole('button', { name: '查看依据' })[0]);
  expect(answer).not.toHaveBeenCalled(); expect(raw).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: '查看原回答', exact: true }));
  await waitFor(() => expect(answer).toHaveBeenCalledWith('forecast1', 'version1'));
  expect(await screen.findByText(version.raw_text)).toBeTruthy();
});

it('separates issues from normal uncertainty and never supplies a paid retry action', async () => {
  const score = mocks(); const raw = vi.spyOn(api, 'modelRaw'); const send = vi.spyOn(api, 'startModelAnalysis');
  mount(<DailyPerformancePanel />);
  fireEvent.click(await screen.findByRole('tab', { name: '待处理 · 1项' }));
  await waitFor(() => expect(score.mock.lastCall?.[0].history_view).toBe('issues'));
  fireEvent.click(await screen.findByText(/查看处理方法/));
  expect(await screen.findByText('回答收到，但格式不符合保存要求')).toBeTruthy();
  expect(screen.getByText(/未判断方向、未到期和正常等待不算故障/)).toBeTruthy();
  expect(raw).not.toHaveBeenCalled(); expect(send).not.toHaveBeenCalled();
  expect(screen.queryByRole('button', { name: /重试|重新发送/ })).toBeNull();
});

it('keeps selected period across remount and clears list cursor on a scope change', async () => {
  const score = mocks(); const first = mount(<DailyPerformancePanel />);
  fireEvent.change(screen.getByLabelText('查看预测周期'), { target: { value: '180' } });
  await waitFor(() => expect(score.mock.lastCall?.[0].minutes).toBe(180));
  first.unmount(); mount(<DailyPerformancePanel />);
  expect((screen.getByLabelText('查看预测周期') as HTMLSelectElement).value).toBe('180');
  fireEvent.change(screen.getByLabelText('查看分析包'), { target: { value: 'manual-analysis/25' } });
  await waitFor(() => expect(score.mock.lastCall?.[0].template).toBe('manual-analysis/25'));
  expect(score.mock.lastCall?.[0].history_cursor).toBeUndefined();
  expect(await screen.findByText('当前范围暂无可评价记录')).toBeTruthy();
});

it('shows reference and real-trade flows separately without reading either on overview', async () => {
  mocks(); const reviews = vi.spyOn(api, 'reviews').mockResolvedValue({ items: [], next_cursor: null }); const trades = vi.spyOn(api, 'trades').mockResolvedValue({ items: [], next_cursor: null });
  mount(<HistoryPage />);
  fireEvent.click(screen.getByRole('tab', { name: '参考方案', exact: true }));
  await waitFor(() => expect(reviews).toHaveBeenCalledWith('reference_plan', undefined, undefined));
  expect(trades).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('tab', { name: '实际交易', exact: true }));
  await waitFor(() => expect(trades).toHaveBeenCalled());
  expect(await screen.findByText(/完整净结果缺资料时保持未知/)).toBeTruthy();
});

it('does not pretend an unknown failure or unknown transmission has a known cure', () => {
  expect(problemAdvice('UNRECOGNIZED').title).toContain('原因还需核对');
  expect(problemAdvice(null, 'call_unknown').impact).toContain('可能已经发送或产生费用');
  expect(problemAdvice('MODEL_RATE_LIMITED').action).toContain('不会自动重发');
  mount(<HistoryIssue item={{ ...item('unknown'), review_outcome: null }} />);
  expect(screen.getByText('到期了，尚无可核验的方向结果')).toBeTruthy();
  expect(screen.getByText(/尚未补算/)).toBeTruthy();
});


it('历史上游错误提供中文解释，原响应核查保留旧错误且没有发送操作', async () => {
  expect(problemAdvice('MODEL_UPSTREAM_FAILED').title).toBe('渠道报告上游生成失败');
  const record=item('failed');record.error_code='MODEL_INCOMPLETE';record.error_detail='原缺少结束标记';
  const before=JSON.stringify(record);
  const raw=vi.spyOn(api,'modelRaw').mockResolvedValue({call_id:'call1',raw_response:'SYNTHETIC preserved stream',answer:null,failure_diagnosis:{code:'MODEL_UPSTREAM_FAILED',answer_text_received:false,detail:'渠道报告上游生成失败。本次未收到答案正文，原响应已保留。'}});
  const send=vi.spyOn(api,'startModelAnalysis');mount(<HistoryIssue item={record} />);
  expect(raw).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button',{name:'查看保存的渠道原响应'}));
  expect(await screen.findByLabelText('原响应只读核查')).toBeTruthy();
  expect(screen.getByText(/原错误、失败记录及原文未改写/)).toBeTruthy();
  expect(JSON.stringify(record)).toBe(before);expect(send).not.toHaveBeenCalled();
  expect(screen.queryByRole('button',{name:/重试|重新发送/})).toBeNull();
});


it('经验关联错误给出具体处理说明，原件诊断按需读取', async () => {
  expect(problemAdvice('EXPERIENCE_EFFECT_MISSING').title).toBe('经验采用说明漏关联');
  const record=item('failed');record.error_code='EXPERIENCE_EFFECT_MISSING';record.error_detail='原经验错误';
  const before=JSON.stringify(record);const raw=vi.spyOn(api,'modelRaw').mockResolvedValue({call_id:'call1',raw_response:'SYNTHETIC channel',answer:'SYNTHETIC preserved answer',experience_effect_diagnostics:{version:'experience-effect-diagnostic/1',adopted_count:7,linked_count:4,missing_count:3,detail:'缺少3条关联',missing_cases:[2,5,7].map(n=>({case_number:n,review_ref:`SYNTHETIC-${n}`,reason:'原采用理由'}))}});
  const send=vi.spyOn(api,'startModelAnalysis');mount(<HistoryIssue item={record}/>);
  expect(raw).not.toHaveBeenCalled();fireEvent.click(screen.getByRole('button',{name:'查看保存的渠道原响应'}));
  expect(await screen.findByLabelText('经验关联只读核查')).toBeTruthy();
  expect(screen.getByText('采用7条经验，漏关联3条')).toBeTruthy();expect(send).not.toHaveBeenCalled();expect(JSON.stringify(record)).toBe(before);
});
