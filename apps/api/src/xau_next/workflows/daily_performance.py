"""Read-only aggregation over immutable versions and existing review revisions."""
from collections import defaultdict
from datetime import timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo
import json
import hashlib
from xau_next.domains.analysis.public import market_scope_key
from xau_next.shared.errors import DomainError
from xau_next.shared.values import parse_utc


DISPLAY_STATES = ('correct', 'incorrect', 'uncertain', 'pending', 'unknown',
                  'failed', 'cancelled', 'call_unknown', 'not_sent', 'processing')


def display_state(version, call, direction, ended, valid, value):
    """Presentation partition only. Never changes a saved metric or legacy counter."""
    if version is None:
        if call is None:
            return 'not_sent'
        state = call.get('state')
        if state in {'failed', 'cancelled'}:
            return state
        if state == 'outcome_unknown' or state not in {'queued', 'running', 'cancelling'}:
            return 'call_unknown'
        return 'processing'
    if not ended:
        return 'pending'
    if direction == 'uncertain':
        return 'uncertain'
    if valid:
        return 'correct' if value == '1' else 'incorrect'
    return 'unknown'


def history_page(items, filters):
    """Page grouped identities after full-scope filtering, not a page-only score."""
    issue_states = {'failed', 'cancelled', 'call_unknown', 'unknown'}
    issues = sum(item['state'] in issue_states for item in items)
    groups = {}
    for item in items:
        if filters.get('history_view') == 'issues' and item['state'] not in issue_states:
            continue
        if filters.get('history_status') and item['state'] != filters['history_status']:
            continue
        if filters.get('history_row') and item['row_id'] != filters['history_row']:
            continue
        key = item['group_id'] or ('legacy-' + item['request_id'])
        if key not in groups:
            groups[key] = {'round_key': key, 'group_id': item['group_id'],
                           'market_cutoff': item['market_cutoff'], 'template_version': item['template_version'],
                           'kind': item['kind'], 'same_question': item['same_question'], 'items': []}
        groups[key]['items'].append(item)
    for group in groups.values():
        group['state_counts']={state:sum(item['state']==state for item in group['items']) for state in DISPLAY_STATES}
        group['saved_items']=sum(item['version_id'] is not None for item in group['items'])
    ordered = sorted(groups.values(), key=lambda g: (g['market_cutoff'], g['round_key']), reverse=True)
    start = 0
    if filters.get('history_cursor'):
        previous = next((i for i, g in enumerate(ordered) if g['round_key'] == filters['history_cursor']), None)
        if previous is None:
            raise DomainError('HISTORY_CURSOR_INVALID', '记录范围已变化，请回到第一页查看。', 422)
        start = previous + 1
    limit = filters.get('history_limit') or 20
    page = ordered[start:start + limit]
    return {'items': page, 'next_cursor': page[-1]['round_key'] if start + limit < len(ordered) else None,
            'total_rounds': len({item['group_id'] or ('legacy-' + item['request_id']) for item in items}),
            'matching_rounds': len(ordered), 'evaluation_items': len(items),
            'answer_versions': len({item['version_id'] for item in items if item['version_id']}),
            'saved_requests': len({item['request_id'] for item in items if item['version_id']}),
            'issue_records': issues}


def performance(w,filters):
    with w.db.read() as scope:
        records=w.daily.store.performance_records(scope=scope)
        review_index={};cursor=None
        while True:
            page=w.review.list_reviews(target_kind='forecast',revisions='latest',limit=100,cursor=cursor,scope=scope).to_dict()
            for review in page['items']:
                inp=review.get('inputs') or {}
                if inp.get('evaluation_scope')=='version_descriptive':
                    review_index[(inp['forecast_version_id'],inp['horizon_minutes'],review['method_version'])]=review
            cursor=page['next_cursor']
            if not cursor:break
        source_filter=filters.get('source_scope')
        latest=w.market.latest(scope=scope)
        if not source_filter and latest:source_filter=market_scope_key(latest)
        now=parse_utc(w.clock());rows={};seen_records=set();legacy=0;history_items=[];seen_items=set()
        for record in records:
            q,v,g,call=record['request'],record['version'],record['group'],record['call']
            if not q:continue
            market=q['context']['market'];kind='SYNTHETIC' if market['source_kind']=='synthetic' else 'REAL'
            # Identity uses only these three fields. Avoid copying historic bars
            # and payload contents merely to hash an account/symbol/source key.
            source=market_scope_key({key:market[key] for key in ('account_scope_id','symbol','source_kind')});model=record['model_id'];template=q.get('prompt_version','unknown')
            day=parse_utc(q['market_cutoff']).astimezone(ZoneInfo('Asia/Shanghai')).date().isoformat()
            if kind!=filters.get('data_kind','REAL') or source_filter and source!=source_filter:continue
            if filters.get('model') and model!=filters['model']:continue
            if filters.get('template') and template!=filters['template']:continue
            if filters.get('from_date') and day<filters['from_date'] or filters.get('to_date') and day>filters['to_date']:continue
            if filters.get('paired_only') and not (g and g['same_question'] and len(g['models'])>=2):continue
            if filters.get('minutes') and filters['minutes'] not in q['horizon_minutes']:continue
            origin=v['origin'] if v else {}
            configuration=(call or {}).get('_performance_configuration')
            # A prepared branch has a selected channel, but no dispatched values.
            # Keep its identity separate without inventing provider evidence.
            if not configuration and g and v is None:
                arm=next((a for a in g['arms'] if a['model_key']==record['arm']),{})
                if arm.get('profile_id'):
                    configuration={'profile_id':arm['profile_id'],'dispatch_state':'not_sent','selected_provider':arm.get('selected_provider')}
            if configuration:
                configuration={**configuration,**{key:(call or {}).get(key) for key in ['outbound_transform_version','answer_schema_version']}}
                config=json.dumps(configuration,ensure_ascii=False,sort_keys=True,separators=(',',':'))
            else:
                config='未记录完整配置 / '+' / '.join(str((call or {}).get(k) or origin.get(k) or 'unknown') for k in ['provider','protocol','reasoning_effort'])
            channel={key:(configuration or {}).get(key) for key in ['profile_id','provider','base_url','protocol','reasoning_effort','max_output_tokens','selected_provider']}
            if not configuration:
                channel.update(provider=(call or {}).get('provider') or origin.get('provider'),protocol=(call or {}).get('protocol') or origin.get('protocol'))
            record_selected=False
            for minutes in q['horizon_minutes']:
                if filters.get('minutes') and minutes!=filters['minutes']:continue
                frozen_method=q['context'].get('prediction_policy',{}).get('review_method')
                review=review_index.get((v['version_id'],minutes,frozen_method)) if v else None
                if v and not frozen_method:
                    matching=[r for (vid,m,_),r in review_index.items() if vid==v['version_id'] and m==minutes]
                    review=max(matching,key=lambda r:(r['available_at'],r['method_version'])) if matching else None
                method=review['method_version'] if review else q['context'].get('prediction_policy',{}).get('review_method','prediction_review/2')
                if filters.get('provider') and channel.get('provider') != filters['provider']:continue
                if filters.get('method') and method != filters['method']:continue
                seen_records.add(q['request_id'])
                if not record_selected:legacy+=int(g is None);record_selected=True
                key=(model,template,minutes,source,kind,method,config)
                if key not in rows:
                    rows[key]={**dict(zip(['model_id','template_version','minutes','source_scope','data_kind','method_version','configuration'],key)),
                      'evaluation_scope':'version_descriptive',**channel,**{k:0 for k in ['records','ended','clear','scored','correct','incorrect','uncertain','unknown','failed','not_sent','pending','range_scored','range_covered','non_overlapping_records','overlapping_records']},
                      'row_id':hashlib.sha256(json.dumps(key,ensure_ascii=False).encode()).hexdigest()[:24],
                      'display_counts':{state:0 for state in DISPLAY_STATES},
                      '_errors':[],'_widths':[],'_intervals':[],
                      'note':'整窗描述性评价包含生成等待；同批资料不等于相同可用时长。按来源、方法和已记录配置分组；未独立认证底层模型身份。'}
                row=rows[key];row['records']+=1
                end=parse_utc(q['window_start'])+timedelta(minutes=minutes)
                ended=end<=now;row['ended']+=int(ended);row['pending']+=int(not ended)
                horizon=next((h for h in v['answer']['horizons'] if h['horizon_minutes']==minutes),None) if v else None
                direction=horizon['direction'] if horizon else None
                row['clear']+=int(direction is not None and direction!='uncertain')
                row['uncertain']+=int(direction=='uncertain')
                row['not_sent']+=int(v is None and not call)
                row['failed']+=int(v is None and call is not None and call['state'] in {'failed','cancelled','outcome_unknown'})
                metrics={m['name']:m for m in (review or {}).get('metrics',[]) if m.get('index') is None}
                score=metrics.get('direction_correct',{})
                valid=score.get('state')=='available' and score.get('value') in {'0','1'}
                state=display_state(v,call,direction,ended,valid,score.get('value'))
                row['display_counts'][state]+=1
                row['scored']+=int(valid);row['correct']+=int(valid and score['value']=='1');row['incorrect']+=int(valid and score['value']=='0')
                row['unknown']+=int(ended and not valid and direction!='uncertain')
                error=metrics.get('range_boundary_absolute_error',{});coverage=metrics.get('range_contained',{})
                if error.get('state')=='available' and error.get('value') is not None:
                    row['range_scored']+=1;row['_errors'].append(Decimal(error['value'])/2)
                    row['range_covered']+=int(coverage.get('state')=='available' and coverage.get('value')=='1')
                if horizon:
                    row['_widths'].append(Decimal(horizon['expected_range']['high'])-Decimal(horizon['expected_range']['low']))
                    row['_intervals'].append((parse_utc(q['market_cutoff']),end))
                if filters.get('include_history'):
                    identity=(q['request_id'],v['version_id'] if v else None,minutes,method)
                    if identity in seen_items:continue
                    seen_items.add(identity)
                    metric_value=lambda name:metrics.get(name,{}).get('value') if metrics.get(name,{}).get('state')=='available' else None
                    history_items.append({'item_id':hashlib.sha256(json.dumps(identity).encode()).hexdigest()[:24],
                      'row_id':row['row_id'],'request_id':q['request_id'],'forecast_id':q['forecast_id'],
                      'version_id':v['version_id'] if v else None,'group_id':g['group_id'] if g else None,
                      'kind':g['kind'] if g else 'legacy','same_question':bool(g and g['same_question']),
                      'market_cutoff':q['market_cutoff'],'available_at':v['available_at'] if v else None,
                      'end_at':end.isoformat().replace('+00:00','Z'),'model_id':model,'template_version':template,
                      'minutes':minutes,'provider':channel.get('provider'),'method_version':method,'state':state,
                      'direction':direction,'predicted_low':horizon['expected_range']['low'] if horizon else None,
                      'predicted_high':horizon['expected_range']['high'] if horizon else None,
                      'actual_low':metric_value('actual_low'),'actual_high':metric_value('actual_high'),
                      'range_error':format(Decimal(error['value'])/2,'f') if error.get('state')=='available' and error.get('value') is not None else None,
                      'review_id':review['review_id'] if review else None,'review_revision':review['revision'] if review else None,
                      'review_outcome':review['outcome'] if review else None,'review_reasons':list((review or {}).get('reasons',[])),
                      'call_id':(call or {}).get('call_id'),'error_code':(call or {}).get('error_code'),
                      'error_detail':(call or {}).get('detail'),'call_state':(call or {}).get('state')})
        for row in rows.values():
            previous_end=None
            for start,end in sorted(row.pop('_intervals')):
                overlap=previous_end is not None and start<previous_end
                row['overlapping_records' if overlap else 'non_overlapping_records']+=1
                previous_end=max(previous_end,end) if previous_end else end
            errors=row.pop('_errors');widths=row.pop('_widths')
            row['range_mae']=format(sum(errors)/len(errors),'f') if errors else None
            row['range_width']=format(sum(widths)/len(widths),'f') if widths else None
        result={'rows':sorted(rows.values(),key=lambda r:(r['model_id'],r['template_version'],r['minutes'],r['method_version'],r['configuration'])),
          'as_of':w.clock(),'records_considered':len(seen_records),'legacy_records_included':legacy,
          'message':'统一日常成绩；显示分母、失败和未知，不自动排名。不同日期成绩为使用观察，同时同题记录为更直接对照；重叠窗口不是独立样本。'}
        if filters.get('include_history'):result['history']=history_page(history_items,filters)
        return result
