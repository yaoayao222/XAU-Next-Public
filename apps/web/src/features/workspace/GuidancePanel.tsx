import { Link } from 'react-router-dom';
import type { Schema } from '../../api/client';
import { Badge, Metric } from '../../shared/ui';
import { label, ruleText, time } from '../../shared/format';
import { entryPresentation, useSnapshotTime } from '../../shared/entryPresentation';
import { ScalpSuitability } from '../plans/ScalpSuitability';
import { HoldingPanel } from './HoldingPanel';

const styles = { scalp: '超短线 · 局部价差机会', intraday: '日内 · 数小时' };
const delivery = { queued: '等待投递', sending: '正在提交', submitted: '已提交本机通知', suppressed: '未发送', failed: '投递失败', unknown: '投递结果未知' };
const refreshLabels = { FRESH: '分析仍有效', DUE: '应准备下一份分析', STALE: '分析已过时', REASSESS: '需要提前重评', WAITING_DATA: '等待行情资料', MISSING: '尚无分析' };
const duration = (seconds: number) => `${Math.floor(Math.max(0, seconds) / 60)}分${Math.floor(Math.max(0, seconds) % 60)}秒`;
const stopLabels: Record<Schema['StoppingReason']['code'], string> = {
  BACKGROUND_INVALIDATED: '候选背景失效', POST_ACTIVATION_INVALIDATED: '激活后结构失效', SUPERSEDED: '已被新版本替代',
  ENTRY_DEADLINE: '候选入场到期', ANALYSIS_EXPIRED: '分析时效到期', REASSESSMENT_CONDITION: '重评条件已确认',
  OBSERVATION_WINDOW_SHORT: '导入后观察窗口不足', TRIGGER_DELAY: '触发事实到达过晚', PRICE_GEOMETRY: '已越过止损或目标', CHASE_LIMIT: '已越过允许追入边界',
};

type SummaryTone = 'neutral' | 'good' | 'warn';

const adviceStateTitles: Record<Schema['ActionAdvice']['state'], string> = {
  WAITING: '等待条件', READY: '当前不可入场', MISSED: '已错过入场', USED: '方案已使用，不再入场',
  INVALIDATED: '候选已失效', EXPIRED: '候选已过期', REFRESH_REQUIRED: '需要新分析', BLOCKED: '当前不可入场', OBSERVE: '继续观察',
};

function summaryRow(
  workspace: Schema['Workspace'],
  refresh: Schema['AnalysisRefresh'],
  offline: boolean,
  now: number,
) {
  const advice = workspace.action_advice?.find(item => item.trading_style === refresh.trading_style);
  const assessment = workspace.candidate_assessments?.find(item => item.trading_style === refresh.trading_style);
  const presentation = entryPresentation(advice, refresh, now, offline);
  const stale = Boolean(refresh.fresh_until && now >= Date.parse(refresh.fresh_until));
  const waitingData = refresh.state === 'WAITING_DATA' && !stale;
  const needsAnalysis = stale || presentation.timedOut || ['STALE', 'REASSESS', 'MISSING'].includes(refresh.state);
  const due = refresh.state === 'DUE' && !needsAnalysis;
  const offlineWaiting = offline && !presentation.ready && advice?.state === 'WAITING';
  const title = presentation.ready ? '当前可入场'
    : offlineWaiting ? '离线 · 判断待更新'
      : (presentation.expired || offline) && presentation.title ? presentation.title
      : waitingData ? '等待行情资料'
      : needsAnalysis ? '需要新分析'
          : due && !advice ? '当前分析仍有效 · 建议准备下一份'
            : assessment?.outcome === 'no_candidate' ? '暂无合格候选'
              : refresh.state === 'MISSING' ? '尚无分析'
                  : advice ? adviceStateTitles[advice.state] : '尚无分析';
  const next = offline ? '恢复本地服务后重新核验'
    : waitingData ? '刷新事实或等待自动读取'
      : advice?.state === 'USED' ? '新建一轮分析'
        : due && !advice ? '准备下一份分析'
          : due && presentation.ready ? '在 MT5 确认后手工执行，同时准备下一份分析'
            : due && advice?.state === 'WAITING' ? '继续等待条件，同时准备下一份分析'
              : presentation.ready ? '在 MT5 确认后手工执行'
                : needsAnalysis ? (refresh.pending_request_id ? '打开已准备的分析包' : '取得新的分析')
                  : advice?.state === 'WAITING' ? '继续等待条件，系统会随事实读取核验'
                    : advice?.state === 'BLOCKED' ? '补充缺失资料后重新核验' : '查看完整判断';
  const tone: SummaryTone = presentation.ready ? 'good' : offline || waitingData || needsAnalysis ? 'warn' : 'neutral';
  return { refresh, title, next, tone };
}

export function ActionSummary({ workspace, offline, zone, onRefresh, refreshPending = false }: { workspace: Schema['Workspace']; offline?: boolean; zone?: string; onRefresh?: () => void; refreshPending?: boolean }) {
  const now = useSnapshotTime(workspace.as_of);
  const rows = (workspace.analysis_refresh ?? []).map(refresh => summaryRow(workspace, refresh, Boolean(offline), now));
  const adviceOnly = workspace.action_advice ?? [];
  if (!rows.length) {
    const hasMarket = Boolean(workspace.market);
    if (adviceOnly.length) return <section className="action-summary" aria-label="当前行动摘要">
      <div className="section-heading"><div><p className="eyebrow">现在需要做什么</p><h2>当前行动摘要</h2></div><Badge tone="warn">资料待核验</Badge></div>
      <div className="action-summary-grid">{adviceOnly.map(advice => <article key={advice.plan_id} className="action-summary-item attention">
        <div className="row-between"><strong>{styles[advice.trading_style ?? 'intraday']}</strong><Badge tone="warn">已有判断，当前资料状态待核验</Badge></div>
        <small>下一步：刷新事实后重新核验</small>
      </article>)}</div>
      <p className="safety-inline">当前可入场只表示本机核验通过，不会自动下单；请在 MT5 中自行确认并执行。</p>
    </section>;
    return <section className="action-summary" aria-label="当前行动摘要">
      <div className="section-heading"><div><p className="eyebrow">现在需要做什么</p><h2>{hasMarket ? '还没有分析' : '尚未取得当前事实'}</h2></div><Badge tone="warn">{hasMarket ? '开始分析' : '先刷新事实'}</Badge></div>
      <p className="muted">{hasMarket ? '当前报价已经读取，但还没有分析判断；新建一轮分析后才会显示候选方案和入场条件。' : '取得当前报价后，系统才会显示候选方案和入场条件。'}所有交易仍由你在 MT5 手工完成。</p>
      {hasMarket ? <Link className="button primary" to="/analysis?mode=manual">新建一轮分析</Link>
        : onRefresh ? <button className="primary" onClick={onRefresh} disabled={refreshPending}>{refreshPending ? '正在刷新…' : '立即刷新事实'}</button> : <Link className="button" to="/">返回工作台</Link>}
    </section>;
  }
  return <section className="action-summary" aria-label="当前行动摘要">
    <div className="section-heading"><div><p className="eyebrow">现在需要做什么</p><h2>当前行动摘要</h2></div><Badge>只读 · 人工执行</Badge></div>
    <div className="action-summary-grid">{rows.map(({ refresh, title, next, tone }) => <article key={refresh.trading_style} className={`action-summary-item ${tone}`}>
      <div className="row-between"><strong>{refresh.trading_style === 'scalp' ? '超短线 · 局部价差机会' : '日内 · 数小时'}</strong><Badge tone={tone}>{title}</Badge></div>
      <small>下一步：{next}</small>
      {refresh.pending_request_id && (title === '需要新分析' || title === '尚无分析') && <Link to={`/analysis?request=${encodeURIComponent(refresh.pending_request_id)}`}>打开已准备的分析包 →</Link>}
    </article>)}</div>
    <p className="safety-inline">当前可入场只表示本机核验通过，不会自动下单；请在 MT5 中自行确认并执行。</p>
  </section>;
}

function StopDetails({ reason, zone }: { reason: Schema['StoppingReason']; zone?: string }) {
  return <div className="notice"><strong>停止依据：{stopLabels[reason.code]}</strong>
    {reason.condition && <p>对应条件：{ruleText(reason.condition)}</p>}
    {reason.deadline && <p>对应截止：{time(reason.deadline, zone)}</p>}
    <p>依据时间：{reason.occurred_at ? time(reason.occurred_at, zone) : '未保存确切发生时间，不补造'}</p>
    <p>下一步：{reason.next_step}</p></div>;
}

export function GuidancePanel({ workspace, offline = false, zone, entryOnly = false, holdingOnly = false }: { workspace: Schema['Workspace']; offline?: boolean; zone?: string; entryOnly?: boolean; holdingOnly?: boolean }) {
  const now = useSnapshotTime(workspace.as_of);
  const advices = workspace.action_advice ?? [];
  const refreshes = workspace.analysis_refresh ?? [];
  if (!workspace.analysis_refresh) return <HoldingPanel workspace={workspace} offline={offline} now={now} zone={zone} />;
  return <section id={entryOnly ? undefined : "entry-guidance"} className="guidance-panel" aria-label="系统行动建议">
    {!holdingOnly && <><div className="section-heading"><div><p className="eyebrow">当前行动</p><h2>系统判断入场时机</h2></div><Badge>MT5 人工执行</Badge></div>
    <p className="muted">候选可以等待未来条件。只有本机按当前价格、费用、预算和保证金核验通过，才显示当前可入场并发送通知。</p>
    <div className="guidance-grid">{refreshes.map(refresh => {
      const advice = advices.find(item => item.trading_style === refresh.trading_style);
      const assessment = workspace.candidate_assessments?.find(item => item.trading_style === refresh.trading_style);
      const planRecord = workspace.plans.find(item => item.plan_id === advice?.plan_id);
      const plan = planRecord?.definition;
      const { timedOut, stopReason, expired, ready, title: currentTitle, effectiveEntryDeadline } = entryPresentation(advice, refresh, now, offline);
      const title = offline && advice?.state === 'WAITING' ? '离线 · 判断待更新' : currentTitle ?? (assessment?.outcome === 'no_candidate' ? '暂无合格候选，继续观察'
          : assessment?.outcome === 'candidate' ? '已有候选，等待系统核验' : '当前没有入场方案，继续观察');
      const stale = Boolean(refresh.fresh_until && now >= Date.parse(refresh.fresh_until));
      const waitingData = refresh.state === 'WAITING_DATA' && !stale;
      const needsAnalysis = stale || timedOut || ['STALE', 'REASSESS', 'MISSING'].includes(refresh.state);
      const preparing = refresh.state === 'DUE' && !needsAnalysis;
      const canWait = Boolean(advice && ['WAITING', 'READY', 'BLOCKED'].includes(advice.state) && !needsAnalysis && !stopReason);
      return <article key={refresh.trading_style} className={`guidance-card ${ready ? 'ready' : needsAnalysis || waitingData ? 'attention' : ''}`}>
        <small>{styles[refresh.trading_style]}</small><h3 role="status" aria-live="polite">{title}</h3>
        {plan && <p className="guidance-entry-mode">入场方式：{label(plan.entry.mode)}</p>}
        {advice && <Badge tone={ready ? 'good' : 'neutral'}>{ready ? '当前可入场' : advice.state === 'WAITING' && !timedOut ? '候选 · 等待条件' : '当前不可入场'}</Badge>}
        {stopReason && <StopDetails reason={stopReason} zone={zone} />}
        {advice ? <>
          <p>{advice.state === 'READY' && (expired || offline) ? '上一次可入场判断已失去当前性，请等待后台重新核验。' : advice.reasons.join('；')}</p>
          {advice.trading_style === 'scalp' && <ScalpSuitability value={advice.scalp_suitability} preferences={planRecord?.scalp_preferences} zone={zone} />}
          {advice.state === 'WAITING' && !timedOut && plan && <div className="candidate-conditions"><p>触发条件：{ruleText(plan.activation)}</p>
            {plan.entry_checks?.map((rule, index) => <p key={index}>入场检查：{ruleText(rule)}</p>)}
            <p>背景失效：{ruleText(plan.invalidation)}</p><small>等待触发及触发后全程检查。</small>
            {plan.post_activation_invalidation && <p>激活后结构失效：{ruleText(plan.post_activation_invalidation)}（激活事件起检查，同事件失效优先）</p>}</div>}
          <div className="metrics three"><Metric label={advice.side === 'long' ? '当前买入侧 Ask' : '当前卖出侧 Bid'} value={advice.entry_price ?? '未知'} />
            <Metric label="止损" value={advice.stop_loss} /><Metric label="目标" value={advice.take_profit} /></div>
          <p>允许入场范围：{advice.allowed_zone ? `${advice.allowed_zone.low}—${advice.allowed_zone.high}` : '尚未明确'}
            {' · '}手数：{advice.volume_lots ?? '待核验'}</p>
          <small>最晚入场 {effectiveEntryDeadline ? time(effectiveEntryDeadline, zone) : '尚未核验'} · 成交后最多 {advice.max_holding_minutes ?? '未知'} 分钟</small>
          {plan && <small className="guidance-time">退出截止 {time(plan.exit_deadline, zone)}；最长持仓从实际成交起算。</small>}
          {plan?.holding_time_basis && <small className="guidance-time">持仓时长依据：{plan.holding_time_basis}</small>}
          {advice.entry_timing && <div className="candidate-conditions">
            <p>资料截止至导入共 {duration(advice.entry_timing.import_delay_seconds)} · 导入时可等 {duration(advice.entry_timing.remaining_at_import_seconds)}</p>
            {advice.entry_timing.cutoff_to_request_seconds !== undefined && advice.entry_timing.request_to_available_seconds !== undefined &&
              <small>其中：截止至请求创建 {duration(advice.entry_timing.cutoff_to_request_seconds)}；请求创建至导入 {duration(advice.entry_timing.request_to_available_seconds)}</small>}
            <p>{canWait ? `最多还能等待入场 ${duration((Date.parse(advice.entry_timing.effective_entry_deadline) - now) / 1000)}` : `当前暂停或停止新入场；原有效截止 ${time(advice.entry_timing.effective_entry_deadline, zone)}`}
              {advice.entry_timing.activation_closes_at_import !== null && ` · 导入时剩余触发闭柱机会 ${advice.entry_timing.activation_closes_at_import} 次`}</p>
            <small>等待时间不等于成交后持仓时长；真实失效成立时会提前停止。</small>
            {!advice.entry_timing.enforced && <small>历史方案仅展示原期限，不追加新预算门槛。</small>}
            {advice.entry_timing.enforced && advice.entry_timing.state === 'INSUFFICIENT' && <p>最低观察所需截止 {time(advice.entry_timing.minimum_entry_deadline, zone)}；已超出允许窗口。保留原回答并等待新方案，不自动延长期限。</p>}
          </div>}
          {advice.valid_until && <small className="guidance-time">本次价格判断有效至 {time(advice.valid_until, zone)}</small>}
        </> : assessment ? <div className="candidate-assessment"><p>{assessment.rationale}</p>
          {assessment.outcome === 'no_candidate' && <><strong>尚缺条件（本次分析判断）</strong>
            <ul>{assessment.missing_conditions.map((condition, index) => <li key={index}>{condition}</li>)}</ul></>}
          <small>这是分析截止时的候选判断；系统不会从预测区间自动补出方案。</small>
        </div> : <p>此回答未提供该周期的独立候选说明；请查看原文。系统不补写历史原因。</p>}
        <div className="guidance-refresh"><Badge tone={needsAnalysis || waitingData ? 'warn' : 'neutral'}>{stale ? '分析已过时' : advice?.state === 'USED' && refresh.stopping_reason?.code !== 'REASSESSMENT_CONDITION' ? '下一次入场需新分析' : refreshLabels[refresh.state]}</Badge>
          <p>{refresh.reasons.join('；')}</p>
          {refresh.market_cutoff && <small>资料截止 {time(refresh.market_cutoff, zone)} · 入场分析时效截至 {time(refresh.fresh_until, zone)}</small>}
          {refresh.remind_at && <small className="guidance-time">下一份分析准备提醒：{time(refresh.remind_at, zone)}；提醒本身不提前取消候选。</small>}
          {canWait && !preparing && <p>候选继续按原条件等待，无需仅因尚未触发或回踩而重新分析。</p>}
          {offline ? <p>本地服务离线，恢复后重新核验当前判断。</p>
            : waitingData ? <p>系统会随每次事实读取重新核验，无需重复导入。</p>
            : (preparing || needsAnalysis) && <Link className="button" to={refresh.pending_request_id ? `/analysis?request=${encodeURIComponent(refresh.pending_request_id)}` : '/analysis'}>
              {preparing ? '准备下一份分析' : refresh.pending_request_id ? '打开已准备的分析包' : '取得新的分析'}</Link>}
        </div>
      </article>;
    })}</div></>}
    {!entryOnly && <HoldingPanel workspace={workspace} offline={offline} now={now} zone={zone} />}
    {!entryOnly && (workspace.position_advice ?? []).filter(item => item.order.kind !== 'position' && item.state !== 'HOLD').map(item => {
      const expired = offline || Boolean(item.valid_until && now >= Date.parse(item.valid_until));
      return <div className="notice" key={item.order.broker_id}>
        <strong>{expired ? '持仓管理资料已过时，等待重新核验' : item.title}</strong><p>{expired ? '旧退出提示保留为历史，不能代表当前持仓与价格。' : item.reasons.join('；')}</p><small>持仓／挂单 {item.order.broker_id} · {time(item.evaluated_at, zone)}</small>
      </div>;
    })}
    {!entryOnly && <details className="notification-history"><summary>最近通知与投递状态（{workspace.notifications?.length ?? 0}）</summary>
      <p className="muted">下方保留通知历史；当前是否可入场以上方最新判断为准。“已提交”不代表用户已看到或已经交易。</p>
      {(workspace.notifications ?? []).map(item => <article key={item.notification_id} className="notification-item">
        <div className="row-between"><strong>{item.title}</strong><Badge tone={item.state === 'failed' || item.state === 'unknown' ? 'warn' : 'neutral'}>{delivery[item.state]}</Badge></div>
        <p>{item.body}</p><small>{time(item.created_at, zone)} · {now >= Date.parse(item.expires_at) ? '历史通知，已过有效期' : `原有效期至 ${time(item.expires_at, zone)}`}</small>
        {item.detail && <p className="muted">{item.detail}</p>}
      </article>)}
    </details>}
  </section>;
}
