import { createApiClient, type components } from '@xau/api-client';

export type Schema = components['schemas'];
let apiBase = 'http://127.0.0.1:8765';
const tokenKey = 'xau-next-session-token';

export function isLocalApi(value: string): boolean {
  try { const url = new URL(value); return url.protocol === 'http:' && ['127.0.0.1', 'localhost', '[::1]'].includes(url.hostname) && !url.username && !url.password; } catch { return false; }
}

export async function configureApi(): Promise<void> {
  const url = new URL(window.location.href);
  const previousBase = sessionStorage.getItem('xau-next-api-base');
  if (previousBase && isLocalApi(previousBase)) apiBase = previousBase;
  const token = new URLSearchParams(url.hash.slice(1)).get('token');
  if (token) { sessionStorage.setItem(tokenKey, token); url.hash = ''; history.replaceState(null, '', url); }
  try {
    const response = await fetch('/runtime-config.json', { cache: 'no-store' });
    if (response.ok) {
      const config = await response.json() as { api_base_url?: string; apiBaseUrl?: string };
      const base = config.api_base_url ?? config.apiBaseUrl;
      if (base && isLocalApi(base)) apiBase = base.replace(/\/$/, '');
    }
  } catch { /* Development can use the documented local API default. */ }
  const override = url.searchParams.get('api');
  if (override && isLocalApi(override)) apiBase = override.replace(/\/$/, '');
  sessionStorage.setItem('xau-next-api-base', apiBase);
}

export const getApiBase = () => apiBase;
export const hasSessionToken = () => Boolean(sessionStorage.getItem(tokenKey));
export const sdk = () => createApiClient(apiBase, sessionStorage.getItem(tokenKey) ?? '');
export const commandKey = () => crypto.randomUUID();

export class ApiProblem extends Error {
  constructor(public problem: Schema['Problem']) { super(problem.detail); this.name = 'ApiProblem'; }
}

function unwrap<T>(result: { data?: T; error?: unknown; response: Response }): T {
  if (!result.response.ok || result.error) {
    const p = result.error as Partial<Schema['Problem']> | undefined;
    throw new ApiProblem({ type: p?.type ?? 'about:blank', title: p?.title ?? '请求未完成', status: result.response.status, code: p?.code ?? 'HTTP_ERROR', detail: p?.detail ?? `本地服务返回 ${result.response.status}，请重试或检查启动状态。`, trace_id: p?.trace_id ?? 'unknown', field_errors: p?.field_errors ?? [], retryable: p?.retryable ?? false });
  }
  if (result.data === undefined) throw new Error('本地服务未返回预期数据。');
  return result.data;
}
const writeHeader = (key?: string) => ({ 'Idempotency-Key': key ?? commandKey() });

export const api = {
  dailyGroups: async (cursor?: string) => unwrap(await sdk().GET('/v1/analysis/daily/groups', { params: { query: { cursor, limit: 20 } } })),
  dailyGroup: async (group_id: string) => unwrap(await sdk().GET('/v1/analysis/daily/groups/{group_id}', { params: { path: { group_id } } })),
  prepareDaily: async (body: Schema['DailyPrepare'], key?: string) => unwrap(await sdk().POST('/v1/analysis/daily/groups', { body, params: { header: writeHeader(key) } })),
  runDaily: async (group_id: string, body: Schema['DailyRun'], key?: string) => unwrap(await sdk().POST('/v1/analysis/daily/groups/{group_id}/run', { body, params: { path: { group_id }, header: writeHeader(key) } })),
  dailyPerformance: async (query: { model?: string; template?: string; minutes?: number; from_date?: string; to_date?: string; paired_only?: boolean; data_kind?: 'REAL' | 'SYNTHETIC'; provider?: string; method?: string; include_history?: boolean; history_cursor?: string; history_limit?: number; history_view?: 'all' | 'issues'; history_status?: Schema['DailyHistoryItem']['state']; history_row?: string }) => unwrap(await sdk().GET('/v1/analysis/daily/performance', { params: { query } })),
  analysisLab: async () => unwrap(await sdk().GET('/v1/analysis/lab')),
  refreshResearch: async (body: Schema['ResearchRefresh']) => unwrap(await sdk().POST('/v1/analysis/lab/sources', { body, params: { header: writeHeader() } })),
  prepareComparison: async (body: Schema['ComparisonPrepare']) => unwrap(await sdk().POST('/v1/analysis/lab/comparisons', { body, params: { header: writeHeader() } })),
  comparison: async (comparison_id: string) => unwrap(await sdk().GET('/v1/analysis/lab/comparisons/{comparison_id}', { params: { path: { comparison_id } } })),
  runComparison: async (comparison_id: string, body: Schema['ComparisonRunCreate'], key?: string) => unwrap(await sdk().POST('/v1/analysis/lab/comparisons/{comparison_id}/run', { body, params: { path: { comparison_id }, header: writeHeader(key) } })),
  scoreComparison: async (comparison_id: string, body: Schema['ComparisonScoreCreate']) => unwrap(await sdk().POST('/v1/analysis/lab/comparisons/{comparison_id}/score', { body, params: { path: { comparison_id }, header: writeHeader() } })),
  comparisonAnswer: async (comparison_id: string, arm: 'A' | 'B' | 'C' | 'D') => unwrap(await sdk().GET('/v1/analysis/lab/comparisons/{comparison_id}/answers/{arm}', { params: { path: { comparison_id, arm } } })),
  modelSettings: async () => unwrap(await sdk().GET('/v1/model/settings')),
  modelProfiles: async () => unwrap(await sdk().GET('/v1/model/profiles')),
  createModelProfile: async (body: Schema['ModelProfileCreate']) => unwrap(await sdk().POST('/v1/model/profiles', { body, params: { header: writeHeader() } })),
  activateModelProfile: async (profile_id: string, expected_revision: number) => unwrap(await sdk().POST('/v1/model/profiles/{profile_id}/activate', { body: { expected_revision }, params: { path: { profile_id }, header: writeHeader() } })),
  saveModelSettings: async (body: Schema['ModelSettingsUpdate']) => unwrap(await sdk().PUT('/v1/model/settings', { body, params: { header: writeHeader() } })),
  setModelCredential: async (body: Schema['ModelCredential'], key: string) => unwrap(await sdk().POST('/v1/model/credential', { body, params: { header: writeHeader(key) } })),
  checkModel: async (kind: 'models' | 'usage') => unwrap(await sdk().POST('/v1/model/checks', { body: { kind }, params: { header: writeHeader() } })),
  probeModel: async (key: string) => unwrap(await sdk().POST('/v1/model/probe', { body: { retry_of: null, acknowledge_possible_charge: true }, params: { header: writeHeader(key) } })),
  startModelAnalysis: async (request_id: string, body: Schema['ModelCallCreate'] = { retry_of: null, acknowledge_possible_charge: false }, key?: string) => unwrap(await sdk().POST('/v1/analysis/requests/{request_id}/model-call', { body, params: { path: { request_id }, header: writeHeader(key) } })),
  requestModelCall: async (request_id: string) => unwrap(await sdk().GET('/v1/analysis/requests/{request_id}/model-call', { params: { path: { request_id } } })),
  modelCall: async (call_id: string) => unwrap(await sdk().GET('/v1/model/calls/{call_id}', { params: { path: { call_id } } })),
  modelRaw: async (call_id: string) => unwrap(await sdk().GET('/v1/model/calls/{call_id}/raw', { params: { path: { call_id } } })),
  health: async () => unwrap(await sdk().GET('/v1/health')),
  dailyAnalysisSource: async () => unwrap(await sdk().GET('/v1/analysis/daily/source')),
  workspace: async () => unwrap(await sdk().GET('/v1/workspace')),
  settings: async () => unwrap(await sdk().GET('/v1/settings')),
  saveSettings: async (body: Schema['SettingsUpdate']) => unwrap(await sdk().PUT('/v1/settings', { body, params: { header: writeHeader() } })),
  refreshMarket: async (body: Schema['MarketRefresh'] = { include_history: false, history_from: null, history_to: null }) => unwrap(await sdk().POST('/v1/market/refresh', { body, params: { header: writeHeader() } })),
  createRequest: async (body: Schema['RequestCreate'], key?: string) => unwrap(await sdk().POST('/v1/analysis/requests', { body, params: { header: writeHeader(key) } })),
  requests: async (cursor?: string) => unwrap(await sdk().GET('/v1/analysis/requests', { params: { query: { state: 'awaiting_answer', limit: 50, cursor } } })),
  request: async (request_id: string) => unwrap(await sdk().GET('/v1/analysis/requests/{request_id}', { params: { path: { request_id } } })),
  importAnswer: async (body: Schema['ImportCreate'], key: string) => unwrap(await sdk().POST('/v1/analysis/imports', { body, params: { header: writeHeader(key) } })),
  job: async (job_id: string) => unwrap(await sdk().GET('/v1/jobs/{job_id}', { params: { path: { job_id } } })),
  cancelJob: async (job_id: string) => unwrap(await sdk().POST('/v1/jobs/{job_id}/cancel', { params: { path: { job_id }, header: writeHeader() } })),
  forecasts: async (cursor?: string) => unwrap(await sdk().GET('/v1/forecasts', { params: { query: { cursor, limit: 50 } } })),
  forecast: async (forecast_id: string) => unwrap(await sdk().GET('/v1/forecasts/{forecast_id}', { params: { path: { forecast_id } } })),
  versions: async (forecast_id: string, cursor?: string) => unwrap(await sdk().GET('/v1/forecasts/{forecast_id}/versions', { params: { path: { forecast_id }, query: { cursor, limit: 50 } } })),
  version: async (forecast_id: string, version_id: string) => unwrap(await sdk().GET('/v1/forecasts/{forecast_id}/versions/{version_id}', { params: { path: { forecast_id, version_id } } })),
  evaluate: async (plan_id: string, body: Schema['EvaluationCreate']) => unwrap(await sdk().POST('/v1/plans/{plan_id}/evaluations', { body, params: { path: { plan_id }, header: writeHeader() } })),
  evaluations: async (plan_id: string, cursor?: string) => unwrap(await sdk().GET('/v1/plans/{plan_id}/evaluations', { params: { path: { plan_id }, query: { cursor, limit: 50 } } })),
  observe: async (plan_id: string, body: Schema['PlanObservationCreate']) => unwrap(await sdk().POST('/v1/plans/{plan_id}/observations', { body, params: { path: { plan_id }, header: writeHeader() } })),
  portfolio: async () => unwrap(await sdk().GET('/v1/portfolio')),
  associate: async (body: Schema['AssociationCreate']) => unwrap(await sdk().POST('/v1/portfolio/associations', { body, params: { header: writeHeader() } })),
  decide: async (body: Schema['DecisionCreate']) => unwrap(await sdk().POST('/v1/portfolio/decisions', { body, params: { header: writeHeader() } })),
  reviews: async (kind: Schema['Review']['target_kind'], cursor?: string, target_id?: string) => unwrap(await sdk().GET('/v1/reviews', { params: { query: { cursor, limit: 50, target_kind: kind, target_id, revisions: 'all' } } })),
  review: async (review_id: string, revision: number) => unwrap(await sdk().GET('/v1/reviews/{review_id}', { params: { path: { review_id }, query: { revision } } })),
  refreshReviews: async (body: Schema['ReviewRefresh']) => unwrap(await sdk().POST('/v1/reviews/refresh', { body, params: { header: writeHeader() } })),
  trades: async (cursor?: string) => unwrap(await sdk().GET('/v1/portfolio/trades', { params: { query: { cursor, limit: 50 } } })),
  trade: async (trade_id: string) => unwrap(await sdk().GET('/v1/portfolio/trades/{trade_id}', { params: { path: { trade_id } } })),
};
