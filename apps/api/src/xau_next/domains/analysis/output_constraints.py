"""Opt-in 27b output guidance derived from authority and existing timing rules."""
from copy import deepcopy

from xau_next.shared.analysis_policy import analysis_preferences
from xau_next.shared.entry_timing import planned_timing
from .experience import non_experience_references

PROMPT_VERSION = "manual-analysis/17"
VERSION = "output-constraints/1"
DOCUMENT_PROMPT_VERSION = "manual-analysis/18"
DOCUMENT_VERSION = "output-constraints/2"
REFERENCE_PROMPT_VERSION = "manual-analysis/19"
REFERENCE_VERSION = "output-constraints/3"
INSTRUCTIONS = (
    "# 输出字段与期限自检（output-constraints/1）\n"
    "提交前按output_constraints逐项自检，不输出自检过程。"
    "prediction_adjustments.aspects只选其aspects列表中的方面；action只选其action列表中的动作，"
    "caution仅可作为action，不能写入aspects，不混用两个字段的枚举。"
    "每个方案entry_deadline不得晚于同一trading_style的max_entry_deadline；"
    "它是代码按原cutoff、模式时效及原预测终点计算的硬上限，不代表一定有足够观察时间。"
    "仍须满足timing_reference、合法闭柱与后续入场余量；不足时说明no_candidate缺口，不能延期。"
    "此上限不延长预测窗口、不限制或重置成交后持仓时间，也不授权当前入场。\n"
)
DOCUMENT_INSTRUCTIONS = INSTRUCTIONS.replace(VERSION, DOCUMENT_VERSION) + (
    "# 完整JSON文档结构自检\n"
    "按output_constraints.json_document中的root_members顺序填写一个根对象，所有字段保持同级。"
    "这些成员及类型来自完整权威Schema，只是结构检查，不是需要输出的额外字段。"
    "candidate_assessments是根对象内的数组；完成最后一个元素并关闭数组后，根对象仍保持打开。"
    "experience_application是同一根对象内的对象，prediction_adjustments是同级数组，不能写到根对象外面。"
    "只有全部必填成员及其嵌套内容完成后才关闭最外层对象；不能先结束根对象再追加逗号和字段。"
    "输出必须能被一次完整JSON解析消耗完毕；字符串以外的括号逐层配对，字符串引号及反斜线正确转义。"
    "根对象结束后只能有空白，不得输出第二个JSON、解释、代码围栏或遗漏字段的补丁。"
    "提交前在内部同时核对完整文档、Schema和业务规则；不要输出自检过程，不为配平括号删除字段或内容。\n"
)
REFERENCE_INSTRUCTIONS = DOCUMENT_INSTRUCTIONS.replace(DOCUMENT_VERSION, REFERENCE_VERSION) + (
    "# 经验案例身份与关联自检\n"
    "output_constraints.experience_links列出本包可引用的案例身份和当前依据身份，不能把两类身份混用。"
    "case_assessments.review_ref只能从allowed_case_review_refs逐字符复制，保留完整字符、大小写和修订尾号；"
    "不要凭记忆重打、缩写或把digest_id、forecast_id、当前快照身份当作案例身份。"
    "先完成实际评估的case_assessments，再从其中已登记的review_ref复制到related_review_refs，"
    "不能重新拼写。提交前核对每个related_review_refs都是已登记review_ref的精确字符串，"
    "同时每个已登记review_ref都在allowed_case_review_refs中；相似但少一个字符也不相等。"
    "每个decision=adopt案例至少被一条本次实际预测周期的调整说明引用；影响可以是retained或caution，"
    "不要求改变方向。current_evidence_refs只从allowed_current_evidence_refs选择。"
    "digest_id须与experience_links.digest_id相同。没有经验时使用unavailable及空案例、空调整；"
    "有经验可评估后不采用，不能声称未收到；不强制登记所有案例或为了通过校验采用不适用案例。"
    "这些是提交前的内部检查，不增加答案字段，不输出检查过程。\n"
)


def build_output_constraints(request, context, schema):
    properties = schema["$defs"]["PredictionAdjustment"]["properties"]
    preferences = {**analysis_preferences(context["settings"].get("values", {})),
                   **request.get("analysis_preferences", {})}
    rows = []
    for style in preferences["enabled_styles"]:
        # Reuse the import validator's timing calculation; no new deadline rule.
        base = {"trading_style": style, "valid_from": request["market_cutoff"],
                "entry_deadline": request["window_end"], "entry_checks": [],
                "activation": {"kind": "quote_compare"}, "entry": {"mode": "market"}}
        timing = planned_timing(base, request, context)
        rows.append({"trading_style": style, "max_entry_deadline": timing["analysis_fresh_until"]})
    return {"version": VERSION,
            "prediction_adjustments": {"aspects": deepcopy(properties["aspects"]["items"]["enum"]),
                                       "action": deepcopy(properties["action"]["enum"])},
            "style_deadlines": rows}


def project_output_constraints(value):
    result = {"version": value["version"],
            "prediction_adjustments": {key: list(value["prediction_adjustments"][key]) for key in ("aspects", "action")},
            "style_deadlines": [{key: row[key] for key in ("trading_style", "max_entry_deadline")}
                                for row in value["style_deadlines"]]}
    if value["version"] in {DOCUMENT_VERSION, REFERENCE_VERSION}:
        document = value["json_document"]
        result["json_document"] = {
            "root_type": document["root_type"],
            "additional_properties": document["additional_properties"],
            "required": list(document["required"]),
            "root_members": [{"name": row["name"], "types": list(row["types"])}
                             for row in document["root_members"]],
        }
    if value["version"] == REFERENCE_VERSION:
        links = value["experience_links"]
        result["experience_links"] = {
            "digest_id": links["digest_id"],
            "allowed_case_review_refs": list(links["allowed_case_review_refs"]),
            "allowed_current_evidence_refs": list(links["allowed_current_evidence_refs"]),
        }
    return result


def build_reference_constraints(request, context, schema):
    """Expose existing frozen reference identities, never select or repair answers."""
    result = build_document_constraints(request, context, schema)
    result["version"] = REFERENCE_VERSION
    experience = context.get("experience")
    result["experience_links"] = {
        "digest_id": experience["digest_id"] if experience else None,
        "allowed_case_review_refs": list(dict.fromkeys(case["review_ref"]
            for case in experience.get("cases", []))) if experience else [],
        "allowed_current_evidence_refs": sorted(non_experience_references(context)),
    }
    return result


def build_document_constraints(request, context, schema):
    """Opt-in structure guidance only; no example values or response repair."""
    def types(node):
        if "$ref" in node:
            prefix = "#/$defs/"
            assert node["$ref"].startswith(prefix)
            return types(schema["$defs"][node["$ref"][len(prefix):]])
        if "type" in node:
            return list(node["type"]) if isinstance(node["type"], list) else [node["type"]]
        return list(dict.fromkeys(name for branch in node.get("anyOf", []) for name in types(branch)))

    result = build_output_constraints(request, context, schema)
    result["version"] = DOCUMENT_VERSION
    result["json_document"] = {
        "root_type": schema["type"],
        "additional_properties": schema["additionalProperties"],
        "required": list(schema["required"]),
        "root_members": [{"name": name, "types": types(schema["properties"][name])}
                         for name in list(schema["required"]) + [key for key in schema["properties"]
                                                                if key not in schema["required"]]],
    }
    return result
