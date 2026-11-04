import { useEffect, useState } from 'react';
import { BrowserRouter, NavLink, Route, Routes } from 'react-router-dom';
import { QueryClient, QueryClientProvider, useQueryClient } from '@tanstack/react-query';
import { api, hasSessionToken } from '../api/client';
import { useSettings, useWorkspace } from '../api/queries';
import { subscribeEvents, type ConnectionState } from '../api/events';
import { WorkspacePage } from '../features/workspace/WorkspacePage';
import { AnalysisPage } from '../features/analysis/AnalysisPage';
import { HistoryPage } from '../features/history/HistoryPage';
import { SettingsPage } from '../features/settings/SettingsPage';
import { Badge } from '../shared/ui';
import { setDisplayTimezone, time } from '../shared/format';

export const queryClient = new QueryClient({ defaultOptions: { queries: { retry: 1, staleTime: 5000, refetchOnWindowFocus: true }, mutations: { retry: false } } });
function Shell() {
  const cache = useQueryClient();
  const workspace = useWorkspace();
  const settings = useSettings();
  const [connection, setConnection] = useState<ConnectionState>('connecting');
  const [initialCursor, setInitialCursor] = useState<number | null>(null);
  const zone = settings.data?.values.display_timezone ?? 'Asia/Shanghai';
  setDisplayTimezone(zone);
  useEffect(() => { if (initialCursor === null && workspace.data) setInitialCursor(workspace.data.event_cursor); }, [initialCursor, workspace.data]);
  useEffect(() => {
    if (initialCursor === null) return;
    return subscribeEvents(initialCursor, () => { void cache.invalidateQueries({}, { cancelRefetch: false }); }, setConnection, async () => {
      await cache.cancelQueries();
      cache.removeQueries({ predicate: query => query.queryKey[0] !== 'workspace' });
      const fresh = await api.workspace(); cache.setQueryData(['workspace'], fresh); return fresh.event_cursor;
    });
  }, [cache, initialCursor]);
  const status = workspace.isError ? 'offline' : connection;
  const connectionText = { connecting: '正在连接', online: '状态同步中', reconnecting: '正在恢复连接', offline: '连接中断' }[status];
  return <div className="app-shell"><a className="skip-link" href="#main">跳到内容</a><aside className="sidebar"><NavLink to="/" className="brand" aria-label="XAU Next 工作台"><span className="brand-symbol">X</span><span>XAU <b>NEXT</b><small>分析 · 决策 · 复盘</small></span></NavLink><nav aria-label="主导航"><NavLink end to="/"><span>01</span>工作台</NavLink><NavLink to="/analysis"><span>02</span>分析与更新</NavLink><NavLink to="/history"><span>03</span>历史与复盘</NavLink><NavLink to="/settings"><span>04</span>设置</NavLink></nav><div className="sidebar-footer"><span className="status-dot" /> MT5 只读连接<p>所有交易均由你在 MT5 手工完成。</p><small>V1 本地候选版</small></div></aside><div className="main-shell"><div className="topbar"><span>个人交易研究工作区</span><div><Badge tone={status === 'online' ? 'good' : 'warn'}>{connectionText}</Badge><span>{zone === 'Asia/Shanghai' ? '北京时间' : zone}</span></div></div>{!hasSessionToken() && <div className="banner warn">未收到本机会话令牌。请通过 XAU Next 启动器打开页面。</div>}{workspace.data?.market && workspace.data.market.source_kind !== 'mt5' && <div className="banner synthetic">{workspace.data.market.source_kind === "synthetic" ? "合成样例数据 · 用于功能验收，不是实际市场报价" : "导入的数据 · 请核对随附来源与事实时间"}</div>}{status === 'offline' && <div className="banner warn" role="status">本地服务暂不可用，显示最后取得的记录（{time(workspace.data?.as_of, zone)}）。当前条件需要重新读取确认。</div>}<main id="main" tabIndex={-1}><Routes><Route path="/" element={<WorkspacePage />} /><Route path="/analysis" element={<AnalysisPage />} /><Route path="/history" element={<HistoryPage />} /><Route path="/settings" element={<SettingsPage />} /><Route path="*" element={<WorkspacePage />} /></Routes></main><footer className="page-footer">事实来自读取，判断来自分析，金额由系统计算。<span>人工执行 · 本机保存</span></footer></div></div>;
}
export function App() { return <QueryClientProvider client={queryClient}><BrowserRouter><Shell /></BrowserRouter></QueryClientProvider>; }
