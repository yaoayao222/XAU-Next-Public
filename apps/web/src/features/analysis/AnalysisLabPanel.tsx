import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { api, type Schema } from '../../api/client';
import { Badge, Empty, ErrorNotice, Field, JobPanel } from '../../shared/ui';
import { downloadText, time } from '../../shared/format';
import { ComparisonRunPanel, comparisonState } from './ComparisonRunPanel';

const quoteName: Record<string, string> = { DXY: '美元指数', US2Y: '美国2年期收益率', US10Y: '美国10年期收益率' };
const quoteState: Record<string, string> = { fresh_snapshot: '近期快照，实时性未认证', stale: '旧值，仅作背景', time_unknown: '观测时间未核实', unavailable: '尚未取得' };
const sources: Record<string, string> = { 'bls-calendar': '美国劳工统计局官方日历', 'bls-cpi': '美国CPI与核心CPI报告', 'bls-ppi': '美国PPI报告', 'bls-jobs': '美国非农就业与失业率报告', 'bea-calendar': '美国BEA官方日历', 'bea-gdp': '美国GDP报告', 'bea-pce': '美国PCE与核心PCE报告', 'fed-policy': '美联储政策消息', 'fed-speeches': '美联储讲话与证词', 'bbc-world': 'BBC国际新闻', 'bbc-business': 'BBC财经新闻', 'treasury-daily': '美国财政部日度收益率参考', calendar: 'Forex Factory备用日历', umich: '密歇根官方实际值' };
const sourceName = (id: string) => id.startsWith('jin10') ? '金十日历（可选）' : sources[id] ?? quoteName[id] ?? id;

export function AnalysisLabPanel({ sourcesOnly = false, archiveOnly = false }: { sourcesOnly?: boolean; archiveOnly?: boolean } = {}) {
  const [open, setOpen] = useState(false);
  return <section className="panel analysis-lab">
    <div className="row-between"><div><h2>{sourcesOnly ? '本轮共同资料来源与更新' : archiveOnly ? '以前的独立研究记录（只读）' : '改进资料与模型比较'}</h2><p className="muted">{sourcesOnly ? '共同资料用于本轮23版分析包；保留来源、更新时间和缺口。打开这里不调用模型。' : archiveOnly ? '旧研究资料、原文和成绩保留，仅查看；新的分析使用统一入口。' : '日常分析继续使用现有14版包。改进包统一收集官方指标、日程和新闻，金十为可选来源；比较记录独立保存。'}</p></div>
      <button aria-expanded={open} onClick={() => setOpen(!open)}>{open ? '收起资料与比较' : '打开资料与比较'}</button></div>
    {open && <LabContent sourcesOnly={sourcesOnly} archiveOnly={archiveOnly} />}
  </section>;
}

function LabContent({ sourcesOnly, archiveOnly }: { sourcesOnly: boolean; archiveOnly: boolean }) {
  const cache = useQueryClient();
  const status = useQuery({ queryKey: ['analysis-lab'], queryFn: api.analysisLab });
  const [month, setMonth] = useState(() => {
    const parts = new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit' }).formatToParts(new Date());
    return `${parts.find(p => p.type === 'year')!.value}-${parts.find(p => p.type === 'month')!.value}`;
  });
  const [jobId, setJobId] = useState<string | null>(null);
  const [selected, setActive] = useState<Schema['AnalysisComparison'] | null>(null);
  const selectedQuery = useQuery({ queryKey: ['analysis-comparison', selected?.comparison_id], queryFn: () => api.comparison(selected!.comparison_id), enabled: !!selected,
    refetchInterval: q => ['queued', 'running', 'awaiting_review'].includes(q.state.data?.state ?? selected?.state ?? '') ? 2000 : false });
  const active = selectedQuery.data ?? selected;
  const [packageKind, setPackageKind] = useState<'current' | 'improved'>('improved');
  const [filter, setFilter] = useState('all');
  const [stars, setStars] = useState('0');
  const [page, setPage] = useState(0);
  const [allNews, setAllNews] = useState(false);
  const [newsPage, setNewsPage] = useState(0);
  const [copied, setCopied] = useState(false);
  const [copyError, setCopyError] = useState<Error | null>(null);
  const bundle = status.data?.bundle;
  const refresh = useMutation({ mutationFn: () => api.refreshResearch({ month }), onSuccess: job => setJobId(job.job_id) });
  const prepare = useMutation({ mutationFn: async (purpose: 'packet' | 'comparison') => {
    const workspace = await api.workspace();
    if (!workspace.market) throw new Error('尚无行情快照。请先到工作台刷新事实；休市期间不能把旧行情说成新行情。');
    const result = await api.prepareComparison({ market_snapshot_id: workspace.market.snapshot_id,
      bundle_id: packageKind === 'improved' ? bundle?.bundle_id ?? null : null, package_kind: packageKind, purpose });
    setActive(result);
    await cache.invalidateQueries({ queryKey: ['analysis-lab'] });
    return result;
  } });
  const show = useMutation({ mutationFn: api.comparison, onSuccess: setActive });
  const events = (bundle?.events ?? []).filter(e => (filter === 'all' || (filter === 'jin10' ? e.source_id.startsWith('jin10') : filter === 'official' ? ['bls-calendar', 'bea-calendar'].includes(e.source_id) : e.currency === 'USD')) &&
    (stars === '0' || (e.importance ?? 0) >= Number(stars)));
  const displayed = events.slice(page * 20, (page + 1) * 20);
  const news = (bundle?.news ?? []).filter(n => allNews || n.state === 'recent');
  return <div className="lab-content">
    <div className="notice"><strong>资料准备不收费，真实比较须单独授权</strong><p>准备改进包、准备四模型比较、查看或下载都不会调用模型，也不会改变日常默认模型。没有本组明确授权时不会发送真实请求；合成资料可独立模拟验收。</p></div>
    <ErrorNotice error={status.error ?? refresh.error ?? prepare.error ?? show.error ?? selectedQuery.error ?? copyError} />
    {!archiveOnly && <div className="lab-step"><h3>1 · 更新共同资料</h3><p>共同读取BLS／BEA官方日程、已公布指标和央行／公开新闻。金十按所选月份读取全部国家、全部评级，无法连接时仍可使用其他来源。官方无星级，不换算为金十评级。</p>
      <div className="actions"><Field label="资料月份"><input type="month" value={month} onChange={e => setMonth(e.target.value)} /></Field>
        <button disabled={refresh.isPending || !/^\d{4}-\d{2}$/.test(month)} onClick={() => refresh.mutate()}>更新公开资料（不调用模型）</button></div>
      {jobId && <JobPanel jobId={jobId} onComplete={() => { void cache.invalidateQueries({ queryKey: ['analysis-lab'] }); }} />}
      {bundle ? <>
        <p><Badge tone={bundle.state === 'available' ? 'good' : 'warn'}>{bundle.state === 'available' ? '资料已取得，仍需核对时点' : '资料有缺口'}</Badge> 最近更新 {time(bundle.captured_at)} · 日历 {bundle.events.length} 条 · 官方报告 {bundle.official_releases?.length ?? 0} 份 · 金十记录 {bundle.jin10_event_count} 条，已取得原始评级 {bundle.rated_event_count} 条</p>
        <div className="lab-quotes">{bundle.quotes.map(q => <article key={q.symbol} className="lab-quote"><h4>{quoteName[q.symbol]}</h4><strong>{q.value === null ? '—' : `${q.value}${q.unit === '%' ? '%' : ''}`}</strong><p>{quoteState[q.state]}</p><small>观测 {time(q.observed_at)}<br />取得 {time(q.captured_at)}</small><p className="muted">{q.publisher}</p></article>)}</div>
        <p className="muted">美元序列观测点 {bundle.quote_series_count ?? 0} 个；有观测点也不等于已认证连续实时，单点和休市旧值仅作标明时间的资料。</p>
        {!!bundle.reference_rates?.length && <details><summary>独立日度利率参考（非实时）</summary>{bundle.reference_rates.map(r => <p key={r.symbol}>{quoteName[r.symbol]} {r.value}% · 数据日期 {r.observation_date} · 财政部日度曲线，非盘中报价</p>)}</details>}
        <details><summary>来源状态与资料缺口</summary>{bundle.sources.map(s => <p key={s.source_id}><Badge tone={s.status === 'available' ? 'good' : 'warn'}>{s.status === 'available' ? '已取得' : '未取得'}</Badge> {sourceName(s.source_id)}：{s.message} · <a href={s.url} target="_blank" rel="noreferrer">来源</a></p>)}{bundle.limitations.map(s => <p key={s}>{s}</p>)}</details>
        <div className="lab-evidence"><h4>已公布的官方指标</h4><p className="muted">官方实际值与市场预期分开保存；月率、年率和季度年化口径不能混用，也不会回填旧预测。</p>
          {bundle.official_releases?.length ? bundle.official_releases.map(r => <article key={r.release_id} className="lab-release"><strong>{sourceName(r.source_id)}</strong><p>报告时期 {r.reference_period} · 公布 {time(r.published_at)} · 取得 {time(r.captured_at)}</p>
            {r.metrics.length ? <div className="lab-arms">{r.metrics.map(m => <span key={m.indicator}>{m.label}：{m.value}{m.unit === '%' ? '%' : m.unit}</span>)}</div> : <p>报告已取得，数值尚未明确提取，不猜测填入。</p>}
            <details><summary>报告原文摘要与来源</summary><p>{r.excerpt}</p><p>{r.limitation}</p><a href={r.url} target="_blank" rel="noreferrer">查看官方报告</a></details></article>) : <p>尚无已核验的官方报告数值；日历有日程不代表实际值已取得。</p>}
        </div>
        <div className="lab-evidence"><h4>共同检索的新闻与央行消息</h4><p>统一检索固定公开来源，四个模型使用同一份资料。只取得标题与摘要，覆盖不完整；尚未执行模型自行联网搜索。</p>
          <p className="muted">未来30／60／180分钟可能受哪些已知消息影响，由模型分析；未知的未来消息和实际公布值不会提前编造。</p>
          <label><input type="checkbox" checked={allNews} onChange={e => { setAllNews(e.target.checked); setNewsPage(0); }} />查看全部已取标题（含旧闻或时间未知）</label>
          <p className="muted">当前 {news.length} 条；默认只显示采集时72小时内的消息，新分析会按行情截止再次筛选。</p>
          {news.slice(newsPage * 10, (newsPage + 1) * 10).map(n => <article className="lab-news" key={n.news_id}><a href={n.url} target="_blank" rel="noreferrer">{n.title}</a><p>{n.summary}</p><small>{n.publisher} · 发布 {time(n.published_at)} · 取得 {time(n.captured_at)} · 标题／摘要，非全文核验{n.state !== 'recent' ? ' · 旧闻或时间待核对' : ''}</small></article>)}
          {!news.length && <p>没有取得符合当前时间条件的消息，不代表市场没有事件。</p>}
          {news.length > 10 && <div className="pagination"><button disabled={!newsPage} onClick={() => setNewsPage(newsPage - 1)}>上一组消息</button><span>第 {newsPage + 1} 页</span><button disabled={(newsPage + 1) * 10 >= news.length} onClick={() => setNewsPage(newsPage + 1)}>下一组消息</button></div>}
        </div>
        <div className="lab-filters"><Field label="查看指标范围"><select value={filter} onChange={e => { setFilter(e.target.value); setPage(0); }}><option value="all">全部来源与国家</option><option value="official">BLS／BEA官方日程</option><option value="jin10">仅金十</option><option value="usd">美元相关</option></select></Field>
          <Field label="金十评级筛选"><select value={stars} onChange={e => { setStars(e.target.value); setPage(0); }}><option value="0">全部评级，包括未知</option><option value="3">3星及以上</option><option value="4">4星及以上</option><option value="5">5星</option></select></Field></div>
        <p className="muted">筛选仅影响下表显示，不删除采集资料，也不直接决定金价方向。备用日历评级不冒充金十星级。</p>
        <div className="table-scroll"><table><thead><tr><th>时间 / 国家</th><th>指标或事件</th><th>原始评级</th><th>前值 / 预期</th><th>公布值 / 修正值</th></tr></thead><tbody>{displayed.map(e => <tr key={`${e.source_id}-${e.event_id}`}><td>{time(e.scheduled_at)}<br />{e.country}</td><td>{e.title}<small>{e.reference_period} {e.unit}</small></td><td>{e.importance === null ? e.importance_scale : `${e.importance}星（金十）`}</td><td>{e.previous ?? '—'} / {e.forecast ?? '—'}</td><td>{e.kind === 'event' ? '讲话 / 事件，无数值' : e.actual ?? (e.status === 'scheduled' ? '尚未到日程时点' : '实际值未取得')} / {e.revised ?? '—'}</td></tr>)}</tbody></table></div>
        {!events.length && <p className="muted">当前筛选没有可显示记录；不代表市场没有事件。</p>}
        <div className="pagination"><button disabled={page === 0} onClick={() => setPage(page - 1)}>上一页</button><span>{events.length} 条 · 第 {page + 1} 页</span><button disabled={(page + 1) * 20 >= events.length} onClick={() => setPage(page + 1)}>下一页</button></div>
      </> : <Empty title="尚未准备共同资料">点击更新后才读取外部公开来源，打开本页不会自动联网取数。</Empty>}
    </div>}
    {!sourcesOnly && !archiveOnly && <div className="lab-step"><h3>2 · 准备分析包或同题比较</h3><p>四个模型使用同一份资料和30／60／180分钟窗口。宏观资料必须先于行情截止取得；晚取到的资料不会回填旧行情。</p>
      <Field label="本次准备的分析包"><select value={packageKind} onChange={e => setPackageKind(e.target.value as typeof packageKind)}><option value="current">现有日常分析包（14版）</option><option value="improved">改进分析包（独立记录）</option></select></Field>
      <div className="actions"><button disabled={prepare.isPending || (packageKind === 'improved' && !bundle)} onClick={() => prepare.mutate('packet')}>准备所选分析包（不收费）</button>
        <button className="primary" disabled={prepare.isPending || (packageKind === 'improved' && !bundle)} onClick={() => prepare.mutate('comparison')}>准备四模型同题比较（不收费）</button>
        <button disabled title="当前没有新收费授权，服务端也会拒绝发送">开始收费比较（尚未授权）</button></div>
    </div>}
    {!sourcesOnly && <div className="lab-step"><h3>3 · 查看已保存资料</h3>{status.data?.comparisons.length ? <div className="lab-records">{status.data.comparisons.map(c => <button key={c.comparison_id} onClick={() => show.mutate(c.comparison_id)}>{c.package_kind === 'current' ? '日常14包' : '改进包'} · {c.purpose === 'comparison' ? '四模型同题' : '资料核对'} · {time(c.created_at)} · {comparisonState[c.state]}</button>)}</div> : <p className="muted">还没有新的比较资料。模型未发送时，不会出现成绩或准确率。</p>}
      {active && <div className="lab-selected"><p><strong>{active.package_kind === 'current' ? '现有日常分析包' : '改进分析包'}</strong> · 行情截止 {time(active.market_cutoff)} · 实际模型调用 {active.model_calls_sent} 次</p><p>{active.message}</p>
        {active.data_kind === 'SYNTHETIC' && <div className="notice">合成资料，仅验证功能，不是真实预测。</div>}
        {!!active.window_risks?.length && <div className="lab-window-risks"><strong>本次预测窗口的已知日程</strong>{active.window_risks.map(w => <p key={w.horizon_minutes}>{w.horizon_minutes}分钟内：已登记 {w.upcoming_event_ids.length} 项 · 覆盖不完整；未来实际值未知</p>)}</div>}
        {active.arms.length > 0 && !active.execution && <div className="lab-arms">{active.arms.map(a => <span key={a.arm}>{a.model_id} · high · 未发送</span>)}</div>}
        <ComparisonRunPanel key={active.comparison_id} record={active} readOnly={archiveOnly} />
        {archiveOnly && active.execution && <div className="notice">原执行与评分记录保留：{active.execution.scores.length}项分项；本页不重新发送模型或改写原记录。</div>}
        <div className="actions"><button onClick={() => { setCopyError(null); void navigator.clipboard.writeText(active.packet_text).then(() => setCopied(true)).catch(() => setCopyError(new Error('复制失败，可下载资料查看。'))); }}>{copied ? '已复制核对资料' : '复制用于核对'}</button><button onClick={() => downloadText(`comparison-${active.comparison_id}.txt`, active.packet_text)}>下载核对资料</button></div>
        <details><summary>查看完整分析包</summary><pre className="packet">{active.packet_text}</pre></details></div>}
      {status.data && <details><summary>既往封存测试说明</summary><p>既往批次共{status.data.historical.attempts}次：{status.data.historical.qualified_answers}份回答通过格式及业务规则、{status.data.historical.failed}次失败、{status.data.historical.unknown}次未知。合格不等于预测命中；不同条件的旧组不混算胜率，目前未确定最准确模型。</p></details>}
    </div>}
  </div>;
}
