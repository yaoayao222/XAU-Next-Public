import type { Schema } from '../api/client';

let displayTimezone = 'Asia/Shanghai';
export const setDisplayTimezone = (zone: string) => { displayTimezone = zone; };
export function time(value?: string | null, zone = displayTimezone): string {
  if (!value) return '未知';
  try { return new Intl.DateTimeFormat('zh-CN', { timeZone: zone, month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false }).format(new Date(value)); } catch { return value; }
}

/** Display-only ROUND_HALF_UP, retaining Decimal strings and exact amounts elsewhere. */
export function decimalDisplay(value: string, places = 2): string {
  if (!/^-?\d+(\.\d+)?$/.test(value)) return value;
  const negative = value.startsWith('-');
  const [whole, fraction = ''] = value.replace(/^-/, '').split('.');
  const padded = fraction.padEnd(places + 1, '0');
  let units = BigInt(whole + padded.slice(0, places));
  if (padded[places] >= '5') units += 1n;
  const result = units.toString().padStart(places + 1, '0');
  return `${negative && units !== 0n ? '-' : ''}${places ? `${result.slice(0, -places)}.${result.slice(-places)}` : result}`;
}
export const money = (amount: string | null | undefined, currency = 'USD') => amount == null ? '未知' : `${currency === 'USD' ? decimalDisplay(amount) : amount} ${currency}`;
export const textValue = (value: string | null | undefined, unit = '') => value == null ? '未知' : `${value}${unit}`;
export const missingLabel = (field: string): string => ({
  entry_price: '精确入场价', volume_lots: '试算手数', contract: '合约规格',
  profit_calculation_or_fx: '利润计算或币种换算依据', account: '账户资料',
  cost_assumption_applicability: '费用适用账户、品种或时间',
  'cost_assumption.commission_roundtrip_per_lot': '来回佣金',
  'cost_assumption.slippage_reserve_per_lot': '滑点预留',
  'cost_assumption.other_cost_per_lot': '其他预计费用',
  per_trade_loss_cap: '单笔预算', risk_budget_sizing_inputs: '按预算求手数的必需输入',
  margin_calculation: '保证金试算依据',
} as Record<string,string>)[field] ?? field;
export const labels: Record<string, string> = {
  WAITING_TRIGGER: '等待触发', ACTIVE: '条件有效', INVALIDATED: '已失效', EXPIRED: '已过期', SUPERSEDED: '已被新版本替代',
  long: '看多', short: '看空', neutral: '中性', uncertain: '不确定', trend: '趋势', range: '区间', transition: '转换阶段',
  available: '已读取', stale: '数据已陈旧', unavailable: '未取得', incomplete: '信息不完整',
  satisfied: '满足', not_satisfied: '不满足', unknown: '未知', not_applicable: '不适用',
  queued: '等待处理', running: '处理中', succeeded: '已完成', failed: '处理失败', cancelling: '正在取消', cancelled: '已取消',
  awaiting_answer: '等待回答', imported: '已导入', expired: '已过期', superseded: '已替代',
  market: '条件满足后判断市价', limit_retest: '回踩限价', stop_breakout: '突破条件入场',
  forecast: '行情预测', reference_plan: '参考方案', actual_trade: '实际交易',
  EVALUATED: '已评价', NOT_TRIGGERED: '未触发', EXPIRED_WITHOUT_ENTRY: '到期未入场', TP: '参考止盈', SL: '参考止损', TIME_EXIT: '到时退出', AMBIGUOUS: '先后顺序不明', DATA_INCOMPLETE: '证据不完整', ACTUAL_CLOSED: '实际已平仓', ACTUAL_OPEN: '实际仍持仓',
  complete: '完整', partial: '部分覆盖', missing: '缺失', open: '持仓中', partially_closed: '部分平仓', closed: '已平仓',
  hold: '持有', close: '退出', reduce: '减仓', modify_sl_tp: '调整止损止盈', cancel_pending: '取消挂单', observe: '保持观察', no_change: '保持不变',
};
export const label = (value: string | null | undefined) => value == null ? '未知' : labels[value] ?? `此版本暂不支持（${value}）`;
export function ruleText(rule: Schema['Plan']['activation'] | NonNullable<Schema['Plan']['entry_checks']>[number]): string {
  if (rule.kind === 'immediate') return '从方案生效时开始观察';
  if (rule.kind === 'manual_observation') return `需人工确认：${rule.instruction}`;
  const operators = { gt: '>', gte: '≥', lt: '<', lte: '≤' };
  const text = rule.kind === 'bar_close' ? `${rule.timeframe} 已收盘价 ${operators[rule.operator]} ${rule.price}` : `${rule.quote_side.toUpperCase()} ${operators[rule.operator]} ${rule.price}`;
  if ('observation' in rule) {
    const roles = { background: '持续背景', new_since_available: '可用后新观察', new_since_activation: '激活后新观察' };
    return `${roles[rule.observation]}：${text}（${rule.purpose}）`;
  }
  return text;
}
export function downloadText(name: string, text: string, type = 'text/plain;charset=utf-8') {
  const url = URL.createObjectURL(new Blob([text], { type }));
  const link = document.createElement('a'); link.href = url; link.download = name; link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
