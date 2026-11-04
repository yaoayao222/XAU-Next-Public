import React from 'react';
import { afterEach, expect, it } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { AnswerView } from '../../../apps/web/src/features/analysis/VersionHistory';
import { version } from '../fixtures';
import example from '../../../fixtures/analysis-answer.experience.json';
import type { Schema } from '../../../apps/web/src/api/client';

afterEach(cleanup);
it('旧版明确未提供说明，不推断未使用经验', () => {
  render(<AnswerView version={version} />);
  expect(screen.getByText('该版本未提供结构化经验采用说明')).toBeTruthy();
  expect(screen.queryByText('未使用经验')).toBeNull();
});
it('新字段默认折叠，保持判断可展示且不伪称验证成功', () => {
  const answer = structuredClone(example) as Schema['AnalysisAnswer'];
  answer.experience_application = { digest_id: 'exp-SYNTHETIC', status: 'applied', limitations: ['样本有限'], case_assessments: [{ review_ref: 'review-SYNTHETIC', relevance: 'partial', current_evidence_refs: ['market-SYNTHETIC'], similarities: ['区间边缘'], differences: ['周期不同'], decision: 'adopt', reason: '保持方向，限制适用范围' }] };
  answer.prediction_adjustments = [{ horizon_minutes: 180, aspects: ['direction', 'range'], related_review_refs: ['review-SYNTHETIC'], action: 'retained', explanation: '当前证据支持保持原区间' }];
  const view = render(<AnswerView version={{ ...version, answer }} />);
  const details = view.container.querySelector('details.experience-application') as HTMLDetailsElement;
  expect(details.open).toBe(false);
  fireEvent.click(screen.getByText('经验采用与本次调整 · 已采用经验'));
  expect(screen.getByText('180 分钟 · 保持 · 方向、区间')).toBeTruthy();
  expect(screen.getByText(/字段与引用校验通过不证明解释正确或预测更准/)).toBeTruthy();
});
it.each(['reviewed_no_change', 'unavailable'] as const)('合法展示%s', status => {
  const answer = structuredClone(example) as Schema['AnalysisAnswer'];
  answer.experience_application!.status = status;
  render(<AnswerView version={{ ...version, answer }} />);
  expect(screen.getByText(status === 'unavailable' ? /本包没有可用经验/ : /已评估，未改变判断/)).toBeTruthy();
});
