import type { Schema } from '../../api/client';
import { Badge } from '../../shared/ui';
import { time } from '../../shared/format';

const stateLabels = { compatible: '符合本次偏好', incompatible: '超出本次偏好', unknown: '资料不足，尺度待核验' };
const triggerLabels = { needed: '尚需价格位移', price_satisfied: '价格已到阈值，条件仍须核验', background_satisfied: '持续背景当前成立，入场时复核', background_waiting: '持续背景尚未成立，继续等待', latched: '已保存触发事实', unknown: '所需价格位移未知' };
const rangeText = (value: Schema['ScalpSuitability']['target_space']) => value ? `${value.low === value.high ? value.low : `${value.low}—${value.high}`}美元` : '未知';

export function ScalpSuitability({ value, preferences, historical = false, zone }: {
  value?: Schema['ScalpSuitability'] | null;
  preferences?: Schema['ScalpPreferences'] | null;
  historical?: boolean;
  zone?: string;
}) {
  if (!value) return <p className="muted">{preferences ? '尚未取得超短线尺度检查，等待完整资料。' : '该方案未提供冻结的超短线偏好，保留原规则。'}</p>;
  return <section className="candidate-conditions" aria-label={historical ? '导入时超短线尺度检查' : '当前超短线尺度检查'}>
    <div className="row-between"><strong>{historical ? '导入时超短线尺度检查' : '超短线尺度检查'}</strong><Badge tone={value.state === 'compatible' ? 'neutral' : 'warn'}>{stateLabels[value.state]}</Badge></div>
    <p>距离入场区间{value.entry_distance === null ? '未知' : `${value.entry_distance}美元`}</p>
    <p>目标价差{rangeText(value.target_space)}</p>
    {value.reasons.map((reason, index) => <p key={index}>{reason}</p>)}
    {value.range_net_check && value.range_net_check.state !== 'satisfied' && <p>全入场范围金额检查：{value.range_net_check.state === 'unknown' ? '待补资料' : '未通过'}。{value.range_net_check.reasons.join('；')}</p>}
    <details><summary>距离依据与必要路径</summary>
    <small>{value.execution_side === 'ask' ? `${historical ? '当时' : '当前'}买入侧 Ask` : value.execution_side === 'bid' ? `${historical ? '当时' : '当前'}卖出侧 Bid` : '执行报价侧未知'} · 报价时间{value.basis_quote_at ? time(value.basis_quote_at, zone) : '未知'}</small>
    <p>有效入场范围：{value.effective_entry_range ? `${value.effective_entry_range.low}—${value.effective_entry_range.high}` : '未知'} · 止损价差{rangeText(value.stop_space)}</p>
    {value.trigger_distances.map((trigger, index) => <div key={`${trigger.role}:${index}`}>
      <p>{trigger.role === 'activation' ? '触发条件' : '入场检查'} · {trigger.price_basis ? trigger.price_basis.toUpperCase() : '价格基准未知'}{trigger.threshold === null ? '' : ` ${trigger.threshold}`}：{triggerLabels[trigger.state]}{trigger.distance === null ? '' : ` ${trigger.distance}美元`}</p>
      {trigger.note && <small>{trigger.note}</small>}
    </div>)}
    {value.required_path && <p>{value.required_path}</p>}
    </details>
    <small>{historical ? '导入检查使用请求截止点的冻结资料，不代表当前可以入场。' : '尺度符合不代表触发或金额检查已通过。'}价差按允许入场范围计算，与账户盈亏和报价步长不同。</small>
    {preferences && <details><summary>本次请求冻结的超短线偏好</summary><p>目标价差{preferences.target_min}—{preferences.target_max}美元；入场距离最多{preferences.max_entry_distance}美元；未满足价格触发位移最多{preferences.max_trigger_distance}美元。</p><p>成交后优先{preferences.preferred_holding_minutes}分钟内完成，兼容上限{preferences.max_holding_minutes}分钟。等待入场期限另行计算。</p></details>}
  </section>;
}
