import { useRef, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { api, type Schema } from '../../api/client';
import { Badge, ErrorNotice } from '../../shared/ui';
import { label, ruleText, time } from '../../shared/format';

const targetLabels = { supported: '原目标仍获支持', weakened: '原目标依据减弱', invalidated: '原目标逻辑已失效', unknown: '目标可达性资料不足' };
const normalized = (v: string | null) => v?.includes('.') ? v.replace(/0+$/, '').replace(/\.$/, '') : v;

function HoldingCard({ advice, offline, now, zone }: { advice: Schema['PositionAdvice']; offline: boolean; now: number; zone?: string }) {
  const navigate = useNavigate();
  const busy = useRef(false);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const m = advice.management;
  const expired = offline || Boolean(advice.valid_until && now >= Date.parse(advice.valid_until));
  const result = m?.latest_review;
  const currentOpinion = Boolean(result?.current && !offline && result.valid_until && now < Date.parse(result.valid_until));
  const assessment = result?.advice.target_assessment;
  const create = async () => {
    if (busy.current) return;
    busy.current = true; setPending(true); setError(null);
    try {
      const fresh = await api.workspace();
      if (!fresh.market) throw new Error('尚无当前行情，请等待事实恢复。');
      const request = await api.createRequest({ kind: 'new', forecast_id: null, base_version_id: null,
        market_snapshot_id: fresh.market.snapshot_id, holding_review_order: advice.order });
      navigate(`/analysis?request=${encodeURIComponent(request.request_id)}`);
    } catch (e) { setError(e); } finally { busy.current = false; setPending(false); }
  };
  return <article className="guidance-card" aria-label={`持仓管理 ${advice.order.broker_id}`}>
    <div className="row-between"><h3>已有持仓 · {advice.order.broker_id}</h3><Badge tone={expired || advice.state !== 'HOLD' ? 'warn' : 'neutral'}>
      {expired ? '资料待更新' : advice.state === 'EXIT' ? '已触发退出条件' : advice.state === 'UNKNOWN' ? '暂不可核验' : '尚未触发原退出条件'}</Badge></div>
    <p><strong>第一层：原规则检查</strong></p>
    <p>{expired ? '持仓管理资料已过时，等待重新核验' : advice.title}</p>
    {advice.reasons.length > 0 && <p>{advice.reasons.join('；')}</p>}
    {m && <>
      <p>{m.structure_reason}</p>
      {(m.structure_checks ?? []).map((c, i) => <p key={i}>结构退出检查：{ruleText(c.condition)} · {c.matched === true ? '已触发' : c.matched === null ? '资料不足' : '尚未触发'}
        {c.observed_at && ` · 依据时间 ${time(c.observed_at, zone)}`}{c.latched && '（已确认事实保留）'}</p>)}
      <p>原方案止损 {m.original_stop ?? '未知'} · 原方案目标 {m.original_target ?? '未知'}</p>
      <p>MT5实际止损 {m.broker_stop ?? '未设置'} · MT5实际止盈 {m.broker_target ?? '未设置'}</p>
      {(normalized(m.original_stop) !== normalized(m.broker_stop) || normalized(m.original_target) !== normalized(m.broker_target)) &&
        <p className="notice">原方案与MT5实际设置有差异，系统不会自动修改。券商执行以实际设置与成交为准。</p>}
      <p>真实成交 {time(m.opened_at, zone)} · 原持仓最晚退出 {time(m.deadline, zone)}</p>
      <p>{expired ? '上次' : '当前'}退出侧报价 {m.exit_price ?? '未知'} · 到原目标尚差 {m.target_distance ?? '未知'} 美元/盎司</p>
      <small>上述距离只是价格差，不是到达概率；原持仓计时不会因重评重新开始。</small>
      <p><strong>第二层：目标重评</strong></p>
      {m.reassessment_state === 'required' && <div className="notice"><strong>结构有变化，建议手动重评当前持仓</strong><p>{m.reassessment_reasons.join('；')}</p></div>}
      {m.reassessment_state === 'unknown' && <p>{m.reassessment_reasons.join('；')}</p>}
      <details><summary>查看原分析的持仓重评条件</summary>{(m.reassessment_checks ?? []).length ? (m.reassessment_checks ?? []).map((c, i) => <p key={i}>{ruleText(c.condition)} · {c.matched === true ? '已观察到' : c.matched === null ? '资料不足' : '尚未观察到'}{c.observed_at && ` · ${time(c.observed_at, zone)}`}</p>) : <p>原分析未登记该模式的可计算重评条件，未补写历史规则。</p>}</details>
      {result && assessment ? <div className="candidate-conditions">
        <strong>{currentOpinion ? '最近一次重评：' : '历史重评：'}{targetLabels[assessment.state]}</strong>
        <p>评价原目标 {assessment.target_price ?? '未知'} · 建议 {label(result.advice.action)}</p>
        <p>{result.advice.reason}</p>
        <p>资料截止 {time(result.market_cutoff, zone)} · 回答可用 {time(result.available_at, zone)} · 建议有效至 {time(result.valid_until, zone)}</p>
        {!currentOpinion && <p>该意见不能代表当前有效判断。{result.stale_reasons.join('；')}</p>}
        <details><summary>查看目标依据与变化</summary>
          <p>支持依据：{assessment.supporting_evidence.join('；') || '未提供'}</p>
          <p>反对依据：{assessment.opposing_evidence.join('；') || '未提供'}</p>
          <p>途中阻力：{assessment.obstacles.join('；') || '未发现明确阻力，不代表不存在'}</p>
          <p>时间依据：{assessment.time_basis}</p><p>相较前次：{assessment.change_since_previous}</p>
          <p>局限：{assessment.limitations.join('；') || '未另列'}</p>
          <p>建议止损 {result.advice.proposed_stop_loss ?? '未建议修改'} · 建议目标 {result.advice.proposed_take_profit ?? '未建议修改'} · 建议数量 {result.advice.proposed_volume_lots ?? '未建议修改'}</p>
          <Link to={`/analysis?request=${encodeURIComponent(result.request_id)}`}>查看保存的重评请求</Link>
        </details>
      </div> : <p>尚无持仓专项目标重评；未触发退出条件，不代表已经确认目标会到达。</p>}
      {!expired && advice.state === 'EXIT' ? <p>请按已触发的原退出规则处理，不等待新的模型回答。</p>
        : m.pending_request_id ? <Link className="button" to={`/analysis?request=${encodeURIComponent(m.pending_request_id)}`}>打开已准备的持仓重评包</Link>
          : <button disabled={expired || advice.state !== 'HOLD' || !advice.plan_id || !m.opened_at || pending} onClick={() => void create()}>
            {pending ? '正在准备持仓重评包…' : '准备当前持仓重评包'}</button>}
      <p className="muted">准备分析包只保存资料，不调用模型。你手动取得回答后导入；建议不会覆盖原方案或操作MT5。</p>
      <ErrorNotice error={error} />
    </>}
  </article>;
}

export function HoldingPanel({ workspace, offline, now, zone }: { workspace: Schema['Workspace']; offline: boolean; now: number; zone?: string }) {
  const positions = (workspace.position_advice ?? []).filter(a => a.order.kind === 'position');
  if (!positions.length) return null;
  return <section aria-label="已有持仓管理"><h2>已有持仓管理</h2><p>原规则持续检查；目标重评需手动发起。交易仍由你在MT5执行。</p>
    <div className="guidance-grid">{positions.map(a => <HoldingCard key={`${a.order.account_scope_id}:${a.order.broker_id}`} advice={a} offline={offline} now={now} zone={zone} />)}</div>
  </section>;
}
