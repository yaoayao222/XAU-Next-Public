import { useRef, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { api, commandKey, type Schema } from '../../api/client';
import { Badge, ErrorNotice, Field, JobPanel } from '../../shared/ui';
import { downloadText, time } from '../../shared/format';

export const comparisonState: Record<string, string> = { prepared: '尚未发送', queued: '已登记，等待执行', running: '正在接收回答', awaiting_review: '回答已保存，等待到期评分', completed: '各窗口评分已完成', failed: '执行失败，未重发', cancelled: '已停止，未重发', interrupted: '执行中断，未重发' };
const armState: Record<string, string> = { not_sent: '尚未发送', succeeded: '回答合格', failed: '失败', outcome_unknown: '状态未知，不重发' };
const direction: Record<string, string> = { long: '看涨', short: '看跌', neutral: '持平', uncertain: '不明确' };
const resultText: Record<string, string> = { correct: '方向命中', incorrect: '方向未命中', uncertain: '判断不明确', unknown: '资料不足，未知', not_sent: '未发送', failed: '回答失败', outcome_unknown: '调用状态未知' };

export function ComparisonRunPanel({ record, readOnly = false }: { record: Schema['AnalysisComparison']; readOnly?: boolean }) {
  const cache = useQueryClient();
  const [authorize, setAuthorize] = useState(false);
  const [cpa, setCpa] = useState('');
  const [deepseek, setDeepseek] = useState('');
  const [accepted, setAccepted] = useState(false);
  const [history, setHistory] = useState(false);
  const [scope, setScope] = useState('after_common_available');
  const [answer, setAnswer] = useState<Schema['ComparisonAnswer'] | null>(null);
  const [scoreJob, setScoreJob] = useState<string | null>(null);
  const key = useRef({ body: '', key: '' });
  const profiles = useQuery({ queryKey: ['comparison-profiles'], queryFn: api.modelProfiles, enabled: authorize });
  const execution = record.execution;
  async function invalidate() {
    await Promise.all([cache.invalidateQueries({ queryKey: ['analysis-comparison', record.comparison_id] }), cache.invalidateQueries({ queryKey: ['analysis-lab'] })]);
  }
  const start = useMutation({ mutationFn: (body: Schema['ComparisonRunCreate']) => {
    const fingerprint = JSON.stringify({ id: record.comparison_id, body });
    if (key.current.body !== fingerprint) key.current = { body: fingerprint, key: commandKey() };
    return api.runComparison(record.comparison_id, body, key.current.key);
  }, onSuccess: () => { setAuthorize(false); void invalidate(); } });
  const score = useMutation({ mutationFn: (advance_simulation: boolean) => api.scoreComparison(record.comparison_id, { advance_simulation }), onSuccess: job => { setScoreJob(job.job_id); void invalidate(); } });
  const viewAnswer = useMutation({ mutationFn: (arm: 'A' | 'B' | 'C' | 'D') => api.comparisonAnswer(record.comparison_id, arm), onSuccess: setAnswer });
  const rows = execution?.scores.filter(r => r.scope === scope) ?? [];
  if (record.purpose !== 'comparison') return null;
  return <div className="comparison-run">
    <h4>比较执行与到期评分</h4>
    {record.data_kind === 'SYNTHETIC' && <div className="notice">模拟验收：四份回答和后续行情均为合成数据，不调用真实模型，模拟成绩不能用于选择模型。</div>}
    <ErrorNotice error={start.error ?? score.error ?? viewAnswer.error ?? profiles.error} />
    {!execution && !readOnly && <>
      {record.data_kind === 'SYNTHETIC' ? <button className="primary" disabled={start.isPending} onClick={() => start.mutate({ mode: 'simulation' })}>运行模拟比较（不收费）</button> : <>
        <p>真实比较不会自动开始。本组须单独授权最多4次调用并选择已有配置；失败、超时和未知都保留次数，不自动补发。正式日常配置不会切换。</p>
        <button disabled={start.isPending} onClick={() => setAuthorize(!authorize)}>设置本组真实比较授权</button>
      </>}
      {authorize && <form className="comparison-authorization" onSubmit={event => { event.preventDefault(); start.mutate({ mode: 'real', max_calls: 4, cpa_profile_id: cpa, deepseek_profile_id: deepseek, acknowledge_possible_charge: true, accept_unknown_cost: accepted, allow_readonly_history: history, authorization_text: '授权本组最多4次真实模型调用' }); }}>
        <p>四个模型使用同一份冻结资料，均为high。必须使用30秒内的行情冻结，派发不超过60秒；过期就停止，不自动换新包。不同组起点至少间隔180分钟。</p>
        <Field label="Sol与Astra共用的已有CPA配置"><select value={cpa} onChange={e => setCpa(e.target.value)}><option value="">请选择</option>{profiles.data?.items.filter(p => p.base_url.startsWith('http://127.0.0.1:')).map(p => <option key={p.profile_id} value={p.profile_id}>{p.name}</option>)}</select></Field>
        <Field label="Flash与Pro共用的已有DeepSeek官方配置"><select value={deepseek} onChange={e => setDeepseek(e.target.value)}><option value="">请选择</option>{profiles.data?.items.filter(p => p.base_url === 'https://api.deepseek.com/v1').map(p => <option key={p.profile_id} value={p.profile_id}>{p.name}</option>)}</select></Field>
        <label><input type="checkbox" checked={accepted} onChange={e => setAccepted(e.target.checked)} />我明确授权本组最多4次真实模型调用，接受实际费用未知；失败或未知不重发。</label>
        <label><input type="checkbox" checked={history} onChange={e => setHistory(e.target.checked)} />允许窗口结束后自动只读获取评分所需行情；交易仍由我手工操作。</label>
        <button className="primary" disabled={!cpa || !deepseek || !accepted || !history || start.isPending}>确认授权并开始本组4次比较（可能收费）</button>
        <small>本页不会要求发送密钥。需要的凭据仍由已有配置在本机保存，不会因为打开此表单发起模型请求。</small>
      </form>}
    </>}
    {execution && <>
      <p><Badge tone={execution.state === 'completed' ? 'good' : 'warn'}>{comparisonState[execution.state]}</Badge> 本组上限 {execution.group_call_limit} 次 · 已占次数 {execution.attempts_reserved} · 真实发送 {execution.real_calls_sent} 次{execution.mode === 'simulation' && ` · 模拟发送 ${execution.simulation_calls_sent} 次`}</p>
      <p>{execution.message}</p>
      <p className="muted">回答通过格式和业务校验，只说明回答可用；方向是否命中要等窗口结束。计次包含已经占用但结果未知的请求。</p>
      <div className="table-scroll"><table><thead><tr><th>模型</th><th>回答状态</th><th>实际可用时间</th><th>原文</th></tr></thead><tbody>{record.arms.map(a => <tr key={a.arm}><td>{a.model_id}<small>high</small></td><td>{armState[a.state] ?? a.state}{a.error_code && <small>{a.error_code}</small>}</td><td>{time(a.available_at)}</td><td><button disabled={!a.answer_available || viewAnswer.isPending} onClick={() => viewAnswer.mutate(a.arm)}>查看{a.arm}回答</button></td></tr>)}</tbody></table></div>
      {!readOnly && ['queued', 'running'].includes(execution.state) && <JobPanel jobId={execution.job_id} onComplete={() => { void invalidate(); }} />}
      {!readOnly && execution.state === 'awaiting_review' && <div className="actions">
        {execution.mode === 'simulation' && <button disabled={score.isPending} onClick={() => score.mutate(true)}>模拟窗口结束并验收评分（不收费）</button>}
        <button disabled={score.isPending} onClick={() => score.mutate(false)}>检查已到期窗口评分</button>
      </div>}
      {scoreJob && <JobPanel jobId={scoreJob} onComplete={() => { void invalidate(); }} />}
      {!!execution.scores.length && <div className="comparison-scores"><h4>方向与区间表现（按周期分别看）</h4>
        {execution.simulation_advanced && <p className="notice">这里推进的是模拟时间，真实时钟与正式行情没有改变。下表全部是合成验收成绩。</p>}
        <Field label="比较时间口径"><select value={scope} onChange={e => setScope(e.target.value)}><option value="after_common_available">全部合格回答共同可用后</option><option value="after_own_available">各模型自己的回答可用后</option><option value="full_descriptive">原截止窗口回看（包含生成等待）</option></select></Field>
        <p className="muted">每个周期保留全部4个登记模型，失败、未发送、不明确和未知均保留。共同30分钟窗口剩余不足15分钟时单列不可用；不同周期不能当作独立样本混算。</p>
        {[30, 60, 180].map(minutes => {
          const group = rows.filter(r => r.minutes === minutes);
          const clear = group.filter(r => r.direction && r.direction !== 'uncertain').length;
          const verified = group.filter(r => ['correct', 'incorrect'].includes(r.direction_class) && r.usable_primary !== false);
          const hit = verified.filter(r => r.direction_class === 'correct').length;
          return <p className="muted" key={minutes}>{minutes}分钟：明确方向 {clear}/4 · 方向已核验 {verified.length}/4 · 命中 {verified.length ? `${hit}/${verified.length}个可核验判断` : '尚不可核验'}（全部登记 {hit}/4）</p>;
        })}
        <div className="table-scroll"><table><thead><tr><th>周期 / 模型</th><th>原方向判断</th><th>方向结果</th><th>上下界平均误差</th><th>内部M1覆盖</th></tr></thead><tbody>{rows.map(r => <tr key={`${r.arm}-${r.minutes}`}><td>{r.minutes}分钟 / {r.arm}</td><td>{r.direction ? direction[r.direction] : '无合格回答'}</td><td>{r.state === 'pending' ? '窗口未结束' : resultText[r.direction_class] ?? r.direction_class}{r.usable_primary === false && <small>共同窗口不足，不计主要比较</small>}</td><td>{r.range_boundary_mae ?? '未知'}</td><td>{r.range_contained === null ? '未知' : r.range_contained ? '包含' : '漏出'}</td></tr>)}</tbody></table></div>
        <p className="muted">区间误差以金价本身的美元价差计算，取上下界绝对误差的平均；内部M1统计不是完整逐笔路径极值。当前不自动选冠军。</p>
      </div>}
    </>}
    {answer && <details className="comparison-answer" open><summary>{answer.arm}回答原文 · {answer.business_valid ? '校验合格' : '未通过校验'}</summary><button onClick={() => downloadText(`comparison-${record.comparison_id}-${answer.arm}.json`, answer.answer_text)}>下载回答原文</button><pre className="packet">{answer.answer_text}</pre></details>}
  </div>;
}
