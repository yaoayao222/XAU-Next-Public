"""Strict JSON parsing and schema-derived generation guidance; never repair text."""
from copy import deepcopy
import json

from xau_next.shared.errors import DomainError
from .json_duplicates import duplicate_fields

VERSION = "json-document-shape/1"
INSTRUCTIONS = (
    "\n# 候选26：JSON对象层级与闭合检查\n"
    "json_document_shape来自本包权威Schema，仅帮助正确排版，不改变任何字段或业务规则。"
    "按root_members核对每个根成员的类型和层级；analysis_basis是一个对象，不能当作数组。"
    "analysis_basis.reassessment_conditions是该对象内的数组：它结束时关闭]，再关闭analysis_basis的}。"
    "此时不能再追加一个]；horizons应在自己的字段内完整结束，不要用它的括号关闭analysis_basis。"
    "candidate_assessments、experience_application、prediction_adjustments和plans均是根对象成员，"
    "不能塞进analysis_basis、某个horizon或experience_application内部。"
    "参考containers的closing_token逐层闭合，只关闭当前容器一次；字符串中的括号只是文本。"
    "先完成一个根成员的全部内部字段，关闭该成员，再写逗号和下一个根成员；全部根成员结束后才关闭根对象。"
    "不要输出第二个根对象、重复字段、NaN、围栏或补丁；不能在根对象结束后追加遗漏字段。"
    "在内部核对完整JSON及每层Schema，不输出检查过程。"
    "这些是结构检查，不提供方向、价格或概率，不得因此把预测改成uncertain、删除必要解释或凑交易候选。"
    "原输入、时间预算、端点和全部校验保持；无法形成交易候选仍按原格式解释。\n"
)


def _error(message):
    return DomainError("SCHEMA_INVALID", message, 422,
                       [{"path": "/", "code": "SCHEMA_INVALID", "message": message}])


class _Duplicate(ValueError):
    pass


class _Constant(ValueError):
    pass


def parse_document(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise _Duplicate()
            result[key] = value
        return result
    def constant(_):
        raise _Constant()
    try:
        # Check the whole grammar before hooks inspect a completed inner object.
        # Otherwise a duplicate in an early object can hide a later broken ']'.
        # This first result is discarded; only the strict second pass may return.
        json.loads(raw)
        return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)
    except json.JSONDecodeError as exc:
        token = raw[exc.pos:exc.pos + 1]
        if token == "]":
            hint = "此处的 ] 与当前JSON结构不符，请检查数组是否多关闭一次或对象尚未正确闭合。"
        elif token == "}":
            hint = "此处的 } 与当前JSON结构不符，请检查对象是否提前闭合或缺少字段分隔。"
        elif exc.msg == "Extra data":
            hint = "根对象结束后仍有额外内容，必须只提交一个完整JSON。"
        elif exc.pos == len(raw):
            hint = "回答在JSON尚未完整结束时终止。"
        else:
            hint = "请检查该处的逗号、引号、括号及完整JSON层级。"
        raise _error(f"回答JSON语法错误：第{exc.lineno}行第{exc.colno}列（第{exc.pos + 1}个字符）。{hint}原回答已保留，未自动修补或导入。") from exc
    except _Duplicate as exc:
        diagnostic = duplicate_fields(raw)
        if diagnostic:
            row = diagnostic['items'][0]
            first, again = row['first'], row['duplicate']
            relation = {'identical': '两处内容相同', 'conflicting': '两处内容不一致', 'unknown': '两处内容是否一致无法可靠判断'}[row['values_relation']]
            message = (f"回答JSON含重复字段：{row['label']}（{row['path']}）。"
                       f"首次在第{first['line']}行第{first['column']}列，再次在第{again['line']}行第{again['column']}列；{relation}。"
                       f"共发现{diagnostic['total']}处重复，仍未通过检查；原回答已保留，未自动删除或选择其中一份。")
        else:
            message = "回答JSON含重复字段；同一对象内的字段只能出现一次。原回答已保留，未自动取其中一个值。"
        error = _error(message)
        error.duplicate_diagnostics = diagnostic
        raise error from exc
    except _Constant as exc:
        raise _error("回答JSON包含NaN或Infinity等非法数值。原回答已保留，未替换为null或其他数字。") from exc
    except (ValueError, TypeError, RecursionError) as exc:
        raise _error("回答必须是一个完整、可解析的JSON文档。原回答已保留，未自动修补或导入。") from exc


def document_error(raw):
    """Derived read-only explanation; successful parsing is not answer acceptance."""
    try:
        parse_document(raw)
    except DomainError as exc:
        return exc.detail
    return None


def document_diagnosis(raw):
    """One strict parse for read-only display; never grants answer acceptance."""
    try:
        parse_document(raw)
    except DomainError as exc:
        return {'detail': exc.detail, 'duplicates': getattr(exc, 'duplicate_diagnostics', None)}
    return {'detail': None, 'duplicates': None}


def _resolve(value, schema):
    seen = set()
    while "$ref" in value:
        ref = value["$ref"]
        if not ref.startswith("#/") or ref in seen:
            raise DomainError("DOCUMENT_SCHEMA_INVALID", "结构指导无法解析本地Schema引用，已停止构包。", 409)
        seen.add(ref)
        value = schema
        for key in ref[2:].split("/"):
            value = value[key.replace("~1", "/").replace("~0", "~")]
    return value


def _types(value, schema):
    value = _resolve(value, schema)
    if "type" in value:
        return sorted(value["type"] if isinstance(value["type"], list) else [value["type"]])
    return sorted({t for key in ("anyOf", "oneOf") for child in value.get(key, []) for t in _types(child, schema)})


def build_shape(schema):
    root = schema["properties"]
    basis = _resolve(root["analysis_basis"], schema)
    return {"version": VERSION, "root_type": schema["type"],
            "root_members": [{"name": name, "types": _types(value, schema), "required": name in schema.get("required", [])}
                             for name, value in root.items()],
            "containers": [{"path": path, "type": kind, "closing_token": "]" if kind == "array" else "}"}
                           for path, value in [("/analysis_basis", root["analysis_basis"]),
                               ("/analysis_basis/reassessment_conditions", basis["properties"]["reassessment_conditions"]),
                               *[("/" + k, root[k]) for k in ["horizons", "candidate_assessments", "experience_application", "prediction_adjustments", "plans"]]]
                           for kind in _types(value, schema) if kind in {"array", "object"}]}


def project_shape(value):
    if value.get("version") != VERSION:
        raise DomainError("DOCUMENT_REVISION_INVALID", "JSON结构指导修订无法识别，已停止外发。", 409)
    return {"version": VERSION, "root_type": value["root_type"],
            "root_members": [{k: deepcopy(row[k]) for k in ["name", "types", "required"]} for row in value["root_members"]],
            "containers": [{k: deepcopy(row[k]) for k in ["path", "type", "closing_token"]} for row in value["containers"]]}
