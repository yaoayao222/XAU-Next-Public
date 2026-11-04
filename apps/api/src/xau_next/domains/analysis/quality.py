"""Frozen forecast evidence and separate diagnostics; never trading probabilities."""
from copy import deepcopy
from collections import Counter
from datetime import timedelta
from decimal import Decimal, localcontext

from xau_next.shared.closed_bars import HISTORY_GAPS
from xau_next.shared.values import parse_utc
from .statistics import MINUTES, contiguous, decimal

PREDICTION_POLICY = {
    'version': 'prediction-quality/2', 'direction_semantics': 'endpoint_change',
    'review_method':'prediction_review/3',
    'direction_anchor':'request_frozen_bid',
    'direction_endpoint':'last_unique_bid_at_or_before_original_endpoint_with_frozen_quote_max_age',
    'neutral_band': '0.00', 'legacy_review_method': 'prediction_review/2',
    'range_semantics': 'whole_remaining_window_extrema',
    'phase_semantics': 'local_segment_change', 'turning_semantics': 'closed_bid_m1_two_neighbors',
    'confidence_semantics': 'model_self_assessment_not_probability',
    'quality_dimensions': ['direction','range_boundaries','range_width','phases','turning_points','conditional_path','confidence'],
    'notes': ['neutral表示端点净变化处于冻结中性带，不是震荡形态的同义词；无法判断用uncertain。',
        '覆盖不等于准确：同时记录边界误差、预测宽度及实际宽度；不合并为单一命中率。',
        '局部机会的目标价差与整段预测范围不同；不强制方向、范围宽度或转折。',
        '方向起点为本包冻结Bid，不改用下一根M1开盘；端点不得使用未来报价，缺少合格逐笔资料保持未知。',
        '旧prediction_review/1、/2为完整M1裁剪口径，不等同原冻结Bid方向命中；旧记录与标签保留。',
        '缺资料不补概率或信心；重叠窗口、周期和修订不是独立样本。']}
CONDITION_POLICY = {
    'version': 'condition-observation/1',
    'background': '冻结时已成立且入场时仍须成立；最近合法闭柱可以早于版本可用时间，不锁存、不放宽新鲜度。',
    'new_since_available': '观察时间>=max(available_at,valid_from)；旧闭柱不能补认。',
    'new_since_activation': '观察时间严格晚于激活事件且>=effective_from；同一事件不算后续确认。',
    'activation': '只接受effective_from之后合法新事件并锁存；独立等待回踩，不追价。',
    'entry_checks': '当前最新观察同时成立；每项须有独立purpose，不是任意多阶段历史顺序。',
    'invalidation': '全程背景失效；post_activation_invalidation从激活事件起，失效和重评优先。'}


def experience_cohort(experience):
    cases = (experience or {}).get('cases', [])
    versions, intervals, unknown = set(), [], 0
    for case in cases:
        inputs = case.get('inputs', {})
        version = inputs.get('forecast_version_id')
        if version:
            versions.add(version)
        else:
            unknown += 1
        if case.get('window_start') and case.get('window_end'):
            intervals.append((parse_utc(case['window_start']),parse_utc(case['window_end'])))
    components, end = 0, None
    for left,right in sorted(intervals):
        if end is None or left >= end:
            components += 1
        end = max(end,right) if end else right
    return {'version':'experience-cohort/1','case_count':len(cases),
        'unique_forecast_versions':len(versions), 'cases_without_forecast_identity':unknown,
        'overlap_components':components, 'independent_sample_count':None,
        'notes':['修订、多个周期和重叠窗口保留，但不作为独立行情实验。',
                 '完整外发、模型登记采用、原因解释正确及预测提高是四种不同证据。']}


def _quantiles(values):
    values = sorted(values)
    if len(values) < 20:
        return None
    result = {}
    for name, ratio in [('p10',Decimal('.1')),('p50',Decimal('.5')),('p90',Decimal('.9'))]:
        position = Decimal(len(values)-1)*ratio
        low = int(position)
        high = min(low+1,len(values)-1)
        result[name] = decimal(values[low]+(values[high]-values[low])*(position-low))
    return result


def _source_windows(source, minutes, cutoff):
    timeframe = source['timeframe']
    size = minutes // MINUTES[timeframe]
    bars = sorted([b for b in source.get('bars',[]) if b['closed'] and parse_utc(b['close_at']) <= cutoff],
                  key=lambda b:parse_utc(b['open_at']))
    samples, skipped = [], 0
    starts = Counter(b['open_at'] for b in bars)
    ends = Counter(b['close_at'] for b in bars)
    for end in range(len(bars),size-1,-size):
        window = bars[end-size:end]
        if (not contiguous(window,MINUTES[timeframe]) or
                any(starts[b['open_at']] != 1 or ends[b['close_at']] != 1 for b in window)):
            skipped += 1
            continue
        try:
            valid = all(0 < Decimal(b['low']) <= min(Decimal(b['open']),Decimal(b['close']))
                <= max(Decimal(b['open']),Decimal(b['close'])) <= Decimal(b['high']) for b in window)
            if not valid:
                skipped += 1
                continue
            anchor = Decimal(window[0]['open'])
            low,high = min(Decimal(b['low']) for b in window),max(Decimal(b['high']) for b in window)
            samples.append((Decimal(window[-1]['close'])-anchor,low-anchor,high-anchor,high-low))
        except (ValueError, ArithmeticError):
            skipped += 1
    return bars,samples,skipped


def forecast_evidence(market, horizons, *, window_start=None):
    """Non-overlapping retrospective descriptions from a single eligible source per horizon."""
    with localcontext() as ctx:
        ctx.prec = 64
        cutoff = parse_utc(market['market_cutoff'])
        sources = []
        for source in market.get('series',[]):
            status = source.get('status',{})
            gap_only = status.get('state') == 'incomplete' and status.get('reasons') == [HISTORY_GAPS]
            if (source.get('timeframe') not in MINUTES or source.get('price_basis') != 'bid'
                or sum(s.get('timeframe') == source['timeframe'] for s in market['series']) != 1
                or (status.get('state') != 'available' and not gap_only)
                or not status.get('captured_at') or parse_utc(status['captured_at']) > cutoff):
                continue
            sources.append(source)
        output = []
        for horizon in sorted(set(horizons)):
            end = parse_utc(window_start or market['market_cutoff']) + timedelta(minutes=horizon)
            remaining = max(0,int((end-cutoff).total_seconds()//60))
            row = {'horizon_minutes':horizon,'remaining_minutes':remaining,'state':'missing',
                   'sample_count':0,'quantiles':None,'baseline':None}
            choices = []
            for source in sources:
                if remaining < 1 or remaining % MINUTES[source['timeframe']]:
                    continue
                bars,samples,skipped = _source_windows(source,remaining,cutoff)
                choices.append((source,bars,samples,skipped))
            # Prefer measured complete coverage, then the finest closed source; never splice.
            choices.sort(key=lambda x:(len(x[2]),-MINUTES[x[0]['timeframe']]),reverse=True)
            if choices:
                source,bars,samples,skipped = choices[0]
                row.update(state='available' if len(samples)>=20 else 'insufficient_samples',
                    timeframe=source['timeframe'], price_basis='bid', sample_count=len(samples),
                    skipped_incomplete_windows=skipped,
                    start_at=bars[0]['open_at'] if bars else None, end_at=bars[-1]['close_at'] if bars else None,
                    quantiles={name:_quantiles([s[i] for s in samples]) for i,name in enumerate(
                        ['endpoint_delta','low_excursion','high_excursion','path_width'])} if len(samples)>=20 else None)
                if bars:
                    lookback = bars[-max(2,remaining//MINUTES[source['timeframe']]):]
                    period = MINUTES[source['timeframe']]
                    if (contiguous(lookback,period) and
                        0 <= (cutoff-parse_utc(bars[-1]['close_at'])).total_seconds() <= period*60):
                        anchor = Decimal(bars[-1]['close'])
                        drift = (anchor-Decimal(lookback[0]['open']))*remaining/(len(lookback)*period)
                        row['baseline'] = {'anchor_at':bars[-1]['close_at'],'anchor_price':decimal(anchor),
                            'no_change':decimal(anchor),'fixed_drift':decimal(anchor+drift),
                            'lookback_minutes':len(lookback)*period}
            output.append(row)
        return {'version':'forecast-evidence/1','market_snapshot_id':market['snapshot_id'],
            'market_cutoff':market['market_cutoff'],'horizons':output,
            'notes':['同源非重叠历史窗口；不同周期的窗口仍重叠，不是独立当前预测。',
                     '分位数是历史描述；p10至p90不表示当前80%概率区间，不强制作为预测价位。',
                     '基准为冻结闭柱，不使用未来走势；主判断须结合当前结构及反对证据。']}


def range_diagnostics(predicted, actual_low, actual_high):
    if actual_low is None or actual_high is None:
        return {'state':'unknown','contained':None,'boundary_mae':None,'width_ratio':None}
    pl,ph,al,ah = map(Decimal,(predicted['low'],predicted['high'],actual_low,actual_high))
    width = ah-al
    return {'state':'available','contained':pl<=al and ph>=ah,
        'predicted_width':float(ph-pl),'actual_width':float(width),
        'boundary_mae':float((abs(pl-al)+abs(ph-ah))/2),
        'width_ratio':float((ph-pl)/width) if width>0 else None}


def new_quality_context(market, horizons, experience, window_start):
    return {'prediction_policy':deepcopy(PREDICTION_POLICY),'condition_policy':deepcopy(CONDITION_POLICY),
        'forecast_evidence':forecast_evidence(market,horizons,window_start=window_start),
        'experience_cohort':experience_cohort(experience)}
