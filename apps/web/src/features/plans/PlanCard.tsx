import { useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { api, type Schema } from '../../api/client';
import { useSettings, useWorkspace } from '../../api/queries';
import { Badge, ErrorNotice, Field, Metric } from '../../shared/ui';
import { entryPresentation, useSnapshotTime } from '../../shared/entryPresentation';
import { label, money, ruleText, textValue, time, missingLabel } from '../../shared/format';
import { ScalpSuitability } from './ScalpSuitability';

function Assessment({ title, value }: { title: string; value: Schema['Assessment'] }) { return <div className="assessment"><span>{title}</span><Badge tone={value.state === 'satisfied' ? 'good' : value.state === 'not_satisfied' ? 'bad' : 'warn'}>{label(value.state)}</Badge>{value.reasons.length > 0 && <small>{value.reasons.join('；')}</small>}</div>; }
export function EvaluationResult({ value }: { value: Schema['Evaluation'] }) {
  return <div className="evaluation"><p className="muted">评估时间 {time(value.evaluated_at)} · 配置版本 {value.settings_revision}</p><div className="metrics three"><Metric label="预计净收益" value={money(value.planned_net_profit, value.currency)} /><Metric label="含费止损损失" value={money(value.planned_loss, value.currency)} /><Metric label="净收益 / 风险" value={textValue(value.net_rr)} /></div><div className="assessment-grid"><Assessment title="方案条件" value={value.lifecycle_check} /><Assessment title="评估时的报价" value={value.quote_check} /><Assessment title="费用完整性" value={value.cost_check} /><Assessment title="单笔预算" value={value.budget_check} /><Assessment title="保证金" value={value.margin_check} /></div>{value.missing_fields.length > 0 && <p className="missing">待补充：{value.missing_fields.map(missingLabel).join('、')}</p>}<details><summary>金额依据与精确值</summary><dl className="detail-grid"><dt>试算价格 / 手数</dt><dd>{textValue(value.entry_price)} / {textValue(value.volume_lots)}</dd><dt>毛止损损失</dt><dd>{textValue(value.gross_stop_loss)} {value.currency}</dd><dt>止损费用估算</dt><dd>{textValue(value.estimated_stop_cost)} {value.currency}</dd><dt>精确含费止损损失</dt><dd>{textValue(value.planned_loss)} {value.currency}</dd><dt>精确预计净收益</dt><dd>{textValue(value.planned_net_profit)} {value.currency}</dd><dt>保证金占用</dt><dd>{textValue(value.margin_required)} {value.currency}</dd><dt>配置版本 / 评估时间</dt><dd>#{value.settings_revision} · {time(value.evaluated_at)}</dd></dl><small>预算按未舍入的精确值比较。显示为 2.00 的 2.004 不满足 2.00 上限。</small></details></div>;
}
export function PlanCard({ plan, evaluation, adviceOverride }: { plan: Schema['PlanRecord']; evaluation?: Schema['Evaluation']; adviceOverride?: Schema['ActionAdvice'] }) {
  const cache = useQueryClient(); const settings = useSettings(); const workspace = useWorkspace();
  const [entry, setEntry] = useState(''); const [volume, setVolume] = useState(''); const [mode, setMode] = useState<'fixed_volume' | 'risk_budget'>('fixed_volume'); const [note, setNote] = useState('');
  const definition = plan.definition;
  const advice = adviceOverride ?? workspace.data?.action_advice?.find(item => item.plan_id === plan.plan_id);
  const refresh = workspace.data?.analysis_refresh?.find(item => item.trading_style === definition.trading_style);
  const now = useSnapshotTime(workspace.data?.as_of);
  const current = entryPresentation(advice, refresh, now, workspace.isError);
  const used = advice?.state === 'USED';
  const currentTitle = used ? '已使用，不再入场' : current.title?.replace(/^(超短线|日内) · /, '') ?? '未取得实时判断';
  const currentEntryLabel = advice?.entry_price ? '当前执行价' : '方案入场价';
  const currentStopLabel = advice ? '当前止损' : '方案止损';
  const currentTargetLabel = advice ? '当前目标' : '方案目标';
  const evaluate = useMutation({ mutationFn: () => api.evaluate(plan.plan_id, { market_snapshot_id: workspace.data!.market!.snapshot_id, settings_revision: settings.data!.revision, entry_price_override: entry || null, volume_lots_override: mode === 'risk_budget' ? null : volume || null, sizing_mode: mode }), onSuccess: () => cache.invalidateQueries({ queryKey: ['workspace'] }) });
  const observe = useMutation({ mutationFn: (role: 'activation' | 'invalidation') => api.observe(plan.plan_id, { condition_role: role, expected_plan_revision: plan.revision, note }), onSuccess: () => { setNote(''); void cache.invalidateQueries(); } });
  const matchesCurrent = (value?: Schema['Evaluation']) => value && value.market_snapshot_id === workspace.data?.market?.snapshot_id && value.portfolio_snapshot_id === workspace.data?.portfolio?.snapshot_id && value.settings_revision === settings.data?.revision;
  const candidates = [evaluate.data, evaluation].filter(matchesCurrent) as Schema['Evaluation'][];
  const effectiveEvaluation = candidates.sort((a,b) => b.evaluated_at.localeCompare(a.evaluated_at))[0];
  return <article className="plan-card" aria-label={`${definition.trading_style === 'scalp' ? '超短线' : definition.trading_style === 'intraday' ? '日内' : '候选'}方案详情`}>
    <div className="plan-title"><span className={`direction ${definition.side}`}>{definition.side === 'long' ? '↗' : '↘'}</span><div>
      <small>{definition.trading_style === 'scalp' ? '超短线方案' : definition.trading_style === 'intraday' ? '日内方案' : definition.role === 'primary' ? '主要方案' : '备选方案'}</small>
      <h3>{label(definition.side)}</h3>
    </div></div>
    <p className="plan-entry-mode">入场方式：{label(definition.entry.mode)}</p>
    <div className="plan-current-state"><span>当前状态</span><Badge tone={current.ready ? 'good' : 'neutral'}>{currentTitle}</Badge><a href="#entry-guidance">查看完整判断 ↑</a></div>
    {used && <p className="notice">此方案已关联真实持仓或成交，平仓后不恢复入场资格。下方保留原方案与金额依据。</p>}
    {definition.trading_style === 'scalp' && <ScalpSuitability value={advice?.scalp_suitability} preferences={plan.scalp_preferences} zone={settings.data?.values.display_timezone} />}
    <div className="plan-quick-facts" aria-label="方案关键参数">
      <Metric label={currentEntryLabel} value={advice?.entry_price ?? textValue(definition.entry.price)} note={advice?.entry_price && advice.side ? advice.side === 'short' ? '卖出侧 Bid' : '买入侧 Ask' : '当前执行侧尚未核验'} />
      <Metric label={currentStopLabel} value={advice?.stop_loss ?? definition.stop_loss} />
      <Metric label={currentTargetLabel} value={advice?.take_profit ?? definition.take_profit} />
    </div>
    <p className="plan-quick-deadline">当前允许入场范围：{advice?.allowed_zone ? `${advice.allowed_zone.low}—${advice.allowed_zone.high}` : '尚未核验'}{' · '}{current.effectiveEntryDeadline ? `当前最晚入场 ${time(current.effectiveEntryDeadline, settings.data?.values.display_timezone)}` : '当前期限尚未核验'}</p>
    {advice?.reasons?.length ? <p className="plan-status-reason">当前依据：{advice.reasons.join('；')}</p> : null}
    <details className="plan-basis"><summary>方案依据与参数</summary>
      <p>{definition.rationale}</p>
      <p className="muted">以下保留原方案依据与参数；当前是否可入场见上方实时判断。</p>
      {definition.trading_style === 'scalp' && plan.scalp_suitability && <ScalpSuitability value={plan.scalp_suitability} preferences={plan.scalp_preferences} historical zone={settings.data?.values.display_timezone} />}
      {definition.holding_time_basis && <p>持仓时长依据：{definition.holding_time_basis}</p>}
      <div className="plan-prices"><Metric label="入场价格" value={textValue(definition.entry.price)} note={definition.entry.zone ? `观察区间 ${definition.entry.zone.low} – ${definition.entry.zone.high}` : '没有入场区间'} /><Metric label="止损" value={definition.stop_loss} /><Metric label="目标" value={definition.take_profit} /></div><dl className="detail-grid"><dt>触发条件</dt><dd>{ruleText(definition.activation)}</dd><dt>全程背景失效</dt><dd>{ruleText(definition.invalidation)}<br /><small className="muted">等待触发期间及触发后均生效；不会等到突破后才检查。</small></dd>{definition.post_activation_invalidation && <><dt>激活后结构失效</dt><dd>{ruleText(definition.post_activation_invalidation)}<br /><small className="muted">激活事件及其后检查，同事件失效优先。</small></dd></>}<dt>新入场截止</dt><dd>{time(definition.entry_deadline, settings.data?.values.display_timezone)}</dd><dt>退出截止</dt><dd>{time(definition.exit_deadline, settings.data?.values.display_timezone)}{definition.max_holding_minutes !== null ? ` · 成交后最多 ${definition.max_holding_minutes} 分钟` : ''}</dd></dl>
    </details>
    <details className="plan-tools"><summary>试算金额与人工观察</summary>{effectiveEvaluation ? <EvaluationResult value={effectiveEvaluation} /> : <p className="notice">尚未取得金额评估。可保留方案并补充所需的费用、手数和报价。</p>}<form className="form-grid" onSubmit={event => { event.preventDefault(); evaluate.mutate(); }}><Field label="试算方式"><select value={mode} onChange={event => setMode(event.target.value as typeof mode)}><option value="fixed_volume">固定手数</option><option value="risk_budget">按单笔预算求合法手数</option></select></Field><Field label="试算入场价格" hint="留空使用后端确定的方案价格；不更改原回答。"><input inputMode="decimal" value={entry} onChange={event => setEntry(event.target.value)} placeholder="待补充或沿用方案" /></Field>{mode === 'fixed_volume' && <Field label="本次试算手数"><input inputMode="decimal" value={volume} onChange={event => setVolume(event.target.value)} placeholder="沿用方案或默认设置" /></Field>}<div className="form-actions"><button className="primary" disabled={evaluate.isPending || !workspace.data?.market || !settings.data}>{evaluate.isPending ? '计算中…' : '计算金额'}</button></div></form><ErrorNotice error={evaluate.error} />{(['activation', 'invalidation'] as const).some(role => definition[role].kind === 'manual_observation') && <div className="observation"><Field label="当时观察说明"><textarea value={note} onChange={event => setNote(event.target.value)} placeholder="记录你刚刚确认的事实，时间由服务端填写。" /></Field><div className="actions">{(['activation', 'invalidation'] as const).filter(role => definition[role].kind === 'manual_observation').map(role => <button key={role} disabled={!note.trim() || observe.isPending} onClick={() => observe.mutate(role)}>记录{role === 'activation' ? '触发' : '失效'}观察</button>)}</div><ErrorNotice error={observe.error} /></div>}<p className="muted">条件有效表示方案条件状态，实际是否入场以券商事实为准。</p>{plan.manual_observations.map(item => <p key={item.observation_id}>人工观察：{item.note}</p>)}</details></article>;
}
