import { useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { api, type Schema } from '../../api/client';
import { Badge, Empty, ErrorNotice, Field, PageControls } from '../../shared/ui';
import { downloadText, label, money, time } from '../../shared/format';
import { HistoryError } from '../history/HistoryIssues';

export function reviewResultText(review: Schema['Review']) {
  const state = review.diagnostics?.session.state;
  if (review.outcome === 'DATA_INCOMPLETE' && state === 'market_closed') return '跨休市，未作全窗评价';
  if (review.outcome === 'DATA_INCOMPLETE' && state === 'mixed') return '休市及待核对缺数';
  if (state === 'schedule_conflict') return `${label(review.outcome)} · 时段证据冲突`;
  return label(review.outcome);
}

function netResultText(review: Schema['Review']) {
  if (review.net_pnl) return money(review.net_pnl.amount, review.net_pnl.currency);
  if (review.target_kind === 'reference_plan' && ['NOT_TRIGGERED', 'EXPIRED_WITHOUT_ENTRY'].includes(review.outcome)) return '不适用（未入场）';
  return '未知';
}

function ReviewDiagnostics({ review }: { review: Schema['Review'] }) {
  const diagnostic = review.diagnostics;
  if (!diagnostic) return null;
  const score = diagnostic.alignment_metrics.find(m => m.name === 'direction_correct');
  const result = score?.state === 'available' ? (score.value === '1' ? '正确' : '错误')
    : score?.state === 'not_applicable' ? '方向未判断，不计对错' : '资料不足，未评分';
  return <section aria-label="评分基准与交易时段核对">
    <h4>评分基准与交易时段核对</h4>
    <p>{diagnostic.direction_basis === 'legacy_complete_m1' ? '原评分采用完整M1裁剪起止价，保留原结果。' : '本请求方向评分采用冻结Bid与原端点前的新鲜Bid。'}</p>
    <p>冻结Bid方向核对：{result}。这是方向分项，不代表范围、阶段或交易收益。</p>
    <p>核对时间 {time(diagnostic.computed_at)}；原复盘可用时间不变。</p>
    {diagnostic.reasons.map((reason,i) => <p key={`reason-${i}`}>{reason}</p>)}
    <p>完整分钟 {diagnostic.session.observed_minutes}/{diagnostic.session.expected_minutes}；符合公布休市的缺失分钟 {diagnostic.session.closed_missing_minutes}；交易时段待核对缺失分钟 {diagnostic.session.unexplained_missing_minutes}。</p>
    {diagnostic.session.reasons.map((reason,i) => <p key={`session-${i}`}>{reason}</p>)}
    {diagnostic.session.schedule_url && <p>时段依据：<a href={diagnostic.session.schedule_url} target="_blank" rel="noreferrer">券商公布时段</a>；核对于 {time(diagnostic.session.schedule_checked_at)}。</p>}
    <details><summary>冻结起点、端点与缺失原因</summary><pre>{JSON.stringify(diagnostic.alignment_metrics,null,2)}</pre></details>
  </section>;
}

export function ReviewList({ kind }: { kind: Schema['Review']['target_kind'] }) {
  const queryClient = useQueryClient();
  const [cursor, setCursor] = useState<string>(); const [outcome, setOutcome] = useState(''); const [method, setMethod] = useState(''); const [target, setTarget] = useState(''); const [selected, setSelected] = useState<Schema['Review'] | null>(null);
  const reviews = useQuery({ queryKey: ['reviews', kind, cursor, target], queryFn: () => api.reviews(kind, cursor, target || undefined) });
  const detail = useQuery({ queryKey: ['review-detail', selected?.review_id, selected?.revision], enabled: !!selected, queryFn: () => api.review(selected!.review_id, selected!.revision) });
  const shown = detail.data?.review_id === selected?.review_id && detail.data?.revision === selected?.revision ? detail.data : selected;
  const visible = reviews.data?.items.filter(item => (!outcome || item.outcome === outcome) && (!method || item.method_version.includes(method)));
  return <section className="panel"><div className="section-heading"><h2>{label(kind)}复盘</h2><Badge>{kind === 'reference_plan' ? '参考模拟结果' : kind === 'actual_trade' ? '券商成交事实' : '行情描述性评价'}</Badge></div>{kind === 'forecast' && <p className="muted">行情预测评价方向、区间、阶段与转折，不计算交易盈亏。</p>}<div className="filters"><Field label="对象 ID"><input value={target} onChange={event => { setTarget(event.target.value); setCursor(undefined); }} placeholder="可选：精确查询对象" /></Field><Field label="结果（本页）"><select value={outcome} onChange={event => setOutcome(event.target.value)}><option value="">全部结果</option>{['EVALUATED', 'NOT_TRIGGERED', 'EXPIRED_WITHOUT_ENTRY', 'TP', 'SL', 'TIME_EXIT', 'AMBIGUOUS', 'DATA_INCOMPLETE', 'ACTUAL_CLOSED', 'ACTUAL_OPEN'].map(value => <option value={value} key={value}>{label(value)}</option>)}</select></Field><Field label="方法版本（本页）"><input value={method} onChange={event => setMethod(event.target.value)} placeholder="按方法版本筛选" /></Field></div><HistoryError error={reviews.error} />{visible?.length ? <div className="table-scroll"><table><thead><tr><th>评价窗口</th><th>结果</th>{kind !== 'forecast' && <th>{kind === 'reference_plan' ? '参考净结果' : '实际净结果'}</th>}<th>覆盖</th><th>方法 / 修订</th><th /></tr></thead><tbody>{visible.map(review => <tr key={`${review.review_id}:${review.revision}`}><td>{time(review.window_start)}<br /><small>至 {time(review.window_end)}</small></td><td><Badge tone={['AMBIGUOUS', 'DATA_INCOMPLETE'].includes(review.outcome) ? 'warn' : 'neutral'}>{reviewResultText(queryClient.getQueryData<Schema['Review']>(['review-detail', review.review_id, review.revision]) ?? review)}</Badge></td>{kind !== 'forecast' && <td>{netResultText(review)}</td>}<td>{label(review.coverage)}</td><td>{review.method_version}<br /><small>修订 #{review.revision} · {review.source_kind === 'synthetic' ? '合成样例' : review.source_kind === 'mt5' ? 'MT5 事实' : '来源待核对'}</small></td><td><button className="text-button" onClick={() => setSelected(review)}>详情</button></td></tr>)}</tbody></table></div> : <Empty title={reviews.isPending ? '正在读取复盘…' : '暂无符合条件的复盘'}>未知结果不会补为零；未触发和到期未入场单独记录。</Empty>}<PageControls next={reviews.data?.next_cursor} loading={reviews.isFetching} onFirst={() => setCursor(undefined)} onNext={() => setCursor(reviews.data!.next_cursor!)} />{selected && <div className="review-detail"><div className="row-between"><h3>{label(selected.outcome)} · 修订 #{selected.revision}</h3><button className="text-button" onClick={() => setSelected(null)}>收起详情</button></div><p>实际覆盖 {time(selected.evaluated_start)} — {time(selected.evaluated_end)}</p><p>结果可用时间 {time(selected.available_at)} · {selected.method_version}</p>{selected.reasons.length > 0 && <ul>{selected.reasons.map((reason, index) => <li key={index}>{reason}</li>)}</ul>}<HistoryError error={detail.error} />{detail.isFetching && <p>正在只读核对保存的报价与交易时段…</p>}{shown && <ReviewDiagnostics review={shown} />}<h4>原保存的分项评价</h4>{selected.metrics.length ? <><div className="table-scroll"><table><thead><tr><th>分项</th><th>原结果</th></tr></thead><tbody>{selected.metrics.filter(m => m.segment_index == null).map((m,i) => <tr key={i}><td>{({ direction_correct:'方向是否正确', direction:'实际方向', predicted_direction:'原预测方向', actual_low:'实际最低价', actual_high:'实际最高价', predicted_low:'预测下边界', predicted_high:'预测上边界', range_contained:'区间是否覆盖', range_boundary_absolute_error:'两端边界偏差合计', predicted_range_width:'预测区间宽度', actual_range_width:'实际区间宽度', delta:'实际价格变化' } as Record<string,string>)[m.name] ?? `原分项：${m.name}`}</td><td>{m.state === 'not_applicable' ? '不适用' : m.state !== 'available' ? '未知 / 尚不可核验' : m.unit === 'label' && m.observed_label ? label(m.observed_label) : m.value === null ? '未知 / 尚不可核验' : m.name === 'direction_correct' || m.name === 'range_contained' ? m.value === '1' ? '是' : '否' : ['direction','predicted_direction'].includes(m.name) ? label(m.value) : `${m.value}${m.unit === 'price' ? ' 美元/盎司' : ''}`}</td></tr>)}</tbody></table></div><details><summary>全部原始分项（含阶段与转折）</summary><pre>{JSON.stringify(selected.metrics, null, 2)}</pre></details></> : <p>暂无分项评价。</p>}<details><summary>固定输入与来源引用</summary><pre>{JSON.stringify({ inputs: selected.inputs, source_refs: selected.source_refs }, null, 2)}</pre></details><button onClick={() => downloadText(`review-${selected.review_id}-r${selected.revision}.json`, JSON.stringify(selected, null, 2), 'application/json')}>导出所选复盘</button></div>}</section>;
}
