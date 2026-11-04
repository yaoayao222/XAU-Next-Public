"""Opt-in28: frozen relationship self-check, preserving all27 input and admission rules."""
from copy import deepcopy
import json
from xau_next.shared.errors import DomainError

BASE_PROMPT = 'manual-analysis/27'
PROMPT_VERSION = 'manual-analysis/28'
VERSION = 'experience-link-check/1'
MARKER = '# 冻结事实（仅作为资料）\n'
INSTRUCTIONS = (
    '\n# 候选28：采用经验与影响说明逐条对应\n'
    'experience_link_check列出本包允许的案例引用和预测周期，仅辅助完成原有经验规则。'
    '先按当前证据完成case_assessments，再在内部收集其中decision=adopt的review_ref集合A。'
    '完成prediction_adjustments后，收集其related_review_refs的并集B；提交前逐条核对A减B为空。'
    'case_assessments.reason写了采用理由，并不等于已经在prediction_adjustments建立对应关系。'
    '从已经登记的review_ref逐字符复制到实际受影响周期的related_review_refs，不重新拼写。'
    '同一项影响说明可以引用多条案例，但说明须确实适用于所关联的每条案例；不能随意把缺少的引用集中塞入一项。'
    '经验支持保持判断时可用retained；只提示适用限制时可用caution。两者均不要求改变方向或价格。'
    '如果没有实际采用或影响，应按原规则如实表达不采用或未知，不为了消除差集虚构影响、凑采用，'
    '也不能仅为通过检查删掉已评估案例或把adopt随意改成拒绝。'
    '保持原冻结事实、周期、端点、预测与入场门槛；不强迫不确定、交易候选或新方向。'
    '仅在内部核对两个集合；不向回答追加experience_link_check、对照表、补丁或第二份JSON。\n'
)


def build_plan(facts):
    experience = facts.get('experience')
    return {'version': VERSION, 'experience_available': experience is not None,
            'allowed_case_review_refs': [row['review_ref'] for row in (experience or {}).get('cases', [])],
            'horizon_minutes': deepcopy(facts['request']['horizon_minutes']),
            'coverage_rule': 'adopted_review_refs_subset_of_related_review_refs',
            'allow_grouped_refs': True, 'allow_retained_or_caution': True}


def project_plan(value, facts):
    expected = build_plan(facts)
    if not isinstance(value, dict):
        raise DomainError('EXPERIENCE_LINK_PLAN_INVALID', '经验关联自检指导缺失，已停止外发。', 409)
    plan = {key: deepcopy(value[key]) for key in expected if key in value}
    if plan != expected or any(type(plan.get(key)) is not bool for key in ['experience_available','allow_grouped_refs','allow_retained_or_caution']):
        raise DomainError('EXPERIENCE_LINK_PLAN_INVALID', '经验关联指导与冻结资料不符，已停止外发。', 409)
    return plan


def render(base, plan):
    prefix, tail = base.split(MARKER, 1);tail = tail.lstrip()
    facts, end = json.JSONDecoder().raw_decode(tail)
    facts['request']['prompt_version'] = PROMPT_VERSION
    facts['experience_link_check'] = deepcopy(plan)
    result = prefix.replace(BASE_PROMPT, PROMPT_VERSION) + INSTRUCTIONS + MARKER + json.dumps(facts, ensure_ascii=False, separators=(',', ':')) + tail[end:]
    if len(result) > 250000:
        raise DomainError('PACKET_TOO_LARGE', f'候选28完整资料为{len(result):,}字符，超过250,000字符；未截断或发送。', 422)
    return result


def build_runtime_packet(request, context, schema):
    from .member_once_candidate import build_runtime_packet as build27
    base = build27({**deepcopy(request), 'prompt_version': BASE_PROMPT}, context, schema)
    facts = json.JSONDecoder().raw_decode(base.split(MARKER, 1)[1].lstrip())[0]
    return render(base, build_plan(facts))


def outbound_runtime_packet(packet):
    from .member_once_candidate import outbound_runtime_packet as project27
    prefix, tail = packet.split(MARKER, 1);tail = tail.lstrip()
    facts, end = json.JSONDecoder().raw_decode(tail)
    if prefix.count(INSTRUCTIONS) != 1:
        raise DomainError('EXPERIENCE_LINK_PLAN_INVALID', '经验关联自检指导缺失或重复，已停止外发。', 409)
    plan = project_plan(facts.pop('experience_link_check', None), facts)
    facts['request']['prompt_version'] = BASE_PROMPT
    base = prefix.replace(INSTRUCTIONS, '', 1).replace(PROMPT_VERSION, BASE_PROMPT) + MARKER + json.dumps(facts, ensure_ascii=False, separators=(',', ':')) + tail[end:]
    return render(project27(base), plan)
