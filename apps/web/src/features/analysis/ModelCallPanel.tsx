import { useEffect, useRef, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import { api, type Schema } from '../../api/client';
import { Badge, ErrorNotice } from '../../shared/ui';
import { time } from '../../shared/format';
import { ResponseFailureNotice } from '../../shared/ResponseFailureNotice';
import { ExperienceEffectNotice } from '../../shared/ExperienceEffectNotice';

const names: Record<Schema['ModelCall']['state'], string> = { queued: '排队中', waiting: '等待模型回答', receiving: '正在接收', validating: '正在校验并保存', succeeded: '处理完成', failed: '调用失败', cancelled: '已取消', outcome_unknown: '结果未知' };
const active = (state?: string) => !state || ['queued', 'waiting', 'receiving', 'validating'].includes(state);

export function ModelCallPanel({ callId }: { callId: string }) {
  const cache = useQueryClient(); const handled = useRef('');
  const [showRaw, setShowRaw] = useState(false);
  const query = useQuery({ queryKey: ['model-call', callId], queryFn: () => api.modelCall(callId), refetchInterval: q => active(q.state.data?.state) ? 1000 : false });
  const raw = useQuery({ queryKey: ['model-raw', callId], queryFn: () => api.modelRaw(callId), enabled: showRaw });
  const cancel = useMutation({ mutationFn: () => api.cancelJob(query.data!.job_id), onSuccess: () => { void cache.invalidateQueries({ queryKey: ['model-call', callId] }); void cache.invalidateQueries({ queryKey: ['request-model-call'] }); } });
  const call = query.data;
  const validation = useQuery({ queryKey: ['model-call-errors', call?.job_id], queryFn: () => api.job(call!.job_id), enabled: call?.state === 'failed' && call.answer_valid === false });
  const fields = validation.data?.error?.field_errors ?? [];
  useEffect(() => {
    if (call && !active(call.state) && handled.current !== call.call_id + call.state) {
      handled.current = call.call_id + call.state;
      void cache.invalidateQueries();
    }
  }, [call, cache]);
  if (!call) return <><p role="status">正在恢复模型任务…</p><ErrorNotice error={query.error} /></>;
  const seconds = call.started_at && call.ended_at ? Math.max(0, (Date.parse(call.ended_at) - Date.parse(call.started_at)) / 1000) : null;
  return <section className="job model-call" aria-label="模型调用状态" aria-live="polite">
    <div className="row-between"><strong>{names[call.state]}</strong><Badge tone={call.state === 'succeeded' ? 'good' : call.state === 'outcome_unknown' || call.state === 'failed' ? 'warn' : 'neutral'}>{call.protocol === 'responses' ? 'Responses' : call.protocol === 'anthropic_messages' ? 'Anthropic Messages' : 'Chat Completions'} · {call.model_requested}</Badge></div>
    <p>{call.purpose === 'probe' ? (call.prompt_version === 'synthetic-integrity/2' ? '字符保真合成测试 · SYNTHETIC' : '旧版合成连接测试 · SYNTHETIC') : '冻结资料分析'} · 推理档位 {call.reasoning_effort}{call.protocol === 'anthropic_messages' ? '（未发送此 OpenAI 参数）' : ''} · 创建于 {time(call.created_at)}{seconds !== null && ` · 总处理 ${seconds.toFixed(1)} 秒`}</p>
    {call.detail && <p role={call.state === 'failed' || call.state === 'outcome_unknown' ? 'alert' : undefined}>{call.detail}</p>}
    <p>响应接收：{call.transport_succeeded ? '传输已结束（不代表生成成功）' : call.raw_response ? '未取得成功完整响应' : '待接收'} · 回答校验：{call.answer_valid === true ? '通过' : call.answer_valid === false ? '未通过' : '未确认'}{call.candidate_count !== null && ` · 候选 ${call.candidate_count} 个`}</p>
    {call.state === 'failed' && call.answer_valid === false && <div className="notice error">
      <strong>{call.purpose === 'probe' ? '合成测试未通过，原文已保留' : '回答未导入，原文已保留'}</strong>
      {validation.isPending && <p role="status">正在读取字段校验详情…</p>}
      {fields.length > 0 && <details open><summary>回答字段错误（{fields.length} 项）</summary><ul>{fields.map((field, index) => <li key={index}><code>{field.path}</code>：{field.message}</li>)}</ul></details>}
      {validation.data && fields.length === 0 && <p>{validation.data.error?.detail ?? '此记录没有逐字段详情，请结合错误摘要检查原回答。'}</p>}
      <p>{call.purpose === 'probe' ? '不会自动修补或重发。确认原因后，可在设置页主动重新测试；测试可能再次收费。' : '不会自动修补或重发。确认原因后再决定手工导入新回答或明确重试；重试可能再次收费。'}</p>
    </div>}
    {call.version_id && <p className="notice success">预测版本已保存。当前是否可入场请看<Link to="/">工作台行动建议</Link>；候选为零也是合法结果。</p>}
    <p className="muted">用量：输入 {call.input_tokens ?? '未知'} / 输出 {call.output_tokens ?? '未知'} token · 费用估算：{call.estimated_cost === null ? '未知' : `${call.estimated_cost} ${call.cost_currency ?? ''}`}。{call.cost_source && `价格来源：${call.cost_source}。`}中转站返回模型：{call.model_returned ?? '未知'}（供应商声明）。</p>
    <div className="actions">{active(call.state) && <button disabled={cancel.isPending} onClick={() => cancel.mutate()}>取消 API 调用</button>}{call.raw_response && <button onClick={() => setShowRaw(v => !v)}>{showRaw ? '收起原文' : '查看模型原响应'}</button>}</div>
    {active(call.state) && <p className="muted">关闭页面后继续处理。取消仅停止本地接收及自动导入，不保证中转站停止或不计费。</p>}
    {call.state === 'outcome_unknown' && <p className="notice">不会自动重发。请先核对中转站记录，也可保留此请求，改用下方手工导入。</p>}
    {showRaw && raw.data && <><ResponseFailureNotice value={raw.data.failure_diagnosis} /><ExperienceEffectNotice value={raw.data.experience_effect_diagnostics} /><details open><summary>完整原响应与提取回答</summary><pre className="packet">{raw.data.raw_response ?? '尚未保存'}</pre><pre className="packet">{raw.data.answer ?? '尚未提取回答'}</pre></details></>}
    <ErrorNotice error={query.error ?? cancel.error ?? raw.error ?? validation.error} />
  </section>;
}

export function RequestModelPanel({ requestId, available }: { requestId: string; available: boolean }) {
  const cache = useQueryClient(); const [acknowledged, setAcknowledged] = useState(false);
  const lookup = useQuery({ queryKey: ['request-model-call', requestId], queryFn: () => api.requestModelCall(requestId) });
  const config = useQuery({ queryKey: ['model-settings'], queryFn: api.modelSettings });
  const start = useMutation({ mutationFn: (retry: boolean) => api.startModelAnalysis(requestId, { retry_of: retry ? lookup.data?.call?.call_id ?? null : null, acknowledge_possible_charge: retry && acknowledged }), onSuccess: result => { cache.setQueryData(['request-model-call', requestId], result); setAcknowledged(false); } });
  const call = lookup.data?.call;
  const failed = Boolean(call && ['failed', 'outcome_unknown', 'cancelled'].includes(call.state));
  const canCall = available && config.data?.values.enabled && config.data.key_configured && config.data.capability_verified;
  return <section className="panel" aria-label="通过 API 分析"><div className="section-heading"><h2>通过 API 分析</h2><Badge>{config.data?.capability_verified ? '当前配置已通过字符保真测试' : '当前配置字符保真待验证'}</Badge></div>
    <p>使用此请求的冻结资料。完整回答通过同一套校验后保存版本，行情读取继续运行。</p>
    {!config.data?.values.enabled || !config.data.key_configured || !config.data.capability_verified ? <p><Link to="/settings">在设置页配置密钥、模型并完成生成测试</Link>。复制和手工导入始终可用。</p> : null}
    {!call && !lookup.data?.version_id && <button className="primary" disabled={!canCall || start.isPending || lookup.isPending} onClick={() => start.mutate(false)}>通过 API 分析此请求</button>}
    {call && <ModelCallPanel key={call.call_id} callId={call.call_id} />}
    {failed && !lookup.data?.version_id && <div className="actions"><label className="checkbox"><input type="checkbox" checked={acknowledged} onChange={event => setAcknowledged(event.target.checked)} />已核对原因，确认重试可能再次收费</label><button disabled={!canCall || start.isPending || (call?.stage !== 'not_sent' && !acknowledged)} onClick={() => start.mutate(true)}>明确重试此请求</button></div>}
    <ErrorNotice error={lookup.error ?? start.error ?? config.error} />
  </section>;
}
