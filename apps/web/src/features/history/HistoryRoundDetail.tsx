import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import { api, type Schema } from '../../api/client';
import { decimalDisplay, downloadText, label, time } from '../../shared/format';
import { VersionHistory } from '../analysis/VersionHistory';
import { HistoryError, HistoryIssue } from './HistoryIssues';

export const directionStates: Record<Schema['DailyHistoryItem']['state'], string> = {
  correct: '方向正确', incorrect: '方向错误', uncertain: '未判断方向', pending: '窗口未结束', unknown: '待核验',
  failed: '回答未保存', cancelled: '已取消', call_unknown: '调用结果未知', not_sent: '尚未发送', processing: '正在处理',
};

function SavedAnswer({ item }: { item: Schema['DailyHistoryItem'] }) {
  const query = useQuery({ queryKey: ['version', item.forecast_id, item.version_id], queryFn: () => api.version(item.forecast_id, item.version_id!) });
  return <><HistoryError error={query.error} />{query.data ? <><pre>{query.data.raw_text}</pre><button onClick={() => downloadText(`answer-${item.version_id}.json`, query.data!.raw_text, 'application/json')}>下载原回答</button></> : <p>正在只读加载原回答…</p>}</>;
}

function SavedReview({ item }: { item: Schema['DailyHistoryItem'] }) {
  const query = useQuery({ queryKey: ['review-detail', item.review_id, item.review_revision], queryFn: () => api.review(item.review_id!, item.review_revision!) });
  return <><HistoryError error={query.error} />{query.data && <><p>原结果：{label(query.data.outcome)} · {query.data.method_version} · 修订{query.data.revision}</p>{query.data.reasons.map((r, i) => <p key={i}>{r}</p>)}<details><summary>分项评分、时间与来源</summary><pre>{JSON.stringify(query.data, null, 2)}</pre></details><button onClick={() => downloadText(`review-${item.review_id}.json`, JSON.stringify(query.data, null, 2), 'application/json')}>导出所选复盘</button></>}</>;
}

export function RangeComparison({ item }: { item: Schema['DailyHistoryItem'] }) {
  const values = [item.predicted_low, item.predicted_high, item.actual_low, item.actual_high];
  const complete = values.every(v => v !== null && Number.isFinite(Number(v)));
  const numbers = values.map(Number); const low = Math.min(...numbers); const high = Math.max(...numbers); const span = high - low;
  const position = (v: number) => span > 0 ? 5 + (v - low) / span * 90 : 50;
  return <section aria-label="原预测区间与实际区间"><h4>预测范围和实际范围</h4><p>预测：{item.predicted_low ?? '未知'} — {item.predicted_high ?? '未知'}；实际：{item.actual_low ?? '未知'} — {item.actual_high ?? '未知'} 美元/盎司。</p>{complete && <div className="history-range" role="img" aria-label={`预测${item.predicted_low}至${item.predicted_high}，实际${item.actual_low}至${item.actual_high}，单位美元每盎司`}><div className="history-range-axis"><span>{decimalDisplay(String(low))}</span><span>黄金价格（美元/盎司）</span><span>{decimalDisplay(String(high))}</span></div>{[0, 2].map(i => <div className="history-range-lane" key={i}><span>{i === 0 ? '预测' : '实际'}</span><div><i className={i === 0 ? 'predicted' : 'actual'} style={{ left: `${position(numbers[i])}%`, width: `${Math.max(0.8, position(numbers[i + 1]) - position(numbers[i]))}%` }} /></div></div>)}</div>}<p>上下边界平均偏差：{item.range_error === null ? '尚无可核验结果' : `${decimalDisplay(item.range_error)}美元/盎司`}。</p><small>实际区间沿原内部闭合M1口径，不代表每一笔报价；区间覆盖与方向对错分别评价，不是账户盈亏。</small></section>;
}

export function HistoryRoundDetail({ round }: { round: Schema['DailyHistoryRound'] }) {
  const [selected, setSelected] = useState<string>(); const [answer, setAnswer] = useState(false); const [review, setReview] = useState(false); const [versions, setVersions] = useState(false);
  const item = round.items.find(i => i.item_id === selected);
  return <div className="history-round-detail"><p>资料截止 {time(round.market_cutoff)} · {round.kind === 'update' ? '窗口内更新，各自使用上一版' : round.same_question ? '同批共同资料，回答可用时间可能不同' : '旧独立记录'}。</p><div className="table-scroll"><table><thead><tr><th>模型 / 周期</th><th>回答可用时间</th><th>方向结果</th><th>依据</th></tr></thead><tbody>{round.items.map(i => <tr key={i.item_id}><td>{i.model_id}<small>{i.minutes}分钟 · {i.provider ?? '渠道未记录'} · {i.method_version}</small></td><td>{i.available_at ? time(i.available_at) : '没有可用回答'}<small>原端点 {time(i.end_at)}</small></td><td>{directionStates[i.state]}</td><td><button className="text-button" onClick={() => { setSelected(i.item_id); setAnswer(false); setReview(false); setVersions(false); }}>查看依据</button></td></tr>)}</tbody></table></div>{round.group_id && <Link to={`/analysis?group=${round.group_id}`}>打开这轮完整分析与回答</Link>}{item && <section className="history-selected"><div className="row-between"><h3>{item.model_id} · {item.minutes}分钟</h3><button className="text-button" onClick={() => setSelected(undefined)}>收起依据</button></div><p>冻结 {time(item.market_cutoff)} → 回答可用 {time(item.available_at)} → 原端点 {time(item.end_at)}。</p><p>原方向：{item.direction ? label(item.direction) : '无可用回答'} · {directionStates[item.state]}。</p>{['failed', 'cancelled', 'call_unknown', 'unknown'].includes(item.state) && <HistoryIssue key={item.item_id} item={item} />}{item.state === 'uncertain' && <p className="notice">模型没有判断明确方向，这是正常分析结果；区间仍可独立评价，无需因此重试。</p>}{item.state === 'pending' && <p className="notice">还没到原端点 {time(item.end_at)}，现在无需反复补算。</p>}<RangeComparison item={item} /><div className="actions">{item.version_id && <button onClick={() => setAnswer(!answer)}>{answer ? '收起原回答' : '查看原回答'}</button>}{item.review_id && <button onClick={() => setReview(!review)}>{review ? '收起原复盘' : '查看原复盘与来源'}</button>}{item.version_id && <button onClick={() => setVersions(!versions)}>{versions ? '收起版本记录' : '查看更新版本与对照'}</button>}</div>{answer && <SavedAnswer item={item} />}{review && <SavedReview item={item} />}{versions && <VersionHistory forecastId={item.forecast_id} />}<p className="muted">整窗评价包含生成等待，不代表收到回答后可执行的交易效果；更新与重叠窗口不作为独立样本。</p></section>}</div>;
}
