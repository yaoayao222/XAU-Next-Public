import { useEffect, useRef, type ReactNode } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { api, ApiProblem, type Schema } from '../api/client';
import { label, time } from './format';

export function Badge({ children, tone = 'neutral' }: { children: ReactNode; tone?: 'neutral' | 'good' | 'warn' | 'bad' }) { return <span className={`badge ${tone}`}>{children}</span>; }
export function Empty({ title, children }: { title: string; children?: ReactNode }) { return <div className="empty"><strong>{title}</strong>{children && <p>{children}</p>}</div>; }
export function ErrorNotice({ error }: { error: unknown }) {
  if (!error) return null;
  const problem = error instanceof ApiProblem ? error.problem : null;
  return <div className="notice error" role="alert"><strong>{problem?.title ?? '暂时无法完成'}</strong><p>{error instanceof Error ? error.message : '请求失败，请重试。'}</p>{problem?.field_errors.length ? <ul>{problem.field_errors.map((field, index) => <li key={index}><code>{field.path}</code>：{field.message}</li>)}</ul> : null}{problem && <details><summary>错误详情</summary><p>{problem.code} · 追踪 {problem.trace_id}</p></details>}{problem?.status === 401 && <p>会话已失效，请通过本机启动器重新打开页面。</p>}</div>;
}
export function PageHeading({ eyebrow, title, children, actions }: { eyebrow: string; title: string; children: ReactNode; actions?: ReactNode }) { return <header className="page-heading"><div><p className="eyebrow">{eyebrow}</p><h1>{title}</h1><p className="lede">{children}</p></div>{actions && <div className="actions">{actions}</div>}</header>; }
export function Field({ label: text, children, hint }: { label: string; children: ReactNode; hint?: string }) { return <label className="field"><span>{text}</span>{children}{hint && <small>{hint}</small>}</label>; }
export function Metric({ label: text, value, note }: { label: string; value: ReactNode; note?: ReactNode }) { return <div className="metric"><span>{text}</span><strong>{value}</strong>{note && <small>{note}</small>}</div>; }
export function SourceLine({ status, zone }: { status: Schema['SourceStatus']; zone?: string }) { return <div className="source-line"><Badge tone={status.state === 'available' ? 'good' : 'warn'}>{label(status.state)}</Badge><span>事实 {time(status.observed_at, zone)} · 读取 {time(status.captured_at, zone)}</span>{status.reasons.length > 0 && <span>{status.reasons.join('；')}</span>}</div>; }
export function JobPanel({ jobId, onComplete }: { jobId: string; onComplete?: (job: Schema['Job']) => void }) {
  const queryClient = useQueryClient();
  const handled = useRef('');
  const query = useQuery({ queryKey: ['job', jobId], queryFn: () => api.job(jobId), refetchInterval: query => ['queued', 'running', 'cancelling'].includes(query.state.data?.state ?? 'queued') ? 1000 : false });
  const cancel = useMutation({ mutationFn: () => api.cancelJob(jobId), onSuccess: () => queryClient.invalidateQueries({ queryKey: ['job', jobId] }) });
  const job = query.data;
  useEffect(() => {
    if (job && ['succeeded', 'failed', 'cancelled'].includes(job.state) && handled.current !== `${job.job_id}:${job.state}`) {
      handled.current = `${job.job_id}:${job.state}`;
      void queryClient.invalidateQueries(); onComplete?.(job);
    }
  }, [job, onComplete, queryClient]);
  if (!job) return <><p role="status">正在查询后台任务…</p><ErrorNotice error={query.error} /></>;
  return <div className="job" aria-live="polite"><div className="row-between"><strong>{label(job.state)} · {job.message}</strong>{['queued', 'running'].includes(job.state) && <button className="text-button" disabled={cancel.isPending} onClick={() => cancel.mutate()}>取消任务</button>}</div>{job.progress !== null && <progress max={1} value={job.progress} aria-label="后台任务进度" />}<ErrorNotice error={job.error ? new ApiProblem(job.error) : query.error ?? cancel.error} /></div>;
}
export function PageControls({ next, onNext, onFirst, loading, note = '每页最多 50 条，筛选只作用于本页。' }: { next: string | null | undefined; onNext: () => void; onFirst: () => void; loading?: boolean; note?: string }) { return <div className="pagination"><button onClick={onFirst} disabled={loading}>回到首页</button><button onClick={onNext} disabled={!next || loading}>下一页</button><small>{note}</small></div>; }
