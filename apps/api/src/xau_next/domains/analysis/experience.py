"""Explicit analysis-facing experience projection; preserve meaning without leaking context."""
import re
import json
from copy import deepcopy

from xau_next.shared.errors import DomainError

API_OUTBOUND_VERSION = "api-projection/6"


def _fields(names, **children):
    return {**{key: None for key in names.split()}, **children}


RANGE = _fields("low high")
HORIZON = _fields("horizon_minutes end_at direction confidence", expected_range=RANGE,
    phases=[_fields("start_at end_at direction note")],
    turning_points=[_fields("kind earliest_at latest_at rationale note", price_range=RANGE)],
    forecast_basis=_fields("direction_basis range_basis", supporting_evidence=[None], opposing_evidence=[None],
                          evidence_refs=[None], limitations=[None]))
METHOD = _fields("neutral_band price_basis bar_timeframe turning_left_bars turning_right_bars "
                 "execution_model_version simulation_delay_ms execution_quote_wait_ms")
INPUTS = _fields("forecast_version_id plan_id trade_id horizon_minutes evaluation_scope cost_settings_revision",
                 market_snapshot_ids=[None], method_parameters=METHOD)
METRIC = _fields("name value unit state reason segment_index observed_label")
SIGNATURE = _fields("version scope_key state cutoff window_minutes price_basis shape direction range_bps efficiency")
SELECTION_REASON = _fields("reason horizon_match method_match outcome_bucket market_comparison", historical_signature=SIGNATURE)
CASE = _fields("review_ref target_id target_kind available_at window_start window_end method_version "
               "outcome coverage evaluated_start evaluated_end metric_selection interpretation source_kind",
    source_refs=[None], reasons=[None], inputs=INPUTS, metrics=[METRIC],
    original_prediction=_fields("summary summary_policy", horizon=HORIZON), selection=SELECTION_REASON)
SUMMARY = _fields("total unique_targets scorable unknown not_triggered_or_not_entered unscorable "
                  "net_unknown_or_not_applicable", outcomes="counts", methods="counts",
                  evaluation_scopes="counts", known_net_totals="money_totals")
FORECAST_COUNTS = _fields("correct incorrect not_predicted unknown numerator denominator rate rate_state")
FORECAST_STATISTICS = _fields("version record_total population scope", notes=[None],
    rows=[_fields("horizon_minutes method_version evaluation_scope coverage source_kind endpoint_max_age_ms total unique_targets unique_versions",
                  method_parameters=METHOD, direction=FORECAST_COUNTS, range_containment=FORECAST_COUNTS)])
CASE_SELECTION = _fields("version candidate_total eligible_forecasts review_method selected_count unique_forecasts reused_forecast_count",
    excluded="counts", requested_horizons=[None], covered_horizons=[None], target_signature=SIGNATURE,
    outcome_counts="counts", notes=[None])
EXPERIENCE = _fields("schema_version digest_id cutoff available_at period_start period_end "
    "review_reference_total omitted_reference_count source_set_sha256 reference_policy",
    groups={key: SUMMARY for key in ("forecast", "reference_plan", "actual_trade")},
    review_refs=[None], notes=[None], cases=[CASE], forecast_statistics=FORECAST_STATISTICS, case_selection=CASE_SELECTION)


def _project(value, shape, path):
    if value is None:
        return None
    if shape is None:
        if isinstance(value, (str, int, float, bool)):
            return value
    elif isinstance(shape, list) and isinstance(value, list):
        return [_project(item, shape[0], f"{path}[{index}]") for index, item in enumerate(value)]
    elif isinstance(shape, dict) and isinstance(value, dict):
        return {key: _project(value[key], child, path + "." + key)
                for key, child in shape.items() if key in value}
    elif shape == "counts" and isinstance(value, dict):
        return {key: count for key, count in value.items()
                if re.fullmatch(r"[A-Za-z0-9_./:-]{1,100}", key)
                and isinstance(count, int) and not isinstance(count, bool)}
    elif shape == "money_totals" and isinstance(value, dict):
        return {key: _project(item, _fields("amount known_count"), path + "." + key)
                for key, item in value.items() if re.fullmatch(r"[A-Z]{3,8}", key)}
    raise DomainError("EXPERIENCE_PROJECTION_INVALID", "经验外发结构无法忠实投影：" + path, 422)


def project_experience(value):
    """Absent keys stay absent; null, zero, labels, dates and opaque IDs stay unchanged."""
    return _project(value, EXPERIENCE, "experience")


def _named_rows(values):
    if not values or not all(isinstance(v,dict) for v in values):
        return values
    groups=[]
    for value in values:
        columns=list(value)
        if not groups or groups[-1]['columns']!=columns:
            groups.append({'columns':columns,'rows':[]})
        groups[-1]['rows'].append([value[k] for k in columns])
    result={'encoding':'named-rows/1','groups':groups}
    compact=lambda v:len(json.dumps(v,ensure_ascii=False,separators=(',',':')).encode())
    return result if compact(result)<compact(values)*.9 else values


def compact_experience(value):
    """Remove repeated field names only; preserve order, absent keys, null and every value."""
    if value is None:
        return None
    result=deepcopy(value)
    statistics=result.get('forecast_statistics')
    if statistics and isinstance(statistics.get('rows'),list):
        statistics['rows']=_named_rows(statistics['rows'])
    for case in result.get('cases',[]):
        if isinstance(case.get('metrics'),list):
            case['metrics']=_named_rows(case['metrics'])
    if 'cases' in result:
        result['cases']=_named_rows(result['cases'])
    return result


def expand_experience(value):
    if value is None:
        return None
    result=deepcopy(value)
    def expand(rows):
        if isinstance(rows,dict) and rows.get('encoding')=='named-rows/1':
            return [dict(zip(group['columns'],row,strict=True)) for group in rows['groups'] for row in group['rows']]
        return rows
    statistics=result.get('forecast_statistics')
    if statistics and 'rows' in statistics:
        statistics['rows']=expand(statistics['rows'])
    if 'cases' in result:
        result['cases']=expand(result['cases'])
        for case in result['cases']:
            if 'metrics' in case:
                case['metrics']=expand(case['metrics'])
    return result


def experience_references(experience):
    if not experience:
        return set()
    return {ref for ref in [experience.get("digest_id"), *experience.get("review_refs", []),
        *(case.get("review_ref") for case in experience.get("cases", []))] if isinstance(ref, str)}


def non_experience_references(context):
    """Keep references shared with other frozen inputs; the reference list alone is not use."""
    def strings(value):
        if isinstance(value, str):
            return {value}
        if isinstance(value, dict):
            return set().union(*(strings(item) for item in value.values()))
        if isinstance(value, list):
            return set().union(*(strings(item) for item in value))
        return set()
    allowed = set(context.get("evidence_refs", []))
    other = strings({key: value for key, value in context.items() if key not in {"experience", "evidence_refs"}})
    return allowed - experience_references(context.get("experience")) | (allowed & other)


def validate_experience_application(answer, context):
    """Validate traceability, not the truth of the model's explanation or its efficacy."""
    def invalid(code, path, message):
        raise DomainError(code, message, 422, [{"path": path, "code": code, "message": message}])
    experience = context.get("experience")
    application = answer["experience_application"]
    adjustments = answer["prediction_adjustments"]
    expected = experience.get("digest_id") if experience else None
    if application["digest_id"] != expected:
        invalid("EXPERIENCE_DIGEST_MISMATCH", "/experience_application/digest_id", "摘要身份必须与实际冻结经验一致；没有经验时为null。")
    assessments = application["case_assessments"]
    if not experience:
        if application["status"] != "unavailable" or assessments or adjustments:
            invalid("EXPERIENCE_STATUS_INVALID", "/experience_application", "本包没有经验；须填unavailable及空案例、空调整，不补造采用。")
        return
    if application["status"] == "unavailable":
        invalid("EXPERIENCE_STATUS_INVALID", "/experience_application/status", "本包已提供经验；可以评估后不采用，但不能声称未收到。")
    available = {case["review_ref"] for case in experience.get("cases", [])}
    current_refs = non_experience_references(context)
    registered, adopted = set(), set()
    for index, case in enumerate(assessments):
        path = f"/experience_application/case_assessments/{index}"
        ref = case["review_ref"]
        if ref not in available:
            invalid("EXPERIENCE_CASE_REF_INVALID", path + "/review_ref", "案例引用必须来自本次实际提供的cases。")
        if ref in registered:
            invalid("EXPERIENCE_CASE_DUPLICATE", path + "/review_ref", "同一经验案例不能重复登记。")
        registered.add(ref)
        if not set(case["current_evidence_refs"]) <= current_refs:
            invalid("EXPERIENCE_CURRENT_REF_INVALID", path + "/current_evidence_refs", "当前依据须来自本包非经验专属来源，不得引用未知或仅属于历史案例的身份。")
        if case["decision"] == "adopt":
            if case["relevance"] not in {"applicable", "partial"} or not case["current_evidence_refs"]:
                invalid("EXPERIENCE_STATUS_INVALID", path, "采用须有适用或部分适用判断及当前依据引用；未知或不相似可拒绝采用。")
            adopted.add(ref)
    horizons = {item["horizon_minutes"] for item in answer["horizons"]}
    related = set()
    for index, adjustment in enumerate(adjustments):
        path = f"/prediction_adjustments/{index}"
        if adjustment["horizon_minutes"] not in horizons:
            invalid("EXPERIENCE_HORIZON_INVALID", path + "/horizon_minutes", "调整说明只能引用本次回答实际包含的预测周期。")
        refs = set(adjustment["related_review_refs"])
        if not refs <= registered:
            invalid("EXPERIENCE_ADJUSTMENT_REF_INVALID", path + "/related_review_refs", "调整关联的案例必须先在采用说明中登记。")
        related |= refs
    if application["status"] == "applied":
        if not adopted:
            invalid("EXPERIENCE_STATUS_INVALID", "/experience_application/status", "applied须至少有一条adopt记录；不强制改变预测。")
        if not adopted <= related:
            gap = _experience_effect_gap(assessments, adopted, related)
            raise DomainError("EXPERIENCE_EFFECT_MISSING", gap['detail'], 422, [
                {"path": f"/experience_application/case_assessments/{item['case_number']-1}/review_ref",
                 "code": "EXPERIENCE_EFFECT_MISSING", "message": f"第{item['case_number']}条采用案例尚未被prediction_adjustments引用。"}
                for item in gap['missing_cases']
            ])
    elif adopted or any(item["action"] == "adjusted" for item in adjustments):
        invalid("EXPERIENCE_STATUS_INVALID", "/experience_application/status", "reviewed_no_change不能同时声称adopt或adjusted；拒绝、未知及保持均可合法表达。")


def _experience_effect_gap(assessments, adopted, related):
    missing = [{'case_number': index+1, 'review_ref': case['review_ref'], 'reason': case['reason']}
               for index, case in enumerate(assessments) if case['review_ref'] in adopted-related]
    numbers = '、'.join(str(item['case_number']) for item in missing)
    return {'version': 'experience-effect-diagnostic/1', 'adopted_count': len(adopted),
            'linked_count': len(adopted & related), 'missing_count': len(missing), 'missing_cases': missing,
            'detail': f"采用了{len(adopted)}条经验，其中{len(missing)}条尚未关联预测影响说明（第{numbers}条）。案例理由中的文字不能代替关联引用；可用保持判断或谨慎提示说明影响。本次未保存为可用预测，原回答已保留。"}


def experience_effect_diagnosis(raw, context):
    """Diagnose only the exact relationship failure; never change admission or saved text."""
    from .json_document import parse_document
    if not isinstance(raw, str):
        return None
    try:
        if len(raw.encode('utf-8')) > 2000000:
            return None
        answer = parse_document(raw)
        assessments = answer['experience_application']['case_assessments']
        adjustments = answer['prediction_adjustments']
        if len(assessments) > 8 or len(adjustments) > 12:
            return None
        try:
            validate_experience_application(answer, context)
        except DomainError as error:
            if error.code != 'EXPERIENCE_EFFECT_MISSING':
                return None
            adopted = {c['review_ref'] for c in assessments if c['decision'] == 'adopt'}
            related = {ref for row in adjustments for ref in row['related_review_refs']}
            gap = _experience_effect_gap(assessments, adopted, related)
            if any(not isinstance(c['reason'], str) or not isinstance(c['review_ref'], str) for c in gap['missing_cases']):
                return None
            return gap
    except (DomainError, KeyError, TypeError, ValueError, RecursionError, UnicodeError):
        return None
    return None
