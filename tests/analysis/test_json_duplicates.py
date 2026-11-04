"""Duplicate diagnostics never repair or change strict document admission."""
import json
import pytest
from xau_next.domains.analysis.json_duplicates import duplicate_fields
from xau_next.domains.analysis.json_document import document_diagnosis, document_error, parse_document
from xau_next.shared.errors import DomainError


def test_same_root_block_names_positions_and_no_answer_values_exposed():
    raw = '{\n  "analysis_basis":{"main_thesis":"private-value"},\n  "analysis_basis":{"main_thesis":"private-value"}\n}'
    diagnosis = document_diagnosis(raw);value = diagnosis['duplicates'];row = value['items'][0]
    assert value['total'] == 1 and not value['truncated']
    assert row['path'] == '/analysis_basis' and row['label'] == '分析依据'
    assert row['first'] == {'line': 2, 'column': 3, 'character': raw.index('"analysis_basis"') + 1}
    assert row['duplicate']['line'] == 3 and row['values_relation'] == 'identical'
    assert '第2行第3列' in diagnosis['detail'] and '第3行第3列' in diagnosis['detail']
    assert 'private-value' not in json.dumps(diagnosis)
    with pytest.raises(DomainError):parse_document(raw)


@pytest.mark.parametrize('first,second,relation', [
    ('1', '2', 'conflicting'), ('true', '1', 'conflicting'),
    ('{"a":1,"b":2}', '{"b":2,"a":1}', 'identical'),
    ('"中文"', '"\\u4e2d\\u6587"', 'identical'),
    ('[1,2]', '[2,1]', 'conflicting'),
    ('{"a":1,"a":1}', '{"a":1}', 'unknown'),
    ('NaN', 'NaN', 'unknown'),
])
def test_equal_conflicting_or_ambiguous_values_remain_rejected(first, second, relation):
    raw = '{"x":' + first + ',"x":' + second + '}'
    value = duplicate_fields(raw)
    assert next(r for r in value['items'] if r['path'] == '/x')['values_relation'] == relation
    with pytest.raises(DomainError):parse_document(raw)


def test_scopes_escaped_keys_pointer_and_crlf_positions():
    raw = '{\r\n"中":"文", "a/b":[{"t~x":1,"\\u0074~x":2}],"other":{"t~x":3}}'
    row = duplicate_fields(raw)['items'][0]
    assert row['field'] == 't~x' and row['path'] == '/a~1b/0/t~0x'
    at = raw.index('"\\u0074~x"');assert row['duplicate']['character'] == at + 1
    assert row['duplicate']['line'] == 2 and row['duplicate']['column'] == at - raw.rfind('\n', 0, at)
    valid = '{"horizons":[{"direction":"long"},{"direction":"short"}],"text":"\\\"direction\\\": [] {}"}'
    assert duplicate_fields(valid) is None and parse_document(valid)['horizons'][1]['direction'] == 'short'


def test_multiple_occurrences_point_to_first_and_summary_is_bounded():
    raw = '{' + ','.join(f'"f{i}":1,"f{i}":2' for i in range(12)) + '}'
    value = duplicate_fields(raw)
    assert value['total'] == 12 and len(value['items']) == 8 and value['truncated']
    rows = duplicate_fields('{"x":1,"x":1,"x":2}')['items']
    assert len(rows) == 2 and rows[0]['first'] == rows[1]['first']
    assert [r['values_relation'] for r in rows] == ['identical', 'conflicting']


@pytest.mark.parametrize('raw', ['{"x":1,"x":2}}', '{}{}', '```json\n{}\n```', '{"x":', '[1,]'])
def test_invalid_grammar_not_mislabelled_as_duplicate(raw):
    assert duplicate_fields(raw) is None
    assert '重复字段' not in document_error(raw)


def test_limits_fall_back_to_strict_rejection_not_silent_acceptance():
    for raw in ['{"data":"' + 'x' * 2_000_001 + '","x":1,"x":2}', '{"a":' * 130 + '{"x":1,"x":2}' + '}' * 130]:
        assert duplicate_fields(raw) is None
        with pytest.raises(DomainError) as exc:parse_document(raw)
        assert '重复字段' in exc.value.detail


def test_strict_finite_scalar_and_valid_document_behavior_kept():
    for raw in ['{"x":NaN}', '{"x":Infinity}', '{"x":-Infinity}']:
        with pytest.raises(DomainError):parse_document(raw)
    assert document_diagnosis('{"ok":1}') == {'detail': None, 'duplicates': None}


def test_invalid_unicode_key_does_not_break_error_serialization():
    raw = r'{"\ud800":1,"\ud800":2}'
    assert duplicate_fields(raw) is None
    error = document_error(raw)
    assert '重复字段' in error and error.encode('utf-8')
