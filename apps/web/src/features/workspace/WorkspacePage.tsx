import { useState } from 'react';
import { Link } from 'react-router-dom';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { api, type Schema } from '../../api/client';
import { useSettings, useWorkspace } from '../../api/queries';
import { Badge, Empty, ErrorNotice, JobPanel, Metric, PageHeading, SourceLine } from '../../shared/ui';
import { money, time } from '../../shared/format';
import { PlanCard } from '../plans/PlanCard';
import { PortfolioPanel } from '../portfolio/PortfolioPanel';
import { ActionSummary, GuidancePanel } from './GuidancePanel';
import { DailyResults } from '../analysis/DailyResults';

function latestRefreshJob(
  latest: Schema['Job'] | null,
  active: Schema['Job'][],
  manual: Schema['Job'] | undefined,
): Schema['Job'] | null {
  // Prefer the authoritative snapshot on ties; an accepted manual response may
  // briefly lead the next snapshot, but must never replace a newer job revision.
  return [latest, ...active, manual].reduce<Schema['Job'] | null>((current, candidate) => {
    if (!candidate || candidate.kind !== 'market_refresh') return current;
    if (!current) return candidate;
    if (candidate.job_id === current.job_id) return candidate.revision > current.revision ? candidate : current;
    return Date.parse(candidate.updated_at) > Date.parse(current.updated_at) ? candidate : current;
  }, null);
}

function FactRefreshStatus({ config, job, sourceKind, hasKnownFacts, zone }: {
  config: NonNullable<Schema['Workspace']['fact_refresh']>;
  job: Schema['Job'] | null;
  sourceKind: Schema['MarketSnapshot']['source_kind'] | undefined;
  hasKnownFacts: boolean;
  zone?: string;
}) {
  const failed = job?.state === 'failed';
  const state = !job ? '等待首次读取'
    : job.state === 'queued' ? '等待读取'
    : job.state === 'running' ? '正在读取'
    : job.state === 'cancelling' ? '正在停止本次读取'
    : failed ? '读取失败'
    : job.state === 'cancelled' ? '本次读取已取消'
    : '上次读取已完成';
  const source = sourceKind === 'mt5' ? 'MT5 只读采集'
    : sourceKind === 'synthetic' ? '合成样例采集' : '报价、账户与订单事实';
  return <section
    className={`fact-refresh ${failed ? 'is-failed' : ''}`}
    aria-label="事实自动读取状态"
  >
    <div className="fact-refresh-heading">
      <div>
        <strong>{config.enabled ? '自动刷新事实已开启' : '自动刷新事实未开启'}</strong>
        <span className="fact-refresh-source">{source}</span>
      </div>
      {(config.enabled || job) && <Badge tone={failed ? 'warn' : job?.state === 'succeeded' ? 'good' : 'neutral'}>{state}</Badge>}
    </div>
    <p className="fact-refresh-schedule">
      {config.enabled
        ? `成功后每 ${config.interval_seconds} 秒读取一次；失败后 ${config.retry_seconds} 秒自动重试。`
        : '仍可点击“刷新事实”手动读取。'}
      {job && <span>最近任务更新 {time(job.updated_at, zone)}</span>}
    </p>
    {failed && <div className="fact-refresh-error" role="alert">
      <p><strong>读取失败原因：</strong>{job.error?.detail ?? job.message}</p>
      <p>{hasKnownFacts
        ? '已保留上次读取的事实；行情与持仓仍以各自事实时间和读取状态为准。'
        : '尚未取得行情或持仓事实，缺失记录不代表空仓。'}
        {config.enabled ? `失败后 ${config.retry_seconds} 秒会再次尝试，也可手动刷新。` : '可手动刷新重试。'}
      </p>
      {job.error && <details>
        <summary>错误详情</summary>
        <p>{job.error.code} · 追踪 {job.error.trace_id}</p>
        {job.error.field_errors.length > 0 && <ul>{job.error.field_errors.map((field, index) =>
          <li key={index}><code>{field.path}</code>：{field.message}</li>)}</ul>}
      </details>}
    </div>}
  </section>;
}

export function WorkspacePage() {
  const workspace = useWorkspace();
  const settings = useSettings();
  const cache = useQueryClient();
  const [jobId, setJobId] = useState<string | null>(null);
  const refresh = useMutation({
    mutationFn: () => api.refreshMarket(),
    onSuccess: job => {
      setJobId(job.job_id);
      void cache.invalidateQueries({ queryKey: ['workspace'] });
    },
  });
  const data = workspace.data;
  const market = data?.market;
  const parallelPlans = data?.daily_group?.arms.flatMap(a => a.plans) ?? [];
  const planSources = Object.fromEntries(data?.daily_group?.arms.flatMap(a => a.plans.map(p => [p.plan_id, `${a.model_id} / ${data.daily_group!.template_version}`])) ?? []);
  const zone = settings.data?.values.display_timezone;
  const factRefresh = data?.fact_refresh;
  const currentRefreshJob = factRefresh
    ? latestRefreshJob(factRefresh.latest_job, data?.active_jobs ?? [], refresh.data)
    : null;
  const otherJobs = data?.active_jobs.filter(job =>
    job.job_id !== jobId && (!factRefresh || job.kind !== 'market_refresh')) ?? [];

  return <>
    <PageHeading eyebrow="DAILY WORKSPACE" title="工作台" actions={<>
      <button title="只读取当前事实，不会下单" onClick={() => refresh.mutate()} disabled={refresh.isPending}>
        {refresh.isPending ? '正在受理…' : '刷新事实'}
      </button>
      <Link className="button primary" title="创建分析请求，不会自动执行交易" to="/analysis?mode=manual">新分析</Link>
      <Link className="button" title="进入 API 或手工分析流程，不会自动执行交易" to="/analysis?mode=api">API 分析</Link>
    </>}>查看系统当前行动建议和入场时机，在 MT5 人工完成交易。<span className="safety-inline">只读 · 人工执行 · 不自动下单</span></PageHeading>
    <ErrorNotice error={workspace.error ?? refresh.error} />
    {jobId && !factRefresh && <JobPanel jobId={jobId} />}
    <section className="quote-panel">
      <div className="row-between">
        <div><p className="eyebrow">MARKET SNAPSHOT</p><h2>{market?.symbol ?? 'XAUUSD'} <small>当前事实</small></h2></div>
        <span className="muted">{market ? `快照截止 ${time(market.market_cutoff, zone)}` : '尚未读取'}</span>
      </div>
      {factRefresh && <FactRefreshStatus
        config={factRefresh}
        job={currentRefreshJob}
        sourceKind={market?.source_kind}
        hasKnownFacts={Boolean(market || data?.portfolio)}
        zone={zone}
      />}
      {market ? <>
        <div className="quote-grid">
          <Metric label="BID · 卖出侧" value={market.quote?.bid ?? '未知'} />
          <Metric label="ASK · 买入侧" value={market.quote?.ask ?? '未知'} />
          <Metric label="合约大小" value={market.contract?.contract_size ?? '未知'} />
          <Metric label="最小手数 / 步进" value={market.contract ? `${market.contract.volume_min} / ${market.contract.volume_step}` : '未知'} />
        </div>
        <div className="freshness-strip" aria-label="行情资料新鲜度">
          <Badge tone={market.quote_status.state === 'available' ? 'good' : 'warn'}>{market.quote_status.state === 'available' ? '资料可用' : '资料需要核对'}</Badge>
          <span><strong>最后读取</strong> {time(market.quote_status.captured_at, zone)}</span>
          <span><strong>资料截止</strong> {time(market.market_cutoff, zone)}</span>
          {market.quote_status.reasons.length > 0 && <span>{market.quote_status.reasons.join('；')}</span>}
        </div>
        <details>
          <summary>合约与 K 线来源</summary>
          <SourceLine status={market.contract_status} zone={zone} />
          {market.series.length ? market.series.map(series => <div key={series.timeframe}>
            <strong>{series.timeframe}</strong><pre>{JSON.stringify(series, null, 2)}</pre>
          </div>) : <p>K 线尚无可展示记录。</p>}
          <small>账户来源标识：{market.account_scope_id} · 数据来源：{market.source_kind}</small>
        </details>
      </> : <Empty title={workspace.isPending ? '正在读取工作台…' : '还没有行情快照'}>
        {factRefresh?.enabled ? '自动读取会取得报价、合约与账户，也可点击“刷新事实”立即尝试。' : '点击“刷新事实”，从配置的数据源读取报价、合约与账户。'}
      </Empty>}
    </section>
    {data && !data.daily_group && <ActionSummary workspace={data} offline={workspace.isError} zone={zone} onRefresh={() => refresh.mutate()} refreshPending={refresh.isPending} />}
    {data?.daily_group && <DailyResults group={data.daily_group} workspace={data} />}
    {data && <GuidancePanel workspace={data} offline={workspace.isError} zone={zone} holdingOnly={!!data.daily_group} />}
    <div className="workspace-columns">
      {!data?.daily_group && <section className="main-column">
        <div className="section-heading"><h2>当前分析</h2>{data?.current_forecast &&
          <Link to={`/analysis?forecast=${encodeURIComponent(data.current_forecast.forecast_id)}`}>查看版本与更新 ↗</Link>}
        </div>
        {data?.current_forecast ? <div className="analysis-summary">
          <p>{data.current_forecast.summary}</p>
          <small>实际可用 {time(data.current_forecast.available_at, zone)} · 原窗口截至 {time(data.current_forecast.window_end, zone)}</small>
        </div> : <Empty title="还没有已导入的分析">新建分析请求，复制分析包并导入回答后，方案会在这里出现。</Empty>}
        <div className="section-heading"><h2>候选方案</h2><span className="muted">方向与入场方式 · 详细依据可展开</span></div>
        {data?.plans.length ? data.plans.map(plan => <PlanCard
          key={plan.plan_id} plan={plan} evaluation={data.evaluations.find(item => item.plan_id === plan.plan_id)}
        />) : <Empty title={data?.current_forecast ? '本次回答未提供候选方案' : '暂无候选方案'}>
          {data?.current_forecast ? '回答已导入。请查看上方超短线、日内各自的候选说明；系统不会从预测区间自动生成交易方案。' : '导入回答后，有依据的候选会在这里显示，可以等待未来触发。'}
        </Empty>}
      </section>}
      <aside className="side-column">
        <section className="panel">
          <p className="eyebrow">NEXT STEP</p><h2>下一步</h2>
          {data?.next_actions.length ? <ol className="next-actions">{data.next_actions.map((action, index) =>
            <li key={index}>{action}</li>)}</ol> : <p className="muted">先刷新事实，再开始一轮分析。</p>}
          {data?.pending_requests.length ? <Link className="button" to="/analysis">恢复 {data.pending_requests.length} 个待回答请求</Link> : null}
        </section>
        <section className="panel">
          <h2>资金与配置</h2>
          <Metric label="账户权益" value={data?.portfolio?.account ? money(data.portfolio.account.equity, data.portfolio.account.currency) : '未知'} />
          <Metric label="单笔损失上限" value={settings.data?.values.per_trade_loss_cap ? money(settings.data.values.per_trade_loss_cap.amount, settings.data.values.per_trade_loss_cap.currency) : '尚未配置'} />
          <Link to="/settings">调整手数与费用 →</Link>
        </section>
        {otherJobs.map(job => <JobPanel key={job.job_id} jobId={job.job_id} />)}
      </aside>
    </div>
    <div className="section-heading"><h2>持仓与挂单</h2><span className="muted">事实读取与人工意图分别保存</span></div>
    <PortfolioPanel portfolio={data?.portfolio ?? null} plans={data?.daily_group ? parallelPlans : data?.plans ?? []} planSources={planSources} />
  </>;
}
