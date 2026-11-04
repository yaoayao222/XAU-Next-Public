"""Bounded diagnostics only: never return a repaired JSON value for admission."""
import json
import math

VERSION = 'json-duplicates/1'
LABELS = {'analysis_basis': '分析依据', 'horizons': '预测周期', 'plans': '候选方案',
          'experience_application': '经验使用说明', 'prediction_adjustments': '预测调整',
          'candidate_assessments': '候选评估', 'reassessment_conditions': '重评条件'}


class _Limit(ValueError):
    pass


def duplicate_fields(raw):
    """Locate duplicate decoded keys in each object, without selecting a value.

    Valid JSON grammar is a prerequisite. Syntax, depth/size limits and failures
    return no diagnostic; callers must still reject through the strict parser.
    Object order and escape spelling do not affect diagnostic value equality.
    A repeated key or nonfinite value inside either value makes equality unknown.
    """
    if not isinstance(raw, str) or len(raw) > 2_000_000:
        return None
    decoder = json.JSONDecoder()
    try:
        json.loads(raw)
        found = []; total = 0; nodes = 0

        def ws(i):
            while i < len(raw) and raw[i].isspace():
                i += 1
            return i

        def location(i):
            return {'line': raw.count('\n', 0, i) + 1,
                    'column': i - raw.rfind('\n', 0, i), 'character': i + 1}

        def scan(i, path, depth):
            nonlocal total, nodes
            nodes += 1
            if depth > 128 or nodes > 100_000:
                raise _Limit()
            i = ws(i); token = raw[i]
            if token == '{':
                i = ws(i + 1); seen = {}; members = []; unambiguous = True
                if raw[i] == '}':
                    return ('object', ()), True, i + 1
                while True:
                    key_at = i; key, i = decoder.raw_decode(raw, i)
                    # Do not put lone Unicode surrogates from an invalid key
                    # into a UTF-8 HTTP error. Fall back to strict rejection.
                    key.encode('utf-8')
                    i = ws(i); assert raw[i] == ':'
                    pointer = path + '/' + key.replace('~', '~0').replace('/', '~1')
                    value, clean, i = scan(i + 1, pointer, depth + 1)
                    if key in seen:
                        total += 1; first_at, prior, prior_clean = seen[key]
                        relation = ('identical' if value == prior else 'conflicting') if clean and prior_clean else 'unknown'
                        row = {'path': pointer[:1024], 'field': key[:128],
                               'label': LABELS.get(key, '字段“' + key[:128] + '”'),
                               'first': location(first_at), 'duplicate': location(key_at),
                               'values_relation': relation}
                        found.append(row)
                        found.sort(key=lambda r: r['duplicate']['character'])
                        del found[8:]
                        unambiguous = False
                    else:
                        seen[key] = (key_at, value, clean)
                    members.append((key, value)); unambiguous = unambiguous and clean
                    i = ws(i)
                    if raw[i] == '}':
                        # Values never leave this diagnostic parser.
                        return ('object', tuple(sorted(members, key=lambda m: m[0]))), unambiguous, i + 1
                    assert raw[i] == ','; i = ws(i + 1)
            if token == '[':
                i = ws(i + 1); values = []; clean = True
                if raw[i] == ']':
                    return ('array', ()), True, i + 1
                while True:
                    value, child_clean, i = scan(i, path + '/' + str(len(values)), depth + 1)
                    values.append(value); clean = clean and child_clean; i = ws(i)
                    if raw[i] == ']':
                        return ('array', tuple(values)), clean, i + 1
                    assert raw[i] == ','; i = ws(i + 1)
            value, end = decoder.raw_decode(raw, i)
            clean = not isinstance(value, float) or math.isfinite(value)
            return (type(value).__name__, value), clean, end

        _, _, end = scan(0, '', 0)
        assert ws(end) == len(raw)
        return {'version': VERSION, 'total': total, 'items': found, 'truncated': total > len(found)} if total else None
    except (ValueError, TypeError, RecursionError, IndexError, AssertionError):
        return None
