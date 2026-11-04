import type { Schema } from '../api/client';

export function ResponseFailureNotice({ value }: { value?: Schema['ResponseFailureDiagnosis'] }) {
  if (!value) return null;
  return <aside className="notice" aria-label="原响应只读核查">
    <strong>原响应核查：渠道报告上游生成失败</strong>
    <p>{value.detail}</p>
    <p className="muted">仅根据保存的响应补充说明；原错误、失败记录及原文未改写。</p>
  </aside>;
}
