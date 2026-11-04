import type { Schema } from '../api/client';

export function ExperienceEffectNotice({ value }: { value?: Schema['ExperienceEffectDiagnostics'] }) {
  if (!value) return null;
  return <aside className="notice" aria-label="经验关联只读核查">
    <strong>采用{value.adopted_count}条经验，漏关联{value.missing_count}条</strong>
    <p>已关联{value.linked_count}条；请核对下面列出的原案例。采用理由中已有文字，也需要与预测影响说明建立对应关系。</p>
    {value.missing_cases.map(item => <details key={item.case_number}>
      <summary>第{item.case_number}条经验尚未关联影响说明</summary>
      <p>原采用理由：{item.reason}</p><small>原经验编号：{item.review_ref}</small>
    </details>)}
    <p className="muted">按保存原文只读核查，原错误和失败记录保留；未自动补关联或重新导入。保持原判断也可以说明影响。</p>
  </aside>;
}
