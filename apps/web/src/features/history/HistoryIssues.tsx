import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { api, ApiProblem, type Schema } from '../../api/client';
import { downloadText } from '../../shared/format';
import { ResponseFailureNotice } from '../../shared/ResponseFailureNotice';
import { ExperienceEffectNotice } from '../../shared/ExperienceEffectNotice';

export function problemAdvice(code?: string | null, state?: string | null) {
  if (state === 'call_unknown' || state === 'outcome_unknown') return { title: '请求结果还不能确认', impact: '可能已经发送或产生费用，原未知记录保留。', action: '先核对这次调用的原始状态；不要直接重新发送。' };
  if (code === 'SCHEMA_INVALID' || code === 'ANSWER_INVALID' || code === 'IMPORT_INVALID') return { title: '回答收到，但格式不符合保存要求', impact: '这份回答没有成为可用预测，原文和失败记录保留。', action: '查看原回答和错误位置，或复制诊断摘要交给维护人员。' };
  if (code === 'EXPERIENCE_EFFECT_MISSING') return { title: '经验采用说明漏关联', impact: '回答中部分采用案例没有关联预测影响说明，原回答及失败记录保留。', action: '查看保存的原响应，核对漏关联的具体条目；不会自动补关联或重新导入。' };
  if (code === 'MODEL_RATE_LIMITED') return { title: '渠道暂时限制了本次请求', impact: '未获得可导入的完整回答，其他模型的记录保留。', action: '核对渠道返回与原调用状态；再次生成可能收费，不会自动重发。' };
  if (code === 'MODEL_UPSTREAM_FAILED') return { title: '渠道报告上游生成失败', impact: '本次没有保存可用预测，原响应和其他模型的记录保留。', action: '查看保存的原响应；具体上游原因尚不明确，不会自动重发或切换渠道。' };
  if (code === 'MODEL_INCOMPLETE') return { title: '收到部分响应，但无法确认回答完整', impact: '部分内容保留，没有当作完整预测导入。', action: '查看渠道原响应及结束状态；不要把片段当作成功回答。' };
  if (code === 'ENTRY_WINDOW_INSUFFICIENT') return { title: '回答的入场时间不足', impact: '原回答未通过时间校验，不会延长期限或改写原时间。', action: '查看原截止时间、准备与生成耗时，交由维护人员核对。' };
  if (code === 'SESSION_REQUIRED' || code === 'UNAUTHORIZED') return { title: '当前页面连接已失效', impact: '本页暂时不能读取服务，不代表历史记录丢失。', action: '通过正式启动器重新打开工作台。' };
  if (state === 'cancelled') return { title: '这次任务已经取消', impact: '已保存的原文与已完成项目保留；取消不等于费用为零。', action: '查看原任务状态；如要再次生成，需要重新确认费用授权。' };
  return { title: '这次处理未完成，原因还需核对', impact: '当前错误不足以确定修复方法，原记录和已完成子项保留。', action: '展开原错误，复制诊断摘要给维护人员；不要反复补发模型请求。' };
}

export function HistoryError({ error }: { error: unknown }) {
  if (!error) return null;
  const p = error instanceof ApiProblem ? error.problem : null;
  const advice = problemAdvice(p?.status === 401 ? 'UNAUTHORIZED' : p?.code);
  return <section className="notice error history-issue" role="alert"><strong>{advice.title}</strong><p>影响：{advice.impact}</p><p>可以这样做：{advice.action}</p><details><summary>原错误与诊断编号</summary><p>{error instanceof Error ? error.message : '错误内容未取得'}</p>{p && <><p>{p.code} · 追踪 {p.trace_id}</p>{p.field_errors.map((f, i) => <p key={i}>{f.path}：{f.message}</p>)}</>}</details></section>;
}

function SavedRaw({ callId }: { callId: string }) {
  const raw = useQuery({ queryKey: ['model-raw', callId], queryFn: () => api.modelRaw(callId) });
  return <><HistoryError error={raw.error} />{raw.isPending ? <p>正在只读加载保存原文…</p> : raw.data && <><ResponseFailureNotice value={raw.data.failure_diagnosis} /><ExperienceEffectNotice value={raw.data.experience_effect_diagnostics} /><details open><summary>保存的渠道原响应</summary><pre>{JSON.stringify(raw.data, null, 2)}</pre><button onClick={() => downloadText(`model-raw-${callId}.json`, JSON.stringify(raw.data, null, 2), 'application/json')}>下载原响应</button></details></>}</>;
}

export function HistoryIssue({ item }: { item: Schema['DailyHistoryItem'] }) {
  const [raw, setRaw] = useState(false); const [copy, setCopy] = useState('');
  const review = item.state === 'unknown';
  const advice = review ? {
    title: item.review_outcome === 'DATA_INCOMPLETE' ? '到期了，但核验资料不完整' : '到期了，尚无可核验的方向结果',
    impact: '当前不能确定方向对错，保留未知；不会显示成0分或方向错误。',
    action: item.review_outcome ? '查看原复盘原因。跨休市、缺资料或观察不完整需分别核对，有新证据后再补算。' : '查看对应预测及复盘状态；如尚未补算，可在页面的复盘任务区登记一次补算。'
  } : problemAdvice(item.error_code, item.state);
  const text = [`模型：${item.model_id}`, `请求：${item.request_id}`, `调用：${item.call_id ?? '无'}`, `复盘：${item.review_id ?? '无'}`, `状态：${item.state}`, `错误：${item.error_code ?? '未记录'}`, item.error_detail ?? '', ...item.review_reasons].filter(Boolean).join('\n');
  async function copySummary() { try { await navigator.clipboard.writeText(text); setCopy('诊断摘要已复制，没有调用模型。'); } catch { setCopy('浏览器未允许复制，请在下面选中诊断摘要复制。'); } }
  return <article className="history-issue"><h3>{advice.title}</h3><p><strong>影响：</strong>{advice.impact}</p><p><strong>可以这样做：</strong>{advice.action}</p><div className="actions"><button onClick={copySummary}>复制诊断摘要</button>{item.call_id && <button onClick={() => setRaw(!raw)}>{raw ? '收起渠道原响应' : '查看保存的渠道原响应'}</button>}</div>{copy && <p role="status">{copy}</p>}<details open={copy.startsWith('浏览器') || undefined}><summary>原错误、原复盘原因与诊断摘要</summary><pre>{text}</pre></details>{raw && item.call_id && <SavedRaw callId={item.call_id} />}<p className="muted">查看与复制不收费；再次生成可能收费。本页不会自动修补、重试或切换渠道。</p></article>;
}
