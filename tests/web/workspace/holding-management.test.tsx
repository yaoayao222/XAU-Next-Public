import React from 'react';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { MemoryRouter, useLocation } from 'react-router-dom';
import { HoldingPanel } from '../../../apps/web/src/features/workspace/HoldingPanel';
import { api, type Schema } from '../../../apps/web/src/api/client';
import { workspace } from '../fixtures';

const at = '2026-10-12T02:00:00Z';
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

function value(): Schema['Workspace'] {
  return { ...workspace, as_of: at, position_advice: [{
    order: { account_scope_id: 'a', broker_id: 'SYNTHETIC', kind: 'position' }, plan_id: 'p',
    state: 'HOLD', reason_code: 'WITHIN_RULES', title: '继续观察已有持仓', reasons: [], evaluated_at: at,
    valid_until: '2026-10-12T02:00:10Z', management: {
      opened_at: '2026-10-12T01:55:00Z', deadline: '2026-10-12T02:15:00Z',
      original_stop: '99', original_target: '103', broker_stop: '99.00', broker_target: '102.90',
      exit_price: '100', target_distance: '3', remaining_seconds: 900,
      structure_status: 'clear', structure_reason: '尚未观察到原结构退出条件；这不代表保证到达目标。',
      reassessment_state: 'none', reassessment_reasons: [], pending_request_id: null, latest_review: null,
    },
  }] };
}
function Location() { return <span data-testid="location">{useLocation().search}</span>; }
function show(w = value(), offline = false, now = Date.parse(at)) {
  return render(<MemoryRouter><HoldingPanel workspace={w} offline={offline} now={now} /><Location /></MemoryRouter>);
}

it('HOLD也显示两层管理、原目标与券商差异，未把距离当概率', () => {
  show();
  expect(screen.getByText('第一层：原规则检查')).toBeTruthy();
  expect(screen.getByText('第二层：目标重评')).toBeTruthy();
  expect(screen.getByText(/原方案与MT5实际设置有差异/)).toBeTruthy();
  expect(screen.getByText(/尚无持仓专项目标重评/)).toBeTruthy();
  expect(screen.getByText(/上述距离只是价格差，不是到达概率/)).toBeTruthy();
});

it('准备持仓包只创建请求，重复点击合并，不调用模型', async () => {
  const w = value();
  const get = vi.spyOn(api, 'workspace').mockResolvedValue(w);
  const create = vi.spyOn(api, 'createRequest').mockResolvedValue({ request_id: 'holding-new' } as Schema['AnalysisRequest']);
  show(w);
  const button = screen.getByRole('button', { name: '准备当前持仓重评包' });
  fireEvent.click(button); fireEvent.click(button);
  await waitFor(() => expect(create).toHaveBeenCalledTimes(1));
  expect(get).toHaveBeenCalledTimes(1);
  expect(create.mock.calls[0][0].holding_review_order).toEqual(w.position_advice![0].order);
  await waitFor(() => expect(screen.getByTestId('location').textContent).toBe('?request=holding-new'));
});

it('退出条件已触发时不引导等待新回答，过时事实不能准备重评', () => {
  const w = value(); w.position_advice![0].state = 'EXIT';
  const view = show(w);
  expect(screen.getByText(/请按已触发的原退出规则处理/)).toBeTruthy();
  expect(screen.queryByRole('button', { name: '准备当前持仓重评包' })).toBeNull();
  view.unmount(); show(value(), true);
  expect((screen.getByRole('button', { name: '准备当前持仓重评包' }) as HTMLButtonElement).disabled).toBe(true);
});

it('旧重评意见过期后标为历史，不继续显示当前支持结论', () => {
  const w = value();
  w.position_advice![0].management!.latest_review = {
    request_id: 'r', version_id: 'v', market_cutoff: at, available_at: at, valid_until: '2026-10-12T01:59:59Z',
    current: true, stale_reasons: [], advice: {
      order: w.position_advice![0].order, action: 'hold', reason: '当时原依据尚在', proposed_stop_loss: null,
      proposed_take_profit: null, proposed_volume_lots: null, valid_until: '2026-10-12T01:59:59Z',
      target_assessment: { target_price: '103', state: 'supported', supporting_evidence: ['当时结构保持'],
        opposing_evidence: [], obstacles: [], time_basis: '原期限内', change_since_previous: '首次', limitations: ['合成'] },
    },
  };
  show(w);
  expect(screen.getByText('历史重评：原目标仍获支持')).toBeTruthy();
  expect(screen.getByText(/该意见不能代表当前有效判断/)).toBeTruthy();
});
