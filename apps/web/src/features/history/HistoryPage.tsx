import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { api, ApiProblem, type Schema } from '../../api/client';
import { useWorkspace } from '../../api/queries';
import { Badge, Empty, ErrorNotice, Field, JobPanel, PageControls, PageHeading, SourceLine } from '../../shared/ui';
import { downloadText, label, money, time } from '../../shared/format';
import { VersionHistory } from '../analysis/VersionHistory';
import { ReviewList } from '../review/ReviewList';
import { DailyPerformancePanel } from './DailyPerformancePanel';
import { HistoryError } from './HistoryIssues';
import { AnalysisLabPanel } from '../analysis/AnalysisLabPanel';

function Trades() {
  const [cursor, setCursor] = useState<string>(); const [tradeId, setTradeId] = useState(''); const workspace = useWorkspace();
  const query = useQuery({ queryKey: ['trades', cursor], queryFn: () => api.trades(cursor) });
  const detail = useQuery({ queryKey: ['trade', tradeId], queryFn: () => api.trade(tradeId), enabled: Boolean(tradeId) });
  return <section className="panel"><h2>成交记录</h2>{workspace.data?.portfolio?.trades_status ? <SourceLine status={workspace.data.portfolio.trades_status} /> : <p className="muted">尚未取得当前成交历史的分项读取状态。</p>}<p className="muted">已知收费小计与完整净值分别展示，未关联成交不会自动归入某个方案。</p><HistoryError error={query.error ?? detail.error} />{query.data?.items.length ? <div className="table-scroll"><table><thead><tr><th>交易</th><th>状态</th><th>已知已实现小计</th><th>完整净结果</th><th>方案关联</th></tr></thead><tbody>{query.data.items.map(trade => <tr key={trade.trade_id}><td><button className="text-button" onClick={() => setTradeId(trade.trade_id)}>{trade.symbol} {label(trade.side)} · {time(trade.opened_at)} · {trade.source_kind === 'synthetic' ? '合成样例' : trade.source_kind === 'mt5' ? 'MT5 事实' : '来源待核对'}</button></td><td>{trade.state === "unknown" ? "状态待核对" : label(trade.state)}</td><td>{money(trade.known_realized_subtotal.amount, trade.known_realized_subtotal.currency)}</td><td>{trade.complete_realized_net ? money(trade.complete_realized_net.amount, trade.complete_realized_net.currency) : '未知 / 尚未完整'}</td><td>{trade.plan_id ? '已关联' : '未关联'}</td></tr>)}</tbody></table></div> : <Empty title="暂无真实成交记录">没有记录不代表未发生交易；以账户历史读取结果为准。</Empty>}{detail.data && <details open><summary>所选交易的原始成交与收费</summary><pre>{JSON.stringify(detail.data, null, 2)}</pre><button onClick={() => downloadText(`trade-${tradeId}.json`, JSON.stringify(detail.data, null, 2), 'application/json')}>导出所选交易</button></details>}<PageControls next={query.data?.next_cursor} loading={query.isFetching} onFirst={() => setCursor(undefined)} onNext={() => setCursor(query.data!.next_cursor!)} /></section>;
}
function LegacyForecastRecords() {
  const [cursor, setCursor] = useState<string>(); const [selected, setSelected] = useState<string>();
  const [search, setSearch] = useState(''); const [date, setDate] = useState('');
  const forecasts = useQuery({ queryKey: ['forecasts', cursor], queryFn: () => api.forecasts(cursor) });
  const visible = forecasts.data?.items.filter(item => (!search || item.summary.includes(search) || item.forecast_id.includes(search)) && (!date || new Intl.DateTimeFormat('sv-SE', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit' }).format(new Date(item.window_start)) === date));
  return <section className="panel history-old-forecast"><h2>原预测历史与全部版本</h2><div className="filters"><Field label="文字或预测 ID（本页）"><input value={search} onChange={e => setSearch(e.target.value)} /></Field><Field label="开始日期（本页，北京时间）"><input type="date" value={date} onChange={e => setDate(e.target.value)} /></Field></div><HistoryError error={forecasts.error} />{visible?.length ? <div className="history-list">{visible.map(item => <button className="history-item" key={item.forecast_id} onClick={() => setSelected(item.forecast_id)}><span>{time(item.window_start)} — {time(item.window_end)}</span><strong>{item.summary}</strong><small>版本修订 #{item.revision} · 可用 {time(item.available_at)}</small></button>)}</div> : <Empty title={forecasts.isPending ? '正在读取历史…' : '本页没有符合条件的预测'} />}<PageControls next={forecasts.data?.next_cursor} loading={forecasts.isFetching} onFirst={() => setCursor(undefined)} onNext={() => setCursor(forecasts.data!.next_cursor!)} />{selected && <VersionHistory key={selected} forecastId={selected} />}</section>;
}

function ReviewTasks() {
  const cache = useQueryClient(); const workspace = useWorkspace(); const [jobId, setJobId] = useState<string>();
  const active = workspace.data?.active_jobs.filter(j => j.kind === 'review_refresh') ?? [];
  const running = active.filter(j => j.state !== 'queued'); const displayed = running.length ? running : active.slice(0, 1);
  const queued = active.filter(j => j.state === 'queued').length;
  const ids = [...new Set([...displayed.map(j => j.job_id), ...(jobId && !active.some(j => j.job_id === jobId) ? [jobId] : [])])];
  const refresh = useMutation({ mutationFn: () => api.refreshReviews({ forecast_id: null, through: workspace.data!.as_of }), onSuccess: job => { setJobId(job.job_id); void cache.invalidateQueries(); } });
  return <section className="panel history-review-tasks"><div className="row-between"><h2>复盘补算与任务</h2><button onClick={() => refresh.mutate()} disabled={refresh.isPending || !workspace.data || active.length > 0}>{refresh.isPending ? '正在受理…' : active.length ? '正在补算…' : '补算到期复盘（不调用模型）'}</button></div><p>只补算到期且尚未完成的项目；缺资料的旧结果等待新证据，不会重新生成模型回答。</p><HistoryError error={refresh.error} />{ids.map(id => <ReviewTask key={id} jobId={id} />)}{queued > 0 && <p role="status">另有 {queued} 个补算任务等待处理，已完成项目会自动跳过。</p>}<details><summary>什么时候需要补算？</summary><p>资料不足项保留未知并等待新证据，不再定时循环补算。窗口未结束、正常未判断方向无需反复点击。只有同源历史资料或真实成交事实改变时才重新核对，结果未变不新增修订；已保存的子项保留。</p></details></section>;
}

function ReviewTask({ jobId }: { jobId: string }) {
  const query = useQuery({ queryKey: ['job', jobId], queryFn: () => api.job(jobId) });
  return <>{query.data?.state === 'failed' ? <HistoryError error={query.data.error ? new ApiProblem(query.data.error) : new Error(query.data.message)} /> : <JobPanel jobId={jobId} />}{query.data?.state === 'failed' && <div className="notice"><strong>本次复盘任务未完成</strong><p>已保存项目保留。先展开原错误核对原因；原因未知时请复制任务编号 {jobId} 与原错误给维护人员。这里补算不会调用模型，不要因此重复发送分析。</p></div>}</>;
}

export function HistoryPage() {
  const workspace = useWorkspace(); const [kind, setKind] = useState<Schema['Review']['target_kind']>('forecast');
  const [legacy, setLegacy] = useState(false); const [reviews, setReviews] = useState(false); const [research, setResearch] = useState(false); const [tasks, setTasks] = useState(false);
  const active = workspace.data?.active_jobs.some(j => j.kind === 'review_refresh');
  return <div className="history-redesign"><PageHeading eyebrow="HISTORY & REVIEW" title="历史与复盘">先看结果，再看依据；有问题时知道下一步怎么做。</PageHeading><div className="tabs history-category-tabs" role="tablist" aria-label="复盘类别">{(['forecast', 'reference_plan', 'actual_trade'] as const).map(value => <button role="tab" aria-selected={kind === value} key={value} onClick={() => setKind(value)}>{value === 'forecast' ? '行情预测' : value === 'reference_plan' ? '参考方案' : '实际交易'}</button>)}</div>{active && <div className="notice" role="status">已有复盘任务正在处理，刷新页面后进度仍保留。</div>}{active && <ReviewTasks />}{kind === 'forecast' ? <DailyPerformancePanel tasks={<button onClick={() => setTasks(true)}>查看复盘任务与补算说明</button>} /> : <><section className="notice history-category-intro"><h2>{kind === 'reference_plan' ? '参考方案：与真实成交分开看' : '实际交易：以真实成交和费用为准'}</h2><p>{kind === 'reference_plan' ? '未触发、到期未入场、证据不足和可评价结果分别保留；没有入场不是亏损，参考结果不代表真实成交。' : '只看真实成交、方案关联和已知费用；完整净结果缺资料时保持未知，不显示成0。MT5只读，交易由你人工完成。'}</p></section>{kind === 'actual_trade' && <Trades />}<ReviewList key={kind} kind={kind} /></>}<details className="panel history-tools" open={tasks} onToggle={e => setTasks(e.currentTarget.open)}><summary>复盘任务与补算说明（不调用模型）</summary><p>正常未到期无需反复补算；补算会保存新的复盘结果，不修改原预测。</p>{!active && tasks && <ReviewTasks />}</details><details className="panel history-tools"><summary>更多资料：原预测、分项复盘、旧研究与经验引用</summary><div className="actions"><button aria-expanded={legacy} onClick={() => setLegacy(!legacy)}>原预测历史与更新版本</button><button aria-expanded={reviews} onClick={() => setReviews(!reviews)}>原分项复盘与导出</button><button aria-expanded={research} onClick={() => setResearch(!research)}>以前的独立研究资料与成绩</button></div>{legacy && <LegacyForecastRecords />}{reviews && <ReviewList kind="forecast" />}{research && <AnalysisLabPanel archiveOnly />}<h3>经验如何回到下一轮</h3><p>新分析包只引用行情截止前已经可用的复盘，保留可评价、未知及未触发数量。可在完整分析包核对原引用；本页不调用模型生成总结。</p></details></div>;
}
