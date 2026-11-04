import { render, screen } from '@testing-library/react';
import { expect, it } from 'vitest';
import { ForecastBasis } from '../../../apps/web/src/features/analysis/ForecastBasis';
import { ruleText } from '../../../apps/web/src/shared/format';

it('shows horizon evidence, counterevidence and endpoint semantics without changing legacy answers', () => {
  const { rerender } = render(<ForecastBasis value={undefined} />);
  expect(screen.queryByText('本周期依据与反对证据')).toBeNull();
  rerender(<ForecastBasis value={{ direction_basis: '端点依据', range_basis: '上下界依据',
    supporting_evidence: ['支持事实'], opposing_evidence: ['反对事实'], evidence_refs: ['snapshot-a'], limitations: ['资料缺口'] }} />);
  expect(screen.getByText('反对事实')).toBeDefined();
  expect(screen.getByText(/中性不等于震荡/)).toBeDefined();
  expect(screen.getByText('资料缺口')).toBeDefined();
});

it('distinguishes background, availability and post-activation observations', () => {
  const base = { kind: 'bar_close' as const, timeframe: 'M5' as const, operator: 'lt' as const, price: '4102.10' };
  expect(ruleText(base)).toBe('M5 已收盘价 < 4102.10');
  expect(ruleText({ ...base, observation: 'background', purpose: '上方结构仍保持' })).toContain('持续背景');
  expect(ruleText({ ...base, observation: 'new_since_available', purpose: '新确认' })).toContain('可用后新观察');
  expect(ruleText({ ...base, observation: 'new_since_activation', purpose: '后续确认' })).toContain('激活后新观察');
});
