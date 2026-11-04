import type { Schema } from '../../api/client';

export function ForecastBasis({ value }: { value: Schema['ForecastVersion']['answer']['horizons'][number]['forecast_basis'] }) {
  if (!value) return null;
  return <details><summary>本周期依据与反对证据</summary>
    <p>方向按窗口端点净变化评价；中性不等于震荡，范围覆盖不等于准确。</p>
    <h4>方向依据</h4><p>{value.direction_basis}</p>
    <h4>区间上下界依据</h4><p>{value.range_basis}</p>
    <h4>支持证据</h4><ul>{value.supporting_evidence.map((text, i) => <li key={i}>{text}</li>)}</ul>
    <h4>反对证据</h4><ul>{value.opposing_evidence.map((text, i) => <li key={i}>{text}</li>)}</ul>
    {value.limitations.length > 0 && <p>{value.limitations.join('；')}</p>}
    <small>依据引用：{value.evidence_refs.join('、')}</small>
  </details>;
}
