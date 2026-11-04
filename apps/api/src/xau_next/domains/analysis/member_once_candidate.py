"""Opt-in27: schema-derived write-once plan; unmodified26 facts and checks."""
from copy import deepcopy
import json
from xau_next.shared.errors import DomainError

BASE_PROMPT = 'manual-analysis/26'
PROMPT_VERSION = 'manual-analysis/27'
VERSION = 'json-member-once/1'
MARKER = '# 冻结事实（仅作为资料）\n'
INSTRUCTIONS = (
    '\n# 候选27：根字段只写一次的完成顺序\n'
    '在26版结构指导上，json_member_once给出从本包Schema派生的根成员顺序和必填成员。'
    '按root_write_order依次完成根成员；可选字段仅在原Schema允许且有内容时填写，不为填满列表编造值。'
    '在内部为每一个对象单独记下已经写过的成员名，写完即标为已完成；同一对象同名字段最多一次。'
    'analysis_basis写过之后，不能在plans、order_advice之后或回答末尾再追加一次分析依据。'
    '若分析依据需要修改，在内部替换其原位置的内容，只输出最后一份完整JSON；不要输出追加段、补丁或重复副本。'
    '不同对象可有相同字段名，同一对象内不可以；例如多个horizon各自都有direction不属于重复。'
    '完成前内部核对：根成员无重复、每个嵌套对象无重复、所有必填成员已出现、一个根对象完整闭合。'
    '不要把这份清单、计数、自检过程或json_member_once写入回答。'
    '字段顺序仅为生成指导，原Schema与业务校验仍是接收依据。'
    '全部冻结事实、时间表、周期和原端点保持；不得为排版缩短必要分析、统一改为不确定、改变价格或放宽条件。\n'
)


def build_plan(shape):
    return {'version': VERSION, 'root_write_order': [r['name'] for r in shape['root_members']],
            'required_root_members': [r['name'] for r in shape['root_members'] if r['required']],
            'max_occurrences_per_object': 1, 'append_completed_member': False}


def project_plan(value, shape):
    fields = ['version', 'root_write_order', 'required_root_members', 'max_occurrences_per_object', 'append_completed_member']
    if not isinstance(value, dict) or any(k not in value for k in fields):
        raise DomainError('MEMBER_PLAN_INVALID', '字段唯一性指导缺失，已停止外发。', 409)
    plan = {k: deepcopy(value[k]) for k in fields}
    if plan != build_plan(shape) or type(plan['max_occurrences_per_object']) is not int or plan['append_completed_member'] is not False:
        raise DomainError('MEMBER_PLAN_INVALID', '字段唯一性指导修订或成员与冻结Schema不符，已停止外发。', 409)
    return plan


def render(base, plan):
    prefix, tail = base.split(MARKER, 1);tail = tail.lstrip()
    facts, end = json.JSONDecoder().raw_decode(tail)
    facts['request']['prompt_version'] = PROMPT_VERSION
    facts['json_member_once'] = deepcopy(plan)
    packet = prefix.replace(BASE_PROMPT, PROMPT_VERSION) + INSTRUCTIONS + MARKER + json.dumps(facts, ensure_ascii=False, separators=(',', ':')) + tail[end:]
    if len(packet) > 250000:
        raise DomainError('PACKET_TOO_LARGE', f'候选27完整资料为{len(packet):,}字符，超过250,000字符；未截断或发送。', 422)
    return packet


def build_runtime_packet(request, context, schema):
    from .document_candidate import build_runtime_packet as build26
    base = build26({**deepcopy(request), 'prompt_version': BASE_PROMPT}, context, schema)
    facts = json.JSONDecoder().raw_decode(base.split(MARKER, 1)[1].lstrip())[0]
    return render(base, build_plan(facts['json_document_shape']))


def outbound_runtime_packet(packet):
    from .document_candidate import outbound_runtime_packet as project26
    prefix, tail = packet.split(MARKER, 1);tail = tail.lstrip()
    facts, end = json.JSONDecoder().raw_decode(tail)
    if prefix.count(INSTRUCTIONS) != 1:
        raise DomainError('MEMBER_PLAN_INVALID', '候选27原指导缺失或重复，已停止外发。', 409)
    plan = project_plan(facts.pop('json_member_once', None), facts['json_document_shape'])
    facts['request']['prompt_version'] = BASE_PROMPT
    base = prefix.replace(INSTRUCTIONS, '', 1).replace(PROMPT_VERSION, BASE_PROMPT) + MARKER + json.dumps(facts, ensure_ascii=False, separators=(',', ':')) + tail[end:]
    return render(project26(base), plan)
