import React from 'react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { DailyAnalysisPanel } from '../../../apps/web/src/features/analysis/DailyAnalysisPanel';
import { DailyResults } from '../../../apps/web/src/features/analysis/DailyResults';
import { type Schema } from '../../../apps/web/src/api/client';
import { workspace, version } from '../fixtures';

let cache: QueryClient;
let groups: Schema['DailyGroup'][];
let writes: { path: string; body: Record<string, any>; key: string | null }[];
let reads: string[];
let synthetic: boolean;
let prepareFailure: boolean;
let runFailure: boolean;
let profileItems: Schema['ModelProfileSummary'][];
function group(models: ('A'|'B'|'C'|'D')[]): Schema['DailyGroup'] {
  const ids = { A: 'gpt-6.1-sol', B: 'gpt-6-astra', C: 'deepseek-flash', D: 'deepseek-v4-pro' };
  return { group_id: 'round-1', kind: 'new', base_group_id: null, created_at: workspace.as_of,
    market_cutoff: workspace.market!.market_cutoff, window_end: '2026-10-12T05:00:00Z', horizon_minutes: [30,60,180],
    source_scope: 'SYNTHETIC-source', data_kind: 'SYNTHETIC', package_kind: 'current', template_version: 'manual-analysis/14',
    bundle_id: null, context_sha256: 'a'.repeat(64), same_question: true, models, state: 'prepared', run_registered: false,
    arms: models.map(key => ({ model_key: key, model_id: ids[key], request_id: `q-${key}`, forecast_id: `f-${key}`,
      packet_sha256: 'b'.repeat(64), state: 'prepared', version_id: null, model_call: null, available_at: null, summary: null,
      horizons: [], plans: [], guidance: {} })) };
}
beforeEach(() => {
  cache = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  groups=[]; writes=[]; reads=[]; synthetic=true;prepareFailure=false;runFailure=false;
  profileItems=[{profile_id:'cpa',name:'SYNTHETIC CPA',base_url:'http://127.0.0.1:8317/v1',model_id:'gpt-6.1-sol'}];
  sessionStorage.setItem('xau-next-session-token','SYNTHETIC-daily-token');
  vi.stubGlobal('fetch', vi.fn(async (input: Request) => {
    const path = new URL(input.url).pathname;
    const reply = (data: unknown, status=200) => new Response(JSON.stringify(data), {status,headers:{'Content-Type':'application/json'}});
    if (input.method === 'GET') reads.push(path);
    else writes.push({path,body:await input.json(),key:input.headers.get('Idempotency-Key')});
    if(path==='/v1/analysis/daily/source') return reply({market_snapshot_id:'fresh-source-id',market_cutoff:workspace.market!.market_cutoff,as_of:workspace.as_of});
    if(path==='/v1/workspace') return reply({...workspace,daily_group:null,market:{...workspace.market,source_kind:synthetic?'synthetic':'mt5'}});
    if(path==='/v1/model/profiles') return reply({settings_revision:1,active_profile_id:'cpa',items:profileItems});
    if(path==='/v1/analysis/lab') return reply({bundle:{bundle_id:'synthetic-bundle',captured_at:workspace.as_of,month:'2026-10',scope:'SYNTHETIC',state:'partial',sources:[],events:[],quotes:[],limitations:[],jin10_event_count:0,rated_event_count:0},comparisons:[]});
    if(path==='/v1/analysis/daily/groups' && input.method==='GET') return reply({items:groups,next_cursor:null});
    if(path==='/v1/analysis/daily/groups' && input.method==='POST') { if(prepareFailure)return reply({code:'PACKET_TOO_LARGE',detail:'资料包尚未准备成功：整理后为260,001字符，超过250,000字符上限。本轮模型未发送。'},422);const body=writes.at(-1)!.body;groups=[{...group(body.models),package_kind:body.package_kind}];return reply(groups[0],201); }
    if(path==='/v1/analysis/daily/groups/round-1/run') { if(runFailure)return reply({code:'HTTP_ERROR',detail:'本轮启动未确认'},503);groups=[{...groups[0],state:'finished',run_registered:true,arms:groups[0].arms.map(a=>({...a,state:'cancelled'}))}];return reply(groups[0],202); }
    if(path==='/v1/analysis/daily/groups/round-1') return reply(groups[0]);
    if(path.includes('/versions/')) return reply(version);
    throw new Error(`Unexpected ${input.method} ${path}`);
  }));
});
afterEach(()=>{cleanup();cache.clear();vi.unstubAllGlobals();});
function show(){render(<QueryClientProvider client={cache}><MemoryRouter><DailyAnalysisPanel/></MemoryRouter></QueryClientProvider>);}

it('超限后停止冻结提示、显示具体容量，重置清除旧错误且不发送模型',async()=>{
  prepareFailure=true;show();
  await waitFor(()=>expect(screen.getByRole('button',{name:'仅准备分析包（不发送）'}).hasAttribute('disabled')).toBe(false));
  fireEvent.click(screen.getByRole('button',{name:'仅准备分析包（不发送）'}));
  await screen.findByText(/整理后为260,001字符/);
  expect(screen.queryByText('正在冻结行情与本轮资料…')).toBeNull();
  expect(screen.getByText('资料准备未完成，本轮未启动模型。请查看下方原因。')).toBeTruthy();
  expect(writes).toHaveLength(1);expect(writes[0].path).not.toContain('/run');
  fireEvent.click(screen.getByRole('button',{name:'重新准备新一轮'}));
  expect(screen.queryByRole('alert')).toBeNull();expect(screen.queryByText(/资料准备未完成/)).toBeNull();
});

it('启动响应未确认时不谎称模型未发送，也不自动补发',async()=>{
  runFailure=true;show();await screen.findByRole('button',{name:'开始模拟分析（不收费）'});
  await waitFor(()=>expect(screen.getByRole('button',{name:'开始模拟分析（不收费）'}).hasAttribute('disabled')).toBe(false));
  fireEvent.click(screen.getByRole('button',{name:'开始模拟分析（不收费）'}));
  await screen.findByText('本轮启动未确认，请查看已保存轮次的任务状态；不会自动重发。');
  expect(screen.queryByText(/资料准备未完成，本轮未启动模型/)).toBeNull();
  expect(writes).toHaveLength(2);
});

it.each([['Sol'],['Sol','Astra'],['Sol','Astra','DeepSeek Flash','DeepSeek Pro']])('只登记用户选择的模型 %j，打开页面不调用',async(...names)=>{
  show(); await screen.findByRole('button',{name:'开始模拟分析（不收费）'});
  await waitFor(()=>expect(screen.getByRole('button',{name:'仅准备分析包（不发送）'}).hasAttribute('disabled')).toBe(false));
  expect(writes).toHaveLength(0);
  for(const name of names.slice(1)) fireEvent.click(screen.getByRole('checkbox',{name:new RegExp(name+'high')}));
  fireEvent.click(screen.getByRole('button',{name:'开始模拟分析（不收费）'}));
  await waitFor(()=>expect(writes).toHaveLength(2));
  expect(writes[0].body.models).toHaveLength(names.length);
  expect(writes[1].body).toMatchObject({max_calls:names.length,mode:'simulation'});
  expect(writes.every(w=>!w.path.includes('/model-call'))).toBe(true);
  expect(await screen.findAllByRole('article')).toHaveLength(names.length);
});

it('仅准备后再启动沿用同轮，不重新准备；真实发送需要主动勾选',async()=>{
  synthetic=false;show();
  await waitFor(()=>expect(screen.getByRole('button',{name:'仅准备分析包（不发送）'}).hasAttribute('disabled')).toBe(false));
  expect(screen.getByRole('button',{name:'开始所选1个模型分析'}).hasAttribute('disabled')).toBe(true);
  fireEvent.click(screen.getByRole('button',{name:'仅准备分析包（不发送）'}));
  await screen.findByText(/资料已保存，尚未发送/);
  expect(writes).toHaveLength(1);
  fireEvent.click(screen.getByRole('checkbox',{name:/我明确同意本轮最多1次/}));
  fireEvent.click(screen.getByRole('button',{name:'开始所选1个模型分析'}));
  await waitFor(()=>expect(writes).toHaveLength(2));
  expect(writes.filter(w=>w.path==='/v1/analysis/daily/groups')).toHaveLength(1);
  expect(writes[1].path).toBe('/v1/analysis/daily/groups/round-1/run');
  expect(writes[0].key).toBeTruthy();expect(writes[1].key).toBeTruthy();
  expect(writes[0].body.selections).toEqual([{model_key:'A',profile_id:'cpa',model_id:'gpt-6.1-sol'}]);
  expect(writes[1].body).toMatchObject({mode:'real',max_calls:1,cpa_profile_id:null});
  fireEvent.click(screen.getByRole('checkbox',{name:/我明确同意本轮最多1次/}));
  fireEvent.click(screen.getByRole('checkbox',{name:/gpt-6-astra · SYNTHETIC CPAhigh/}));
  expect(screen.getByRole('button',{name:'开始所选2个模型分析'}).hasAttribute('disabled')).toBe(true);
});

it('各回答并列，没有自动主回答；原文仅在展开时读取',async()=>{
  const g=group(['A','B']);g.arms=g.arms.map(a=>({...a,state:'succeeded',version_id:'version-demo',summary:'独立保存的回答',answer_source:'model_api',answer_model_id:a.model_id}));
  render(<QueryClientProvider client={cache}><MemoryRouter><DailyResults group={g}/></MemoryRouter></QueryClientProvider>);
  expect(screen.getAllByRole('article')).toHaveLength(2);expect(reads).toEqual([]);
  expect(screen.getByText(/不指定主回答/)).toBeTruthy();
  fireEvent.click(screen.getAllByRole('button',{name:'查看完整回答与原文'})[0]);
  await waitFor(()=>expect(reads).toEqual(['/v1/forecasts/f-A/versions/version-demo']));
  expect(screen.getAllByRole('link',{name:'分析包／手工导入'}).map(e=>e.getAttribute('href'))).toEqual(['/analysis?group=round-1&request=q-A','/analysis?group=round-1&request=q-B']);
});

it('同一模型不同渠道可同时选择，最多四份；选择来源写入准备请求且不自动发起',async()=>{
  synthetic=false;
  profileItems.push({profile_id:'official',name:'DeepSeek官方',base_url:'https://api.deepseek.com/v1',model_id:'deepseek-flash',model_ids:['deepseek-flash','deepseek-v4-pro'],enabled:true},
    {profile_id:'relay',name:'鸡出没',base_url:'https://xcmapi.org/v1',model_id:'deepseek-v4-pro',model_ids:['deepseek-v4-pro'],enabled:true},
    {profile_id:'pending',name:'未配置',base_url:'https://pending.invalid/v1',model_id:'待选择模型',enabled:false});
  show();await screen.findByRole('checkbox',{name:/deepseek-v4-pro · 鸡出没/});
  for(const pattern of [/deepseek-flash · DeepSeek官方/,/deepseek-v4-pro · DeepSeek官方/,/deepseek-v4-pro · 鸡出没/])fireEvent.click(screen.getByRole('checkbox',{name:pattern}));
  expect((screen.getByRole('checkbox',{name:/gpt-6-astra/}) as HTMLInputElement).disabled).toBe(true);
  expect(screen.queryByRole('checkbox',{name:/待选择模型/})).toBeNull();expect(writes).toHaveLength(0);
  fireEvent.click(screen.getByRole('button',{name:'仅准备分析包（不发送）'}));
  await waitFor(()=>expect(writes).toHaveLength(1));
  expect(writes[0].body.selections).toEqual([{model_key:'A',profile_id:'cpa',model_id:'gpt-6.1-sol'},
    {model_key:'B',profile_id:'official',model_id:'deepseek-flash'},{model_key:'C',profile_id:'official',model_id:'deepseek-v4-pro'},
    {model_key:'D',profile_id:'relay',model_id:'deepseek-v4-pro'}]);
  expect(writes.some(w=>w.path.includes('activate') || w.path.endsWith('/run'))).toBe(false);
});


it('准备时使用专用行情身份，不先等待整个工作台，启动仍沿用原冻结包',async()=>{
  show();await waitFor(()=>expect(screen.getByRole('button',{name:'仅准备分析包（不发送）'}).hasAttribute('disabled')).toBe(false));
  const readStart=reads.length;
  fireEvent.click(screen.getByRole('button',{name:'仅准备分析包（不发送）'}));
  await screen.findByText(/资料已保存，尚未发送/);
  expect(writes[0].body.market_snapshot_id).toBe('fresh-source-id');
  expect(reads.slice(readStart)[0]).toBe('/v1/analysis/daily/source');
  expect(reads.filter(p=>p==='/v1/analysis/daily/source')).toHaveLength(1);
  fireEvent.click(screen.getByRole('button',{name:'开始模拟分析（不收费）'}));
  await waitFor(()=>expect(writes).toHaveLength(2));
  expect(reads.filter(p=>p==='/v1/analysis/daily/source')).toHaveLength(1);
  expect(writes.filter(p=>p.path==='/v1/analysis/daily/groups')).toHaveLength(1);
});


it('新分析默认26替换25，仅准备不调用；14与23仍可选择',async()=>{
  show();await waitFor(()=>expect(screen.getByRole('button',{name:'仅准备分析包（不发送）'}).hasAttribute('disabled')).toBe(false));
  const template=screen.getByRole('combobox',{name:'本次分析包模板'}) as HTMLSelectElement;
  expect(template.value).toBe('candidate26');expect(writes).toHaveLength(0);
  expect(Array.from(template.options).map(o=>o.value)).toEqual(['current','improved','candidate26','candidate27','candidate28']);
  expect(screen.getByText(/26版保留25版资料与时间表/)).toBeTruthy();
  fireEvent.click(screen.getByRole('button',{name:'仅准备分析包（不发送）'}));await screen.findByText(/资料已保存，尚未发送/);
  expect(writes).toHaveLength(1);expect(writes[0].body.package_kind).toBe('candidate26');
});

it('27候选须主动选择，仅准备不发送，默认26保持',async()=>{
  show();await waitFor(()=>expect(screen.getByRole('button',{name:'仅准备分析包（不发送）'}).hasAttribute('disabled')).toBe(false));
  const template=screen.getByRole('combobox',{name:'本次分析包模板'}) as HTMLSelectElement;
  expect(template.value).toBe('candidate26');
  fireEvent.change(template,{target:{value:'candidate27'}});
  expect(screen.getByText(/27版候选保留26版全部资料和时间规则/)).toBeTruthy();expect(writes).toHaveLength(0);
  fireEvent.click(screen.getByRole('button',{name:'仅准备分析包（不发送）'}));await waitFor(()=>expect(writes).toHaveLength(1));
  expect(writes[0].body.package_kind).toBe('candidate27');expect(writes.some(w=>w.path.endsWith('/run'))).toBe(false);
});

it.each(['candidate24','candidate25'] as const)('旧%s窗口更新保留原模板，切回新分析恢复26',async legacy=>{
  const old=group(['A']);old.package_kind=legacy;old.template_version='manual-analysis/'+legacy.slice(-2);
  old.arms[0]={...old.arms[0],state:'succeeded',version_id:'version-demo'};groups=[old];
  show();await screen.findByText(new RegExp(old.template_version+' · 资料截止'));
  fireEvent.change(screen.getByRole('combobox',{name:'本次分析方式'}),{target:{value:'update'}});
  const template=screen.getByRole('combobox',{name:'本次分析包模板'}) as HTMLSelectElement;
  expect(template.value).toBe(legacy);expect(template.disabled).toBe(true);
  fireEvent.click(screen.getByRole('button',{name:'仅准备分析包（不发送）'}));await waitFor(()=>expect(writes).toHaveLength(1));
  expect(writes[0].body).toMatchObject({package_kind:legacy,kind:'update',base_group_id:'round-1'});
  fireEvent.click(screen.getByRole('button',{name:'重新准备新一轮'}));expect(template.value).toBe('candidate26');
  expect(Array.from(template.options).some(o=>o.value===legacy)).toBe(false);
});


it('28经验自检候选须主动选择，准备不发送且默认仍26',async()=>{
  show();await waitFor(()=>expect(screen.getByRole('button',{name:'仅准备分析包（不发送）'}).hasAttribute('disabled')).toBe(false));
  const select=screen.getByRole('combobox',{name:'本次分析包模板'});
  expect((select as HTMLSelectElement).value).toBe('candidate26');
  fireEvent.change(select,{target:{value:'candidate28'}});
  expect(await screen.findByText(/28版候选保留27版全部资料与时间规则/)).toBeTruthy();
  expect(writes).toHaveLength(0);
  fireEvent.click(screen.getByRole('button',{name:'仅准备分析包（不发送）'}));
  await screen.findByText(/资料已保存，尚未发送/);
  expect(writes).toHaveLength(1);expect(writes[0].body.package_kind).toBe('candidate28');
  expect(writes.some(w=>w.path.endsWith('/run'))).toBe(false);
});
