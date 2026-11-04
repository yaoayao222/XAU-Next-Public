import type { Schema } from '../../api/client';

const statusLabels = { applied: '已采用经验', reviewed_no_change: '已评估，未改变判断', unavailable: '本包没有可用经验' };
const relevanceLabels = { applicable: '适用', partial: '部分适用', not_applicable: '不适用', unknown: '适用性未知' };
const decisionLabels = { adopt: '采用', do_not_adopt: '不采用', uncertain: '暂不能判断' };
const actionLabels = { adjusted: '调整', retained: '保持', caution: '提示限制' };
const aspectLabels = { direction: '方向', range: '区间', phases: '阶段', turning_points: '转折', confidence: '模型自评', reassessment: '重评' };

export function ExperienceApplication({ answer }: { answer: Schema['AnalysisAnswer'] }) {
  const application = answer.experience_application;
  if (!application) return <p className="muted">该版本未提供结构化经验采用说明</p>;
  return <details className="experience-application"><summary>经验采用与本次调整 · {statusLabels[application.status]}</summary>
    {application.case_assessments.length === 0 && <p>本次没有登记采用的具体案例。</p>}
    {application.case_assessments.map(item => <article key={item.review_ref}>
      <h4>{decisionLabels[item.decision]} · {relevanceLabels[item.relevance]}</h4><p>{item.reason}</p>
      {item.similarities.length > 0 && <p>相似之处：{item.similarities.join('；')}</p>}
      {item.differences.length > 0 && <p>重要差异：{item.differences.join('；')}</p>}
      <details><summary>案例与当前依据引用</summary><p>案例：{item.review_ref}</p><p>当前依据：{item.current_evidence_refs.join('、') || '未提供'}</p></details>
    </article>)}
    {(answer.prediction_adjustments ?? []).map((item, i) => <article key={i}><h4>{item.horizon_minutes} 分钟 · {actionLabels[item.action]} · {item.aspects.map(aspect => aspectLabels[aspect]).join('、')}</h4><p>{item.explanation}</p><small>关联案例：{item.related_review_refs.join('、')}</small></article>)}
    {application.limitations.length > 0 && <p>{application.limitations.join('；')}</p>}
    <p className="muted">以上为模型提供的采用说明；字段与引用校验通过不证明解释正确或预测更准。模型自评不代表验证胜率。</p>
  </details>;
}
