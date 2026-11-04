"""Candidate23: pure as-of validation and matching for independently sourced actuals."""
import re
from copy import deepcopy
from decimal import Decimal

from xau_next.shared.errors import DomainError
from xau_next.shared.values import parse_utc

PROMPT_VERSION = "manual-analysis/23"
INPUT_REVISION = "economic-actuals/1"
URL = "https://www.sca.isr.umich.edu/"
TITLES = {"Prelim UoM Consumer Sentiment": ("preliminary", "consumer_sentiment"),
          "Revised UoM Consumer Sentiment": ("final", "consumer_sentiment"),
          "Prelim UoM Inflation Expectations": ("preliminary", "inflation_expectations_1y"),
          "Revised UoM Inflation Expectations": ("final", "inflation_expectations_1y")}
INSTRUCTIONS = (
    "# 候选23：官方经济实际值补充\n"
    "actual_source_id标明独立的官方实际值来源；source_id仍是日历日程、预期和前值的来源，不能混为一家。"
    "actual_available_at是本程序取得该页的保守可用时点，不是原始公布时刻；不能回填旧预测。"
    "actual_supplement只覆盖标明月份及初值/终值的密歇根信心与一年通胀预期，"
    "不是五年通胀预期，也不代表其他事件实际值或实时美元/利率已齐。"
    "matched_event_actuals是最近日历事件的已取得实际值背景；它不重置30/60/180分钟端点，"
    "不能机械从数据好坏决定金价涨跌。未匹配、过早、缺值保持未知。\n")


def invalid(message="官方实际值来源、版本或截止时间无效。"):
    raise DomainError("ECONOMIC_ACTUALS_INVALID", message, 409)


def project_actuals(value, *, cutoff, data_kind):
    if not isinstance(value, dict) or value.get("version") != "umich-actuals/1" or value.get("data_kind") != data_kind:
        invalid()
    period, kind = value.get("reference_period"), value.get("release_kind")
    if not isinstance(period, str) or not re.fullmatch(r"20\d{2}-(?:0[1-9]|1[0-2])", period) or kind not in {"preliminary", "final"}:
        invalid()
    source = value.get("source")
    if not isinstance(source, dict):
        invalid()
    source = {k: deepcopy(source.get(k)) for k in ("source_id", "publisher", "url", "captured_at", "content_sha256")}
    if (source["source_id"] != "umich-" + period + "-" + kind or source["url"] != URL
            or source["publisher"] != "University of Michigan Surveys of Consumers"
            or not isinstance(source["content_sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", source["content_sha256"])):
        invalid()
    if parse_utc(source["captured_at"]) > parse_utc(cutoff):
        invalid("官方实际值在预测截止后才取得，不能补入原试卷。")
    if value.get("availability_basis") != "first_observed_at_capture_not_original_publication" or value.get("original_publication_at") is not None:
        invalid("不能把网页获取时间或日程时间冒充首次发布时间。")
    values = value.get("values")
    if not isinstance(values, dict) or set(values) != {"consumer_sentiment", "inflation_expectations_1y"}:
        invalid()
    sentiment, inflation = values["consumer_sentiment"], values["inflation_expectations_1y"]
    if not isinstance(sentiment, str) or not re.fullmatch(r"\d+(?:\.\d+)?", sentiment) or not Decimal("0") <= Decimal(sentiment) <= Decimal("200"):
        invalid()
    if inflation is not None and (not isinstance(inflation, str) or not re.fullmatch(r"-?\d+(?:\.\d+)?%", inflation)
                                 or not Decimal("-20") <= Decimal(inflation[:-1]) <= Decimal("100")):
        invalid()
    state = "available" if inflation is not None else "partial"
    missing = [] if inflation is not None else ["inflation_expectations_1y_not_unambiguously_parsed"]
    if value.get("state") != state or value.get("missing") != missing:
        invalid()
    return {"version": "umich-actuals/1", "data_kind": data_kind, "reference_period": period,
            "release_kind": kind, "state": state, "source": source, "values": deepcopy(values),
            "missing": missing, "availability_basis": value["availability_basis"], "original_publication_at": None}


def matched_actuals(events, supplement):
    result, seen = {}, set()
    if not isinstance(events, list) or len(events) > 512 or any(not isinstance(e, dict) for e in events):
        invalid()
    for event in events:
        match = TITLES.get(event.get("title"))
        at = parse_utc(event["scheduled_at"])
        if not match or event.get("currency") != "USD" or at.strftime("%Y-%m") != supplement["reference_period"] or match[0] != supplement["release_kind"]:
            if event.get("actual_source_id") is not None:
                invalid("实际值来源被关联到其他月份或其他指标。")
            continue
        if match[1] in seen:
            invalid("同月同版本的指标日程重复，不能猜测关联。")
        seen.add(match[1])
        captured = supplement["source"]["captured_at"]
        value = supplement["values"][match[1]]
        if at > parse_utc(captured) or value is None:
            if event.get("actual_source_id") is not None:
                invalid("事件尚未到时或实际值缺失，不能保留补入值。")
            continue
        result[event["event_id"]] = {"event_id": event["event_id"], "title": event["title"],
            "scheduled_at": event["scheduled_at"], "actual": value, "actual_available_at": captured,
            "actual_source_id": supplement["source"]["source_id"]}
    return result
