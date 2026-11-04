"""Equivalent flattening for the existing official DeepSeek wire schema.

This does not derive or relax the authoritative answer contract. It only annotates
the already-derived provider schema; the original full validator remains in use.
"""

from copy import deepcopy

from xau_next.shared.errors import DomainError

TYPE_NAMES = {"object", "array", "string", "number", "integer", "boolean", "null"}


def annotate_union_types(schema):
    """Flatten pure nested OR branches; preserve all assertion keywords.

    The existing callable name is retained. No type is invented: an untyped
    nested anyOf branch containing only a description is distributed into its
    parent anyOf. OR association preserves the accepted JSON instances.
    """

    def unsupported():
        raise DomainError(
            "MODEL_SCHEMA_UNSUPPORTED", "DeepSeek格式含无法证明等价的类型分支，未发送。", 409
        )

    def types(node):
        value = node.get("type")
        if value is None and "type" not in node and node.get("anyOf"):
            return list(dict.fromkeys(name for branch in node["anyOf"] for name in types(branch)))
        result = [value] if isinstance(value, str) else value
        if (
            not isinstance(result, list)
            or not result
            or any(not isinstance(name, str) or name not in TYPE_NAMES for name in result)
            or len(set(result)) != len(result)
        ):
            unsupported()
        return result

    def visit(node):
        if not isinstance(node, dict):
            unsupported()
        result = deepcopy(node)
        if "properties" in node:
            if not isinstance(node["properties"], dict):
                unsupported()
            result["properties"] = {name: visit(child) for name, child in node["properties"].items()}
        if "items" in node:
            result["items"] = visit(node["items"])
        if "anyOf" in node:
            branches = node["anyOf"]
            if not isinstance(branches, list) or not branches:
                unsupported()
            result["anyOf"] = []
            for child in branches:
                branch = visit(child)
                if "anyOf" in branch and set(branch) <= {"anyOf", "description"}:
                    for leaf in branch["anyOf"]:
                        leaf = deepcopy(leaf)
                        if branch.get("description"):
                            leaf["description"] = "\n".join(
                                text for text in (branch["description"], leaf.get("description")) if text
                            )
                        result["anyOf"].append(leaf)
                else:
                    result["anyOf"].append(branch)
            if any("type" not in branch for branch in result["anyOf"]):
                unsupported()
        # References/other combinators are outside the already-expanded provider
        # subset. Refuse rather than claim an unproved redundant annotation.
        if any(key in node for key in ("$ref", "$defs", "allOf", "oneOf", "not", "if", "then", "else")):
            unsupported()
        inferred = types(result)
        if isinstance(result.get("type"), list) and any(name in {"object", "array"} for name in inferred):
            unsupported()
        if result.get("type") == "object" and not result.get("properties"):
            unsupported()
        return result

    return visit(schema)
