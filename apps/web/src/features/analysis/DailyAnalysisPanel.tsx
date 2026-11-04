import { useRef, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { api, commandKey, type Schema } from '../../api/client';
import { useWorkspace } from '../../api/queries';
import { Badge, ErrorNotice, Field, JobPanel, PageControls } from '../../shared/ui';
import { time } from '../../shared/format';
import { DailyResults } from './DailyResults';
import { AnalysisLabPanel } from './AnalysisLabPanel';

const models = [{ key: 'A', name: 'Sol' }, { key: 'B', name: 'Astra' }, { key: 'C', name: 'DeepSeek Flash' }, { key: 'D', name: 'DeepSeek Pro' }] as const;
type Key = 'A' | 'B' | 'C' | 'D';
type Attempt = { fingerprint: string; prepareKey: string; runKey: string; body: Schema['DailyPrepare']; group?: Schema['DailyGroup'] };

export function DailyAnalysisPanel() {
  const cache = useQueryClient(); const workspace = useWorkspace(); const [params, setParams] = useSearchParams();
  const [template, setTemplate] = useState<'current' | 'improved' | 'candidate24' | 'candidate25' | 'candidate26' | 'candidate27' | 'candidate28'>('candidate26');
  const [chosen, setChosen] = useState<Key[]>(['A']); const [kind, setKind] = useState<'new' | 'update'>('new');
  const [horizons, setHorizons] = useState([30, 60, 180]); const [accepted, setAccepted] = useState(false);
  const [selectedModels, setSelectedModels] = useState<Schema['DailyModelSelection'][]>([]); const [cursor, setCursor] = useState<string>();
  const [sourceJob, setSourceJob] = useState<string | null>(null); const [stage, setStage] = useState('');
  const [baseRound, setBaseRound] = useState<Schema['DailyGroup'] | null>(null);
  const attempt = useRef<Attempt | null>(null);
  const supported = !!workspace.data && Object.hasOwn(workspace.data, 'daily_group');
  const groups = useQuery({ queryKey: ['daily-groups', cursor], queryFn: () => api.dailyGroups(cursor), enabled: supported, refetchInterval: q => q.state.data?.items.some(g => g.state === 'running') ? 2000 : false });
  const id = params.get('group') ?? groups.data?.items[0]?.group_id;
  const selected = useQuery({ queryKey: ['daily-group', id], queryFn: () => api.dailyGroup(id!), enabled: !!id, refetchInterval: q => q.state.data?.state === 'running' ? 1500 : false });
  const research = useQuery({ queryKey: ['analysis-lab'], queryFn: api.analysisLab, enabled: supported });
  const profiles = useQuery({ queryKey: ['model-profiles'], queryFn: api.modelProfiles, enabled: supported });
  const active = selected.data;
  const options = (profiles.data?.items ?? []).filter(p => p.enabled !== false).flatMap(p => {
    const ids = p.model_ids ?? (p.base_url.startsWith('http://127.0.0.1:') ? ['gpt-6.1-sol','gpt-6-astra'] : p.base_url === 'https://api.deepseek.com/v1' ? ['deepseek-flash','deepseek-v4-pro'] : [p.model_id]);
    return ids.map(model_id => ({ profile_id: p.profile_id, model_id, provider: p.name, effort: p.reasoning_effort ?? 'high' }));
  });
  const defaultOption = options.find(o => o.profile_id === profiles.data?.active_profile_id && o.model_id === 'gpt-6.1-sol') ?? options.find(o => o.profile_id === profiles.data?.active_profile_id) ?? options[0];
  // Empty initial draft selects one saved connection; an explicit empty choice stays empty.
  const [hasChosen, setHasChosen] = useState(false);
  const routing = hasChosen ? selectedModels : defaultOption ? [{ model_key: 'A' as Key, profile_id: defaultOption.profile_id, model_id: defaultOption.model_id }] : [];
  const packageKind = kind === 'update' && baseRound ? baseRound.package_kind : template;
  const synthetic = workspace.data?.market?.source_kind === 'synthetic';
  const effectiveChosen = synthetic ? chosen : routing.map(s => s.model_key);
  const channelReady = synthetic || routing.length > 0 && routing.every(s => options.some(o => o.profile_id === s.profile_id && o.model_id === s.model_id));
  const validBase = kind === 'new' || Boolean(baseRound && effectiveChosen.every(key => baseRound.arms.some(a => a.model_key === key && a.version_id)));
  const ready = supported && effectiveChosen.length > 0 && (kind === 'update' || horizons.length > 0) && validBase && (packageKind === 'current' || !research.isPending);
  function choose(key: Key) { setChosen(items => items.includes(key) ? items.filter(k => k !== key) : models.filter(m => items.includes(m.key) || m.key === key).map(m => m.key)); setAccepted(false); }
  function chooseSaved(profile_id: string, model_id: string) {
    const existing = routing.find(s => s.profile_id === profile_id && s.model_id === model_id);
    const next = existing ? routing.filter(s => s !== existing) : routing.length < 4 ? [...routing, { model_key: (kind === 'update' ? baseRound?.arms.find(a => a.profile_id === profile_id && a.model_id === model_id)?.model_key : undefined) ?? (['A','B','C','D'] as Key[]).find(k => !routing.some(s => s.model_key === k))!, profile_id, model_id }] : routing;
    setSelectedModels(next); setHasChosen(true); setAccepted(false);
  }
  async function refreshSources() {
    const month = new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit' }).format(new Date());
    const job = await api.refreshResearch({ month }); setSourceJob(job.job_id); setStage('正在准备共同资料，不调用模型…');
    const deadline = Date.now() + 180000;
    while (Date.now() < deadline) {
      const state = await api.job(job.job_id);
      if (state.state === 'succeeded') { const result = await api.analysisLab(); cache.setQueryData(['analysis-lab'], result); return result.bundle; }
      if (['failed', 'cancelled'].includes(state.state)) throw new Error(state.error?.detail ?? '资料准备未完成，模型未发送。');
      await new Promise(resolve => setTimeout(resolve, 1000));
    }
    throw new Error('资料任务尚未确认完成；请查看原任务，未调用模型。');
  }
  const prepare = useMutation({ mutationFn: async (send: boolean) => {
    if (!ready) throw new Error('请选择模板、模型和预测周期；更新需要所选模型自己的上一版。');
    if (send && !synthetic && !accepted) throw new Error('请先明确同意本轮调用次数、未知费用和只读评分。');
    if (send && !channelReady) throw new Error('所选模型还缺少保存的连接，请先在设置页补齐；分析包仍可准备或手工使用。');
    // Connection selection affects dispatch, not the already frozen analysis.
    const setup = { packageKind, chosen: effectiveChosen, routing: synthetic ? null : routing, kind, base: kind === 'update' ? baseRound?.group_id : null, horizons };
    const fingerprint = JSON.stringify(setup);
    if (!attempt.current || attempt.current.fingerprint !== fingerprint) {
      let bundle = research.data?.bundle;
      if (packageKind !== 'current' && !bundle) bundle = await refreshSources();
      setStage('正在冻结行情与本轮资料…');
      const fresh = await api.dailyAnalysisSource();
      if (!fresh.market_snapshot_id) throw new Error('尚无可用行情快照，请先确认MT5只读连接。');
      attempt.current = { fingerprint, prepareKey: commandKey(), runKey: commandKey(), body: {
        package_kind: packageKind, models: [...effectiveChosen], ...(synthetic || !routing.length ? {} : { selections: routing }), kind, base_group_id: setup.base ?? null,
        bundle_id: packageKind !== 'current' ? bundle?.bundle_id ?? null : null,
        market_snapshot_id: fresh.market_snapshot_id, horizon_minutes: kind === 'new' ? horizons : null,
      } };
    }
    const current = attempt.current;
    if (!current.group) current.group = await api.prepareDaily(current.body, current.prepareKey);
    const group = current.group;
    setParams({ group: group.group_id }); cache.setQueryData(['daily-group', group.group_id], group);
    if (!send) return group;
    setStage(synthetic ? '登记模拟回答，不访问真实模型…' : '登记本轮调用，失败或未知不会自动补发…');
    return api.runDaily(group.group_id, { mode: synthetic ? 'simulation' : 'real', max_calls: group.models.length,
      acknowledge_possible_charge: true, accept_unknown_cost: true, allow_readonly_history: true,
      cpa_profile_id: null, deepseek_profile_id: null }, current.runKey);
  }, onSuccess: group => {
    cache.setQueryData(['daily-group', group.group_id], group); setStage(group.run_registered ? '本轮已登记；查看下方结果。如需新的时点，请点“重新准备新一轮”。' : '资料已保存，尚未发送。可手工取得回答，或在资料仍新鲜时明确启动所选模型。'); setSourceJob(null); setAccepted(false);
    void cache.invalidateQueries({ queryKey: ['daily-groups'] }); void cache.invalidateQueries({ queryKey: ['workspace'] });
  }, onError: () => {
    setAccepted(false);
    setStage(attempt.current?.group
      ? '本轮启动未确认，请查看已保存轮次的任务状态；不会自动重发。'
      : '资料准备未完成，本轮未启动模型。请查看下方原因。');
  } });
  return <section className="daily-analysis panel">
    <div className="row-between"><div><h2>选择分析包与模型</h2><p>日常分析和模型比较在同一轮完成，回答独立保存，到期进入统一复盘。</p></div><Badge>一轮 · 多模型</Badge></div>
    <div className="daily-setup-grid"><Field label="本次分析包模板"><select value={packageKind} disabled={prepare.isPending || kind === 'update'} onChange={e => { setTemplate(e.target.value as typeof template); setAccepted(false); }}><option value="current">14版 · 现有日常分析包</option><option value="improved">23版 · 改进共同资料包</option>{kind === 'update' && baseRound?.package_kind === 'candidate24' && <option value="candidate24">24版 · 原历史轮次更新</option>}{kind === 'update' && baseRound?.package_kind === 'candidate25' && <option value="candidate25">25版 · 原历史轮次更新</option>}<option value="candidate26">26版 · JSON结构检查（默认）</option><option value="candidate27">27版 · 字段只写一次（候选）</option><option value="candidate28">28版 · 经验关联逐条核对（候选）</option></select></Field>
      <Field label="本次分析方式"><select value={kind} disabled={prepare.isPending} onChange={e => { const value = e.target.value as typeof kind; setKind(value); setAccepted(false); setBaseRound(value === 'update' ? active ?? null : null); if (value === 'update' && active) { const arms = active.arms.filter(a => a.version_id); setChosen(arms.map(a => a.model_key)); setSelectedModels(arms.filter(a => a.profile_id).map(a => ({ model_key: a.model_key, profile_id: a.profile_id!, model_id: a.model_id }))); setHasChosen(true); } }}><option value="new">新一轮分析</option><option value="update" disabled={!active?.arms.some(a => a.version_id)}>窗口内更新 · 各自沿用上一版</option></select></Field></div>
    <fieldset className="daily-model-options" disabled={prepare.isPending}><legend>参与分析的模型（可选1—4个）</legend>{synthetic ? models.map(m => <label key={m.key}><input type="checkbox" checked={chosen.includes(m.key)} onChange={() => choose(m.key)} disabled={kind === 'update' && !baseRound?.arms.some(a => a.model_key === m.key && a.version_id)} /><span>{m.name}<small>high</small></span></label>) : options.map(o => {
      const checked = routing.some(s => s.profile_id === o.profile_id && s.model_id === o.model_id);
      return <label key={o.profile_id + ':' + o.model_id}><input type="checkbox" checked={checked} disabled={kind === 'update' && !baseRound?.arms.some(a => a.version_id && a.profile_id === o.profile_id && a.model_id === o.model_id) || !checked && routing.length >= 4} onChange={() => chooseSaved(o.profile_id, o.model_id)} /><span>{o.model_id} · {o.provider}<small>{o.effort === 'omit' ? '未发送推理档位' : o.effort}</small></span></label>;
    })}</fieldset>
    {!synthetic && <p className="muted">每个选项是一组“模型＋供应商”，同一模型可选择不同渠道并列分析。最多4份回答；在设置页管理连接和可选模型。窗口内更新保留各自的模型和渠道。</p>}
    <details className="daily-options"><summary>预测周期与连接设置</summary>
      <fieldset disabled={prepare.isPending || kind === 'update'}><legend>预测周期（分钟）</legend>{[30, 60, 180, 360, 720].map(m => <label key={m}><input type="checkbox" checked={(kind === 'update' ? baseRound?.horizon_minutes ?? [] : horizons).includes(m)} onChange={() => { setHorizons(values => values.includes(m) ? values.filter(v => v !== m) : [...values, m].sort((a, b) => a - b)); setAccepted(false); }} />{m}分钟</label>)}</fieldset>
      <p className="muted">使用所选连接保存的协议、推理档位和输出上限，不切换设置页当前连接。首次真实渠道表现仍待实测。</p>
    </details>
    {packageKind === 'candidate28' && <p className="notice">28版候选保留27版全部资料与时间规则，增加采用案例与预测影响说明的逐条对应核对。不保证模型必然遵从；只准备不会发送模型。</p>}
    {packageKind === 'candidate27' && <p className="notice">27版候选保留26版全部资料和时间规则，增加根字段只写一次的生成顺序及完成核对。旧26包不变，不保证模型必然遵从；只准备不会发送模型。</p>}
    {packageKind === 'candidate26' && <p className="notice">26版保留25版资料与时间表，增加JSON层级检查；不改变方向判断，不能保证模型永不出错。选择或仅准备不会发送模型。</p>}
    {packageKind === 'candidate25' && <p className="notice">当前沿原25版窗口更新，保留原模板和端点。新一轮使用26版，旧记录不改写；选择或仅准备不会发送模型。</p>}
    {packageKind === 'candidate24' && <p className="notice">当前沿原24版窗口更新，保留原模板和端点。新一轮使用25版，旧记录不改写；选择或仅准备不会发送模型。</p>}
    {packageKind !== 'current' && <AnalysisLabPanel sourcesOnly />}
    <div className="daily-run-summary"><strong>本轮选择：{packageKind === 'current' ? '14版' : packageKind === 'candidate28' ? '28版（候选）' : packageKind === 'candidate27' ? '27版（候选）' : packageKind === 'candidate26' ? '26版' : packageKind === 'candidate25' ? '25版' : packageKind === 'candidate24' ? '24版（试用）' : '23版'}＋{effectiveChosen.length}个模型</strong><p>{synthetic ? '当前为合成验收，使用本机模拟回答，不产生真实模型费用。' : `开始后本轮最多${effectiveChosen.length}次真实模型调用，实际费用未知；准备包不收费。`}</p>
      {!channelReady && <p className="notice">尚无可用的所选模型连接。请在设置页保存供应商、准确模型编号和密钥，并启用分析入口。</p>}
      {!synthetic && <label><input type="checkbox" checked={accepted} onChange={e => setAccepted(e.target.checked)} disabled={prepare.isPending} />我明确同意本轮最多{effectiveChosen.length}次调用及未知费用，并允许到期只读获取行情评分；失败或未知不自动补发。</label>}
      <div className="actions"><button disabled={!ready || prepare.isPending} onClick={() => prepare.mutate(false)}>仅准备分析包（不发送）</button><button className="primary" disabled={!ready || !channelReady || prepare.isPending || (!synthetic && !accepted)} onClick={() => prepare.mutate(true)}>{prepare.isPending ? '正在准备与登记…' : synthetic ? '开始模拟分析（不收费）' : `开始所选${effectiveChosen.length}个模型分析`}</button><button disabled={prepare.isPending} onClick={() => { attempt.current = null; prepare.reset(); setStage(''); setSourceJob(null); setAccepted(false); setKind('new'); }}>重新准备新一轮</button></div>
      {stage && <p role="status">{stage}</p>}{sourceJob && <JobPanel jobId={sourceJob} />}
    </div>
    <ErrorNotice error={prepare.error ?? groups.error ?? selected.error ?? profiles.error ?? research.error} />
    {!!groups.data?.items.length && <details className="daily-round-list"><summary>已保存分析轮次（本页{groups.data.items.length}轮）</summary>{groups.data.items.map(g => <button key={g.group_id} className={g.group_id === id ? 'selected' : ''} onClick={() => { setParams({ group: g.group_id }); setKind('new'); }}>{time(g.market_cutoff)} · {g.template_version} · {g.models.length}个模型 · {g.state === 'running' ? '处理中' : g.state === 'prepared' ? '已准备' : '记录已保存'}</button>)}<PageControls note="轮次每页20条。" next={groups.data.next_cursor} onFirst={() => setCursor(undefined)} onNext={() => setCursor(groups.data!.next_cursor!)} /></details>}
    {active && <DailyResults group={active} workspace={workspace.data} />}
  </section>;
}
