"""Explicit derivative of packet23, with no model I/O or storage ownership.

The existing 23 builder/projector remains the base authority. Only the candidate
revision and a verified descriptive worksheet are added after each projection.
Ordinary runtime entry points use the same wrapper as the archived offline pair.
"""
from copy import deepcopy
from decimal import Decimal, localcontext
import hashlib
import json

from xau_next.shared.errors import DomainError
from .forecast_reference import build_forecast_reference
from .package import build_packet
from .model_api import outbound_packet

BASE_PROMPT = 'manual-analysis/23'
PROMPT_VERSION = 'manual-analysis/24'
INPUT_REVISION = 'forecast-deliberation/1'
MARKER = '# 冻结事实（仅作为资料）\n'

INSTRUCTIONS = (
    '\n# 23版派生候选24：逐周期取舍与区间位置检查\n'
    'forecast_deliberation是同份截止前资料的算术对照表，不提供方向、预测价格或概率。'
    '先看每行原end_at与remaining_seconds；30分钟、60分钟和180分钟是不同问题，'
    '不能把某一周期的支持证据或一次条件突破直接复制成所有周期的结论。\n'
    '在每个forecast_basis.direction_basis中简短说明：本周期最有力的当前证据、'
    '最强反证，以及为何前者仍足以支持原端点的净变化判断。'
    '如果认为最近几根涨跌会延续，指出截止时已经存在的延续依据；'
    '如果认为只是暂时反弹或回落，指出已有的拒绝、承接或较大尺度约束。'
    '尚未发生的突破/收复只能写成未来条件，不能冒充当前证据。'
    '证据不足允许uncertain，既不强迫给方向，也不为躲避错误全部弃判；'
    '不规定必须顺大周期、逆短线或等某根K线确认。\n'
    '独立检查expected_range的位置，再检查宽度：上下边界分别距离当次Bid多少，'
    '哪一侧更容易被突破，支持这种不对称位置的冻结事实是什么。'
    'references中的历史中位极值与外侧极值仅供对照；比较本次上下边界与其差异，'
    '偏离时说明当前结构或波动依据，不机械复制中位值、外侧值或旧支撑阻力。'
    'midpoint_offset_from_bid只描述历史参照中心偏移，不表示未来方向。'
    '历史中位区间可能漏行情，外侧区间也不是当前置信区间；'
    '不得为了保覆盖一律放宽，也不得因看多/看空就裁掉相反侧波动。'
    '关键价位若在所报区间外，不得写成已被包含。\n'
    '当短时与较长周期证据冲突，在原supporting_evidence和opposing_evidence中'
    '各保留最关键的具体事实，并让direction_basis说明最终取舍，避免只是并列复述。'
    '历史经验只采用原冻结experience中的案例，不添加后来发生的本轮结果。'
    '某案例正确或错误不构成永久规则；没有可用参照的行保留具体缺口。\n'
    '只使用既有forecast_basis、analysis_basis、summary和limitations等字段给出简短依据，'
    '不新增回答字段、不要求长篇推演。完整答案Schema、原端点、经验引用、'
    '来源及时间检查、条件方案、费用与人工交易约束全部保持。\n'
)


def _geometry(name, reference, bid):
    low, high = Decimal(reference['low']), Decimal(reference['high'])
    return {'name': name, 'low': str(low), 'high': str(high),
            'width': str(high-low), 'down_from_bid': str(bid-low),
            'up_from_bid': str(high-bid), 'midpoint_offset_from_bid': str((low+high)/2-bid)}


def build_deliberation(request, context):
    """Verify raw frozen evidence through the existing cutoff-safe reference module."""
    with localcontext() as precision:
        precision.prec = 64
        source = build_forecast_reference(request, context)
        result = {'version': INPUT_REVISION, 'base_prompt_version': BASE_PROMPT,
                  'market_snapshot_id': source['snapshot_id'], 'market_cutoff': source['market_cutoff'],
                  'price_unit': source['price_unit'], 'role': 'descriptive_worksheet_not_prediction',
                  'quote': deepcopy(source['quote']), 'horizons': [],
                  'evidence_paths': {'recent_price_relations': 'forecast_review_input.recent_price_relations',
                                    'historical_distributions': 'forecast_evidence.horizons'},
                  'limitations': [
                      '历史极值参照、中心位置和两侧距离不是本次预测、胜率或概率区间。',
                      '同一段重叠行情及同题多个模型不构成独立样本，不从失败案例推出反向规则。',
                      '资料不足、陈旧、未来时点或统计无法核对时保留未知；不补价格或更换截止。']}
        for h in source['horizons']:
            row = {k: deepcopy(h[k]) for k in ['horizon_minutes', 'end_at', 'remaining_seconds', 'state', 'reason']}
            row['history'] = deepcopy(h['history'])
            row['references'] = []
            if h['state'] == 'available':
                bid = Decimal(source['quote']['bid'])
                row['references'] = [_geometry('historical_median_extrema', h['median_extrema_reference'], bid),
                                     _geometry('historical_outer_extrema', h['outer_extrema_reference'], bid)]
            result['horizons'].append(row)
        return result


def _render(base, worksheet):
    prefix, tail = base.split(MARKER, 1)
    tail = tail.lstrip()
    facts, end = json.JSONDecoder().raw_decode(tail)
    facts['request']['prompt_version'] = PROMPT_VERSION
    facts['forecast_deliberation'] = deepcopy(worksheet)
    text = (prefix.replace(BASE_PROMPT, PROMPT_VERSION) + INSTRUCTIONS + MARKER +
            json.dumps(facts, ensure_ascii=False, separators=(',', ':')) + tail[end:])
    if len(text) > 250000:
        raise DomainError('PACKET_TOO_LARGE',
            f'候选24完整资料为{len(text):,}字符，超过250,000字符；未截断或发送。', 422)
    return text


def build_runtime_packet(request, context, schema):
    base_request = deepcopy(request)
    base_request['prompt_version'] = BASE_PROMPT
    return _render(build_packet(base_request, context, schema), build_deliberation(request, context))


def project_deliberation(value):
    """Explicit nested allowlist; future/private extensions are never externalized."""
    def pick(item, names):
        return {k: deepcopy(item[k]) for k in names.split() if k in item}
    if value.get('version') != INPUT_REVISION:
        raise DomainError('CANDIDATE_REVISION_INVALID', '候选24冻结工作表修订无法识别，已停止外发。', 409)
    result = pick(value, 'version base_prompt_version market_snapshot_id market_cutoff price_unit role evidence_paths limitations')
    result['quote'] = pick(value.get('quote') or {}, 'state bid event_time reason')
    result['horizons'] = []
    for h in value['horizons']:
        row = pick(h, 'horizon_minutes end_at remaining_seconds state reason')
        history = h.get('history')
        row['history'] = pick(history, 'timeframe sample_count remaining_minutes start_at end_at') if history else None
        if history:
            for k in ['endpoint_delta', 'path_width']:
                row['history'][k] = pick(history[k], 'p10 p50 p90')
        row['references'] = [pick(ref, 'name low high width down_from_bid up_from_bid midpoint_offset_from_bid')
                             for ref in h['references']]
        result['horizons'].append(row)
    # evidence_paths is also a fixed protocol, never arbitrary caller text.
    result['evidence_paths'] = {'recent_price_relations': 'forecast_review_input.recent_price_relations',
                                'historical_distributions': 'forecast_evidence.horizons'}
    return result


def outbound_runtime_packet(packet):
    prefix, tail = packet.split(MARKER, 1)
    tail = tail.lstrip()
    facts, end = json.JSONDecoder().raw_decode(tail)
    if prefix.count(INSTRUCTIONS) != 1:
        raise DomainError('CANDIDATE_PACKET_INVALID', '候选24原指导缺失或重复，已保留原包并停止外发。', 409)
    worksheet = project_deliberation(facts.pop('forecast_deliberation'))
    facts['request']['prompt_version'] = BASE_PROMPT
    base = (prefix.replace(INSTRUCTIONS, '', 1).replace(PROMPT_VERSION, BASE_PROMPT) + MARKER +
            json.dumps(facts, ensure_ascii=False, separators=(',', ':')) + tail[end:])
    return _render(outbound_packet(base), worksheet)


def build_candidate(source, schema, *, request_id, forecast_id):
    """Build manual/API artifacts in memory, with separate candidate identities.

    No database, model adapter, credential adapter or clock is used here. Source
    created_at and window fields remain historical evidence, not a new live clock.
    """
    if source.get('prompt_version') != BASE_PROMPT:
        raise DomainError('CANDIDATE_BASE_REQUIRED', '本候选只从原23版派生，不能静默升级14版或其他版本。', 409)
    if request_id == source['request_id'] or (source['kind'] == 'new' and forecast_id == source['forecast_id']):
        raise DomainError('CANDIDATE_IDENTITY_REQUIRED', '候选必须分配独立请求及新建预测身份，不能重标原记录。', 409)
    if source['kind'] == 'update' and forecast_id != source['forecast_id']:
        raise DomainError('CANDIDATE_UPDATE_IDENTITY', '更新候选保留原预测身份和端点，只分配独立请求身份。', 409)
    # DocumentStore metadata is not part of the frozen request. Preserve every
    # actual request field; do not accidentally drop future base extensions.
    request = {k: deepcopy(v) for k,v in source.items()
               if k not in {'context', 'packet_text', 'id', 'revision', 'recorded_at'}}
    request.update(request_id=request_id, forecast_id=forecast_id)
    worksheet = build_deliberation(request, source['context'])
    base = build_packet(request, source['context'], schema)
    api_base = outbound_packet(base)
    manual, api = _render(base, worksheet), _render(api_base, worksheet)
    request['prompt_version'] = PROMPT_VERSION
    return {'candidate_revision': INPUT_REVISION, 'base_prompt_version': BASE_PROMPT,
            'request': request, 'manual_packet': manual, 'api_packet': api,
            'manual_sha256': hashlib.sha256(manual.encode()).hexdigest(),
            'api_sha256': hashlib.sha256(api.encode()).hexdigest(),
            'worksheet': worksheet, 'registered': False, 'model_calls': 0}
