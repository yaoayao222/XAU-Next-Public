# 内部领域交接边界

首版内部接口使用 `shared.snapshots.Snapshot` 保存规范 JSON 并按需返回独立副本。
公开字段形状引用 OpenAPI 同名对象，不另写同义字段源。传输层显式映射并按规范验证。

- market 输出 MarketSnapshot、Portfolio 原始事实快照；Snapshot 不携带 SDK 对象。
- analysis 接收 MarketSnapshot、Portfolio、Settings、复盘经验；返回 AnalysisRequest、ForecastVersion 与待登记 Plan 定义。
- plans 接收 Plan、MarketSnapshot、Portfolio、Settings；输出 PlanRecord、Evaluation。
  `get_evaluation_inputs(evaluation_id, scope=...)` 返回冻结输入的只读投影：行情头部与 `market_snapshot_ref`，以及计划、持仓、设置和用户输入；引用由 market 的公开 `get_snapshot` 还原。旧内联评估在读取时投影为同形状，原记录不改写。计算和生命周期仍接收完整 MarketSnapshot。
- portfolio 接收已读取 Portfolio/TradeDeal 事实，输出 Portfolio、TradeDetail、Association、Decision。
- review 接收被冻结版本/方案/成交和市场窗口；输出 Review 与带 available_at 的经验摘要。
- settings 返回 Settings 及指定 revision 的同形历史快照。

跨域用例由 workflows 传入上述不可变快照。所有领域写入必须通过自身 public 入口。
跨域原子用例共享 `Database.write()` 返回的不透明 UoW，读聚合共享 `Database.read()`。
领域独占 `Database.documents(owner)`；get/list/insert/update/history 只能访问该 owner。
该基础设施边界不进入 HTTP，不允许传出 ORM、Session 或可变共享字典。

只读成交内部 envelope 为 `{trade_id,account_scope_id,broker_position_id,symbol,side,opened_at,currency,deals}`。
每个 deal 是公开 TradeDeal 加真实券商 `entry_kind`（in/out/inout/out_by），不能从正负利润猜入出方向。
market 的 PreparedCapture.deals 为上述 Snapshot 元组；portfolio 负责去重、修订和生命周期归集。
关联用例传入 `{plan: PlanRecord, account_scope_id, symbol}`，身份由计划的原始分析快照确定。
历史缺失或反转无法明确拆分时，生命周期为 unknown，保留已知金额小计与缺失原因。
