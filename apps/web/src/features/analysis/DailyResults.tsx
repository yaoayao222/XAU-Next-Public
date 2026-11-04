import { useState } from 'react';
import { Link } from 'react-router-dom';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { api, type Schema } from '../../api/client';
import { Badge, ErrorNotice, JobPanel } from '../../shared/ui';
import { time } from '../../shared/format';
import { AnswerView } from './VersionHistory';
import { PlanCard } from '../plans/PlanCard';
import { GuidancePanel } from '../workspace/GuidancePanel';
import { DuplicateFieldNotice, LocatedAnswer } from './DuplicateAnswer';
import { ResponseFailureNotice } from '../../shared/ResponseFailureNotice';
import { ExperienceEffectNotice } from '../../shared/ExperienceEffectNotice';

const states: Record<string, string> = { prepared: '尚未发送', expired: '资料已过期', queued: '等待发送', waiting: '等待模型回答', receiving: '接收回答中', validating: '校验保存中', succeeded: '回答已保存', failed: '失败，未重发', cancelled: '已取消', outcome_unknown: '结果未知，未重发' };
const directions = { long: '看涨', short: '看跌', neutral: '持平', uncertain: '不明确' };
function Result({ arm, group, workspace }: { arm: Schema['DailyArm']; group: Schema['DailyGroup']; workspace?: Schema['Workspace'] }) {
  const [details, setDetails] = useState(false); const [showRaw, setShowRaw] = useState(false); const cache = useQueryClient();
  const [targetLine, setTargetLine] = useState<number>();
  const version = useQuery({ queryKey: ['version', arm.forecast_id, arm.version_id], queryFn: () => api.version(arm.forecast_id, arm.version_id!), enabled: details && !!arm.version_id, staleTime: Infinity });
  const call = arm.model_call;
  const raw = useQuery({ queryKey: ['model-raw', call?.call_id], queryFn: () => api.modelRaw(call!.call_id), enabled: showRaw && !!call?.raw_response, staleTime: Infinity });
  const label = arm.state !== 'failed' ? states[arm.state]
    : call?.error_code === 'EXPERIENCE_EFFECT_MISSING' ? '经验关联不完整，未重发'
    : call?.error_code === 'MODEL_UPSTREAM_FAILED' || (showRaw && raw.data?.failure_diagnosis) ? '渠道生成失败，未重发'
    : call?.answer && call.transport_succeeded ? '回答未通过检查，未重发'
    : call?.error_code === 'MODEL_RATE_LIMITED' ? '渠道限流，未重发'
    : call?.transport_succeeded ? '响应已收到，回答未保存' : '请求未成功，未重发';
  const branch = workspace ? { ...workspace, ...arm.guidance, plans: arm.plans, position_advice: [], notifications: [] } : null;
  return <article className="daily-model-card" aria-label={`${arm.model_id}分析结果`}>
    <div className="row-between"><strong>{arm.model_id}</strong><Badge tone={arm.state === 'succeeded' ? 'good' : 'warn'}>{label}</Badge></div>
    <p className="muted">{group.template_version} · {arm.reasoning_effort ?? 'high'} · 可用时间 {time(arm.available_at)}</p>
    <p className="muted">{!arm.model_call && arm.selected_provider ? `所选渠道：${arm.selected_provider}（尚未发送）` : `渠道：${arm.provider ?? arm.model_call?.provider ?? '旧记录未保存渠道'}`}{arm.protocol && ` · ${arm.protocol}`}{arm.max_output_tokens && ` · 输出上限 ${arm.max_output_tokens} token`}</p>
    {arm.base_url && <details><summary>本次连接地址</summary><p>{arm.base_url}</p></details>}
    {arm.version_id && <p className="muted">回答来源：{arm.answer_source === 'model_api' ? 'API接收' : '手工导入（模型信息为填写记录）'} · 回答模型 {arm.answer_model_id ?? '未记录，成绩归入未知来源'}</p>}
    {arm.summary && <p className="daily-summary">{arm.summary}</p>}
    {!!arm.horizons.length && <div className="daily-horizons">{arm.horizons.map(h => <div key={h.minutes}><strong>{h.minutes}分钟 · {directions[h.direction]}</strong><span>{h.low} — {h.high}</span><small>原端点 {time(h.end_at)}</small></div>)}</div>}
    {arm.duplicate_field_diagnostics ? <DuplicateFieldNotice value={arm.duplicate_field_diagnostics} onLocate={line => { setTargetLine(line); setShowRaw(true); }} /> : arm.model_call?.error_code && <p className="notice">{arm.model_call.detail} <small>{arm.model_call.error_code}</small></p>}
    {arm.state === 'failed' && <p className="muted">{call?.answer ? '原回答已保留，未生成可用预测版本；不会自动修补或重发。' : call?.error_code === 'MODEL_RATE_LIMITED' ? '渠道拒绝了本次请求，未取得分析回答；具体限额原因尚未确认，不会自动重试或切换连接。' : '本次没有保存可用预测版本；原失败记录保留，不会自动重发。'}</p>}
    {!!arm.entry_timing_diagnostics?.length && <div className="notice" aria-label="入场时间检查详情">
      <strong>入场时间不足或超过时效</strong>
      {arm.entry_timing_diagnostics.map(row => <dl key={row.plan_index}>
        <dt>候选 {row.plan_index + 1} · {row.trading_style === 'scalp' ? '短线' : '日内'}</dt>
        <dd>原回答入场截止：{time(row.entry_deadline)}</dd>
        <dd>最低观察所需截止：{time(row.minimum_entry_deadline)}</dd>
        <dd>原分析时效上限：{time(row.max_entry_deadline)}</dd>
        {Number(row.shortfall_seconds) > 0 && <dd>少留了 {Number(row.shortfall_seconds).toLocaleString('zh-CN', { maximumFractionDigits: 3 })} 秒</dd>}
        {Number(row.exceeds_seconds) > 0 && <dd>超过时效 {Number(row.exceeds_seconds).toLocaleString('zh-CN', { maximumFractionDigits: 3 })} 秒</dd>}
      </dl>)}
      <p>按原冻结资料重算，仅说明失败原因；原回答和期限未修改。</p>
    </div>}
    {arm.model_call && ['queued', 'waiting', 'receiving', 'validating'].includes(arm.state) && <JobPanel jobId={arm.model_call.job_id} onComplete={() => { void cache.invalidateQueries({ queryKey: ['daily-groups'] }); void cache.invalidateQueries({ queryKey: ['daily-group'] }); }} />}
    <div className="actions"><Link className="button" to={`/analysis?group=${group.group_id}&request=${arm.request_id}`}>分析包／手工导入</Link>{arm.version_id && <button onClick={() => setDetails(!details)}>{details ? '收起完整回答' : '查看完整回答与原文'}</button>}{arm.state === 'failed' && call?.raw_response && <button onClick={() => setShowRaw(!showRaw)}>{showRaw ? '收起失败原文' : call.answer ? '查看未通过的原回答' : '查看渠道原响应'}</button>}</div>
    {showRaw && <><ErrorNotice error={raw.error} />{raw.isPending && <p role="status">正在读取已保存原文…</p>}<ResponseFailureNotice value={raw.data?.failure_diagnosis} /><ExperienceEffectNotice value={raw.data?.experience_effect_diagnostics} />{raw.data && (arm.duplicate_field_diagnostics && raw.data.answer ? <LocatedAnswer text={raw.data.answer} value={arm.duplicate_field_diagnostics} line={targetLine} /> : <details open><summary>已保存原文（未修改）</summary><pre className="packet">{raw.data.answer ?? raw.data.raw_response ?? '此记录未保存原文'}</pre></details>)}</>}
    {details && <><ErrorNotice error={version.error} />{version.data && <AnswerView version={version.data} />}</>}
    {branch && <details><summary>本模型的条件检查与候选方案（{arm.plans.length}）</summary><GuidancePanel workspace={branch} entryOnly />{arm.plans.map(p => <PlanCard key={p.plan_id} plan={p} evaluation={workspace?.evaluations.find(e => e.plan_id === p.plan_id)} adviceOverride={arm.guidance.action_advice?.find(a => a.plan_id === p.plan_id)} />)}</details>}
  </article>;
}

export function DailyResults({ group, workspace }: { group: Schema['DailyGroup']; workspace?: Schema['Workspace'] }) {
  return <section className="daily-round">
    <div className="row-between"><h2>本轮各模型结果</h2><Link to="/history">查看历史成绩 →</Link></div>
    <p>{group.template_version} · 资料截止 {time(group.market_cutoff)} · {group.arms.length}个模型 · {group.kind === 'new' ? '新一轮分析' : '沿原窗口更新'}</p>
    <p className="muted">各模型独立保存、并列展示，不指定主回答；不同方案不合并，交易由你手工完成。{group.same_question ? '本轮资料内容一致，仅各自请求身份不同。' : '更新沿用各模型自己的上一版，不能按完全相同输入比较。'}</p>
    {group.data_kind === 'SYNTHETIC' && <p className="notice">SYNTHETIC合成验收：模拟回答与成绩不能用于选择真实模型。</p>}
    <div className={`daily-results-grid models-${group.arms.length}`}>{group.arms.map(a => <Result key={a.request_id} arm={a} group={group} workspace={workspace} />)}</div>
  </section>;
}
