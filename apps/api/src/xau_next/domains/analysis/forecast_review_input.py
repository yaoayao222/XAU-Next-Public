"""Opt-in candidate: deterministic evidence quality and recent price relations only."""
from datetime import timedelta
from decimal import Decimal

from xau_next.shared.values import parse_utc

PROMPT_VERSION = "manual-analysis/22"
INPUT_REVISION = "shared-search/2"

INSTRUCTIONS = (
    "# 固定候选22：先核对资料，再分别判断端点与范围\n"
    "forecast_review_input是本机根据同份冻结资料计算的辅助表，不是另一个模型的预测。"
    "先区分已知、时点未核实和缺失：scheduled_time_elapsed_actual_missing表示日程时点已过但实际值缺失，"
    "不能说尚未公布、没有事件，不能用forecast/previous或后来查到的数值补成actual。"
    "publication_age_screen仅筛查网页标注的发布时间；24小时是资料检查标记，不是交易阈值，"
    "新近抓取或新近发布的网页仍可能引用旧数据。美元/利率没有带同步观测时点的序列时，"
    "只能作来源背景，不能把报道中的数值当作截止点实时变化。\n"
    "每个30/60/180分钟的forecast_basis.direction_basis分别比较：本周期最强支持、最强反证，"
    "以及主路径为何在原端点更占优；引用具体闭柱、价位、时间或资料缺口，避免堆叠同义证据。"
    "短线反弹不自动等于30分钟终点上涨，大周期下跌也不自动等于短期终点下跌。"
    "方向不限于顺大周期或逆大周期；发生冲突时解释哪项事实决定取舍，缺少的突破或延续确认须明示。"
    "条件方案未来可能触发不代表已经触发，也不能充当无条件端点判断的证据。"
    "无法判断仍用uncertain并在limitations指出具体缺口，不为减少错误全部弃判，"
    "也不为提高明确率硬猜；不编造概率或已验证的优势。\n"
    "分别解释expected_range下界和上界：当前报价到该边界的价格距离、支持它的结构/波动参照、"
    "本周期内可能越过该边界的反证。历史支撑阻力是参照，不是未来极值的硬边界；"
    "数值自检：低于预测下界的价位或高于预测上界的价位不能写成已被本区间包含。"
    "下跌端点与途中反弹、上涨端点与途中回落可以同时存在，不让方向强制裁掉另一侧。"
    "不复制所有旧极值扩大范围保覆盖，不机械缩窄；资料不足或尾部风险写入原有理由与局限。"
    "recent_price_relations只使用连续、完整、已闭柱，差值按标明的Bid/Ask口径，"
    "不能把数值正负、最近三根变化或历史案例直接当作未来交易规则。\n"
    "使用原analysis_basis、forecast_basis、summary、candidate_assessments和局限字段作答；"
    "不新增回答字段，不删减Schema要求，不反复复述同一案例、通用规则或已过期分析。"
    "已有引用、条件、费用、持仓和人工交易约束完整保留。\n"
)


def build_review_input(request, economic, market):
    """Consume validated economic context and closed-bar packet; never fetch or mutate."""
    cutoff = parse_utc(request["market_cutoff"])
    events = []
    for event in economic["events"]:
        at = parse_utc(event["scheduled_at"])
        state = ("actual_provided" if event["actual"] is not None else
                 "scheduled_time_elapsed_actual_missing" if at <= cutoff else "scheduled_after_cutoff")
        events.append({"event_id": event["event_id"], "source_id": event["source_id"],
                       "scheduled_at": event["scheduled_at"], "state": state})
    sources = []
    for query in economic.get("shared_research", {}).get("queries", []):
        for row in query["results"]:
            age = None if row["published_at"] is None else int((cutoff-parse_utc(row["published_at"])).total_seconds())
            sources.append({"source_id": row["source_id"], "component": query["component"],
                            "published_age_seconds": age,
                            "publication_age_screen": "publication_time_unknown" if age is None else
                            "published_more_than_24h_ago" if age > 86400 else "published_within_24h",
                            "observation_time_verified": False})
    observations = []
    durations = {"M1": 1, "M5": 5, "M15": 15, "H1": 60}
    quote = market.get("quote")
    for series in market.get("series", []):
        minutes = durations.get(series["timeframe"])
        if minutes is None:
            continue
        bars = sorted([b for b in series["bars"] if b.get("closed") and parse_utc(b["close_at"]) <= cutoff],
                      key=lambda b: b["open_at"])[-3:]
        duration = timedelta(minutes=minutes)
        complete = len(bars) == 3 and all(parse_utc(b["close_at"])-parse_utc(b["open_at"]) == duration for b in bars)
        continuous = complete and all(parse_utc(bars[i]["close_at"]) == parse_utc(bars[i+1]["open_at"]) for i in range(2))
        recent = continuous and timedelta(0) <= cutoff-parse_utc(bars[-1]["close_at"]) < duration
        row = {"timeframe": series["timeframe"], "price_basis": series["price_basis"],
               "state": "available" if recent else "insufficient_or_stale_closed_bars",
               "bars": [{k: b[k] for k in ("open_at", "close_at", "high", "low", "close")} for b in bars]}
        if recent:
            high = max(Decimal(b["high"]) for b in bars)
            low = min(Decimal(b["low"]) for b in bars)
            row.update(close_changes=[str(Decimal(bars[i+1]["close"])-Decimal(bars[i]["close"])) for i in range(2)],
                       recent_high=str(high), recent_low=str(low))
            side = {"bid": "bid", "ask": "ask"}.get(series["price_basis"].lower())
            if quote and side:
                row.update(quote_minus_recent_high=str(Decimal(quote[side])-high),
                           quote_minus_recent_low=str(Decimal(quote[side])-low))
        observations.append(row)
    return {"version": "forecast-review-input/1", "as_of": request["market_cutoff"],
            "event_value_states": events, "search_source_quality": sources,
            "macro_observation_state": "synchronized_usd_rates_series_not_provided",
            "recent_price_relations": observations,
            "limitations": ["资料状态不等于事件影响大小，缺实际值不证明实际尚未公布。",
                            "网页发布时间与所述数据的观测时间不同，来源全部保留，未认证实时宏观序列。",
                            "价格关系仅为冻结事实算术，不给未来方向、概率或最优阈值。"]}
