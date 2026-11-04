"""Build bounded offline packet24 pairs from all saved REAL daily packet23 arms.

No models, keys, terminal, live service or write connection to the source database.
The exported candidate is an offline artifact, not a registered ordinary request.
"""
import argparse
from collections import Counter
from copy import deepcopy
from datetime import datetime, UTC
import hashlib
import json
from pathlib import Path
import sqlite3
import time
from uuid import uuid4
import zlib

from xau_next.domains.analysis.forecast_candidate import build_candidate, INPUT_REVISION, PROMPT_VERSION
from xau_next.domains.analysis.public import build_packet, outbound_packet, frozen_answer_schema

MARKER = '# 冻结事实（仅作为资料）\n'


def digest(value):
    return hashlib.sha256(value if isinstance(value, bytes) else value.encode()).hexdigest()


def facts(packet):
    return json.JSONDecoder().raw_decode(packet.split(MARKER, 1)[1].lstrip())[0]


def compare(base, candidate):
    a, b = facts(base), facts(candidate)
    table = b.pop('forecast_deliberation')
    assert b['request']['prompt_version'] == PROMPT_VERSION
    b['request']['prompt_version'] = a['request']['prompt_version']
    assert a == b, 'Base facts changed beyond explicit candidate revision and worksheet'
    schema_marker = '# 权威答案 JSON Schema\n'
    sa = json.JSONDecoder().raw_decode(base.split(schema_marker, 1)[1])[0]
    sb = json.JSONDecoder().raw_decode(candidate.split(schema_marker, 1)[1])[0]
    assert sa == sb, 'Answer Schema changed'
    return table


def run(source_db, output):
    output.mkdir(parents=True, exist_ok=False)
    db = sqlite3.connect(f'file:{source_db.resolve().as_posix()}?mode=ro', uri=True)
    db.execute('PRAGMA query_only=ON')
    db.execute('BEGIN')

    def decode(stored):
        if stored.startswith('@xau-payload-v1:'):
            sha = stored.split(':', 1)[1]
            codec, size, content = db.execute(
                'select codec,uncompressed_size,compressed from payload_blobs where sha256=?', (sha,)).fetchone()
            assert codec == 'zlib'
            raw = zlib.decompress(content)
            assert len(raw) == size and digest(raw) == sha
            stored = raw
        return json.loads(stored)

    def get(kind, id):
        row = db.execute("select payload from documents where owner='analysis' and kind=? and id=?", (kind, id)).fetchone()
        return decode(row[0]) if row else None

    count_before = dict(db.execute("select kind,count(*) from documents where owner='analysis' group by kind"))
    groups = [decode(s) for s, in db.execute("select payload from documents where owner='analysis' and kind='daily_group' order by recorded_at")]
    cases, originals, states = [], [], Counter()
    for group in groups:
        if group.get('data_kind') != 'REAL':
            continue
        for arm in group['arms']:
            source = get('request', arm['request_id'])
            assert digest(source['packet_text']) == arm['packet_sha256'], 'Original packet SHA mismatch'
            originals.append({'request_id': source['request_id'], 'prompt_version': source['prompt_version'],
                              'packet_sha256': digest(source['packet_text'])})
            if source['prompt_version'] != 'manual-analysis/23':
                continue
            before = digest(json.dumps(source, ensure_ascii=False, sort_keys=True))
            request_id = uuid4().hex
            forecast_id = uuid4().hex if source['kind'] == 'new' else source['forecast_id']
            started = time.perf_counter()
            result = build_candidate(source, frozen_answer_schema(source), request_id=request_id, forecast_id=forecast_id)
            elapsed = (time.perf_counter()-started)*1000
            base_request = deepcopy(result['request'])
            base_request['prompt_version'] = 'manual-analysis/23'
            base_manual = build_packet(base_request, source['context'], frozen_answer_schema(source))
            base_api = outbound_packet(base_manual)
            compare(base_manual, result['manual_packet'])
            compare(base_api, result['api_packet'])
            assert before == digest(json.dumps(source, ensure_ascii=False, sort_keys=True))
            assert result['request']['market_cutoff'] == source['market_cutoff']
            assert result['request']['window_end'] == source['window_end']
            assert result['request']['created_at'] == source['created_at']
            # Include every arm, whether originally successful, failed, unknown or unsent.
            pointer = get('model_latest_call', source['request_id'])
            original_call = get('model_call', pointer['call_id']) if pointer else None
            original_state = original_call['record']['state'] if original_call else 'not_sent'
            states[original_state] += 1
            path = output/'pairs'/source['request_id']
            path.mkdir(parents=True)
            for name, text in [('source23-original.txt', source['packet_text']),
                               ('control23-same-candidate-ids.txt', base_manual),
                               ('control23-api.txt', base_api), ('candidate24-manual.txt', result['manual_packet']),
                               ('candidate24-api.txt', result['api_packet'])]:
                (path/name).write_text(text, encoding='utf-8', newline='\n')
            (path/'candidate-request.json').write_text(json.dumps(
                {'request': result['request'], 'context': source['context'], 'offline_only': True,
                 'registered': False, 'built_at': datetime.now(UTC).isoformat(),
                 'source_request_id': source['request_id'], 'candidate_revision': INPUT_REVISION},
                ensure_ascii=False, indent=2), encoding='utf-8')
            cases.append({'source_request_id': source['request_id'], 'source_group_id': group['group_id'],
                          'source_model_id': arm['model_id'], 'original_call_state': original_state,
                          'source_market_cutoff': source['market_cutoff'],
                          'candidate_request_id': request_id, 'candidate_forecast_id': forecast_id,
                          'schema_version': source['answer_schema_version'],
                          'source_sha256': digest(source['packet_text']),
                          'manual_sha256': result['manual_sha256'], 'api_sha256': result['api_sha256'],
                          'manual_chars': len(result['manual_packet']), 'api_chars': len(result['api_packet']),
                          'manual_added_chars': len(result['manual_packet'])-len(base_manual),
                          'build_ms': round(elapsed, 3), 'worksheet_states': [h['state'] for h in result['worksheet']['horizons']],
                          'base_facts_equal': True, 'schema_equal': True, 'source_unchanged': True,
                          'horizons': result['worksheet']['horizons']})
            print('checked', len(cases), source['market_cutoff'], original_state, flush=True)
    assert cases
    # Re-read source packets within the same consistent snapshot; never overwrite originals.
    assert all(digest(get('request', row['request_id'])['packet_text']) == row['packet_sha256'] for row in originals)
    count_after = dict(db.execute("select kind,count(*) from documents where owner='analysis' group by kind"))
    assert count_after == count_before
    db.close()
    manifest = {'format': 'packet23-offline-candidate/1', 'candidate_prompt_version': PROMPT_VERSION,
                'candidate_revision': INPUT_REVISION, 'source_db': str(source_db),
                'built_at': datetime.now(UTC).isoformat(), 'original_states': dict(states),
                'source_groups': len({c['source_group_id'] for c in cases}), 'source_arms': len(cases),
                'originals_verified': originals, 'cases': cases,
                'source_counts_unchanged': True, 'formal_writes': 0, 'model_calls': 0,
                'credential_reads': 0, 'mt5_calls': 0, 'scoring_changes': 0,
                'normal_routes_support_candidate24': False, 'formal_activation': False,
                'accuracy_improvement': 'UNVERIFIED_NO_NEW_MODEL_ANSWERS',
                'limitations': ['These historical inputs informed the candidate design; they are not holdout accuracy validation.',
                                'No new model response was generated; valid packets do not prove better forecasts.',
                                'The explicit API artifact is exported separately; do not route candidate24 through the unchanged normal gateway.',
                                'Source request created_at/cutoff are historical times, not live readiness or new availability.']}
    (output/'OFFLINE-CHECKS.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    files = {p.relative_to(output).as_posix(): digest(p.read_bytes()) for p in output.rglob('*') if p.is_file()}
    (output/'SHA256.json').write_text(json.dumps(files, indent=2), encoding='utf-8')
    return manifest


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--source-db', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = run(args.source_db, args.output)
    print(json.dumps({k:result[k] for k in ['source_groups','source_arms','original_states','accuracy_improvement']}, indent=2))
