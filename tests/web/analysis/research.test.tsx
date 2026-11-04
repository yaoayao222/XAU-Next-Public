import React from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { AnalysisLabPanel } from '../../../apps/web/src/features/analysis/AnalysisLabPanel';

const at = '2026-10-12T02:00:00Z';
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
describe('多来源共同资料', () => {
  it('金十失败仍显示官方实际值和共同新闻；日度参考不冒充实时', async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    sessionStorage.setItem('xau-next-session-token', 'synthetic-research');
    const release = { release_id: 'cpi', source_id: 'bls-cpi', title: 'CPI', reference_period: '2026-09', published_at: at, captured_at: at,
      url: 'https://www.bls.gov/news.release/cpi.nr0.htm', state: 'available', excerpt: 'SYNTHETIC source body', limitation: 'No forecast inferred',
      metrics: [{ indicator: 'cpi_mom', label: 'CPI月率', value: '0.0', unit: '%', basis: 'month_change_seasonally_adjusted' }] };
    const news = { news_id: 'recent', title: 'SYNTHETIC rate announcement', summary: 'Known before cutoff', publisher: 'BBC', source_id: 'bbc-world',
      url: 'https://www.bbc.com/news/articles/synthetic', published_at: at, captured_at: at, evidence_level: 'feed_summary_only', state: 'recent', relevance_basis: 'keyword' };
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ daily_package: 'manual-analysis/14', improved_package: 'manual-analysis/23',
      paid_execution_enabled: false, authorized_calls_remaining: 0, historical: { attempts: 58, qualified_answers: 34, failed: 22, unknown: 2 }, comparisons: [],
      bundle: { bundle_id: 'b1', captured_at: at, data_kind: 'SYNTHETIC', input_revision: 'macro-research/2', state: 'partial', events: [], quotes: [],
        sources: [{ source_id: 'jin10-data', status: 'unavailable', url: 'https://rili.jin10.com', message: '暂未取得' },
          { source_id: 'bls-cpi', status: 'available', url: release.url, message: '官方已取得' }],
        official_releases: [release], news: [news, { ...news, news_id: 'old', title: 'SYNTHETIC old headline', state: 'old' }],
        reference_rates: [{ symbol: 'US10Y', value: '4.0', observation_date: '2026-10-09' }],
        quote_series_count: 0, jin10_event_count: 0, rated_event_count: 0, limitations: ['覆盖不完整'], search: null }
    }), { headers: { 'Content-Type': 'application/json' } })));
    render(<QueryClientProvider client={client}><AnalysisLabPanel /></QueryClientProvider>);
    fireEvent.click(screen.getByRole('button', { name: '打开资料与比较' }));
    await screen.findByText('CPI月率：0.0%');
    expect(screen.getByText('SYNTHETIC rate announcement')).toBeTruthy();
    expect(screen.queryByText('SYNTHETIC old headline')).toBeNull();
    fireEvent.click(screen.getByRole('checkbox', { name: '查看全部已取标题（含旧闻或时间未知）' }));
    expect(screen.getByText('SYNTHETIC old headline')).toBeTruthy();
    expect(screen.getByText('独立日度利率参考（非实时）')).toBeTruthy();
    expect(screen.getByRole('button', { name: '准备四模型同题比较（不收费）' }).hasAttribute('disabled')).toBe(false);
    expect(screen.getByRole('button', { name: '开始收费比较（尚未授权）' }).hasAttribute('disabled')).toBe(true);
    client.clear();
  });
});
