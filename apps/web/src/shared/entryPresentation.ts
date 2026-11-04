import { useEffect, useRef, useState } from 'react';
import type { Schema } from '../api/client';

/** Continue the server snapshot clock locally so stale READY and deadlines expire together. */
export function useSnapshotTime(asOf?: string) {
  const [elapsed, setElapsed] = useState(0);
  const anchor = useRef(performance.now());
  useEffect(() => {
    anchor.current = performance.now(); setElapsed(0);
    const update = () => setElapsed(performance.now() - anchor.current);
    const interval = setInterval(update, 500);
    document.addEventListener('visibilitychange', update);
    return () => { clearInterval(interval); document.removeEventListener('visibilitychange', update); };
  }, [asOf]);
  return asOf ? Date.parse(asOf) + elapsed : NaN;
}

/** Display the existing server decision; never infer a trigger or broker association here. */
export function entryPresentation(
  advice: Schema['ActionAdvice'] | undefined,
  refresh: Schema['AnalysisRefresh'] | undefined,
  now: number,
  offline = false,
) {
  const fallbackDeadline = advice?.entry_deadline && refresh?.fresh_until
    ? new Date(Math.min(Date.parse(advice.entry_deadline), Date.parse(refresh.fresh_until))).toISOString()
    : advice?.entry_deadline;
  const effectiveEntryDeadline = advice?.entry_timing?.effective_entry_deadline ?? fallbackDeadline;
  const effectiveDeadline = effectiveEntryDeadline ? Date.parse(effectiveEntryDeadline) : Infinity;
  const timedOut = Boolean(advice && ['WAITING', 'READY', 'BLOCKED'].includes(advice.state) && Number.isFinite(effectiveDeadline) && now >= effectiveDeadline);
  const analysisEndsFirst = Boolean(advice && refresh?.fresh_until && Date.parse(refresh.fresh_until) < Date.parse(advice.entry_deadline));
  const stopReason = advice?.stopping_reason ?? (timedOut ? {
    code: analysisEndsFirst ? 'ANALYSIS_EXPIRED' as const : 'ENTRY_DEADLINE' as const,
    occurred_at: new Date(effectiveDeadline).toISOString(), deadline: new Date(effectiveDeadline).toISOString(), condition: null,
    next_step: '停止按原候选新入场，使用最新资料取得新分析；等待后台核验。',
  } : refresh?.stopping_reason);
  const expired = Boolean(advice?.valid_until && now >= Date.parse(advice.valid_until));
  const ready = advice?.state === 'READY' && !expired && !offline && !timedOut && !refresh?.blocks_entry;
  const title = timedOut ? (analysisEndsFirst ? '分析时效已到，停止新入场' : '候选入场期限已到')
    : advice?.state === 'READY' && (expired || offline) ? '报价待更新，暂停入场提示' : advice?.title;
  return { timedOut, stopReason, expired, ready, title, effectiveEntryDeadline };
}
