import React from 'react';
import { afterEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { DailyPerformancePanel } from '../../../apps/web/src/features/history/DailyPerformancePanel';
import { workspace } from '../fixtures';

afterEach(()=>{cleanup();vi.unstubAllGlobals();localStorage.clear();});
it('显示命中分母、未发送与未知；筛选传入统一统计接口，不编造冠军',async()=>{
  const cache=new QueryClient({defaultOptions:{queries:{retry:false}}});const queries:URLSearchParams[]=[];
  sessionStorage.setItem('xau-next-session-token','SYNTHETIC-only');
  vi.stubGlobal('fetch',vi.fn(async(input:Request)=>{
    const url=new URL(input.url);expect(input.method).toBe('GET');
    let value:unknown;
    if(url.pathname==='/v1/workspace')value={...workspace,daily_group:null,market:{...workspace.market,source_kind:'synthetic'}};
    else if(url.pathname==='/v1/analysis/daily/groups')value={items:[],next_cursor:null};
    else if(url.pathname==='/v1/analysis/daily/performance'){
      queries.push(url.searchParams);value={as_of:workspace.as_of,records_considered:6,legacy_records_included:2,message:'不自动排名',rows:[{
        model_id:'gpt-6.1-sol',template_version:'manual-analysis/14',minutes:30,source_scope:'SYNTHETIC',data_kind:'SYNTHETIC',
        method_version:'prediction_review/3',configuration:'SYNTHETIC/responses/high',evaluation_scope:'version_descriptive',
        records:6,ended:6,pending:0,clear:3,uncertain:1,scored:2,correct:1,incorrect:1,unknown:3,failed:1,not_sent:1,row_id:'row',display_counts:{correct:1,incorrect:1,uncertain:1,unknown:1,failed:1,not_sent:1,cancelled:0,call_unknown:0,pending:0,processing:0},
        range_scored:3,range_covered:2,range_mae:'2.555',range_width:'8.30',non_overlapping_records:2,overlapping_records:1,note:'同源评价'
      }]};
    }else throw new Error('Unexpected '+url.pathname);
    return new Response(JSON.stringify(value),{headers:{'Content-Type':'application/json'}});
  }));
  render(<QueryClientProvider client={cache}><MemoryRouter><DailyPerformancePanel/></MemoryRouter></QueryClientProvider>);
  expect(await screen.findByText(/能核验的2个明确判断中，1个方向正确、1个错误/)).toBeTruthy();
  expect(screen.getByText(/全部登记命中 1\/6/)).toBeTruthy();
  expect(screen.getByText(/已结束方向未知 3/)).toBeTruthy();
  expect(screen.getByText(/SYNTHETIC合成演示，不能用于选择真实模型/)).toBeTruthy();
  fireEvent.click(screen.getByRole('button',{name:'更多筛选'}));
  fireEvent.click(screen.getByRole('checkbox',{name:'只看同时同资料、至少两个模型的轮次'}));
  fireEvent.change(screen.getByLabelText('查看分析包'),{target:{value:'manual-analysis/23'}});
  await waitFor(()=>expect(queries.at(-1)?.get('template')).toBe('manual-analysis/23'));
  expect(queries.at(-1)?.get('paired_only')).toBe('true');expect(queries.at(-1)?.get('data_kind')).toBe('SYNTHETIC');
  cache.clear();
});
