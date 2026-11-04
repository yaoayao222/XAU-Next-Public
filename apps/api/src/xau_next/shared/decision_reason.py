"""Structured stopping evidence shared as values, without deciding domain eligibility."""

NEW_ANALYSIS = "使用最新资料创建新请求并取得新候选；不延长或恢复原方案。"


def stopping_reason(code, occurred_at, *, condition=None, deadline=None, next_step=NEW_ANALYSIS):
    return {"code": code, "occurred_at": occurred_at, "condition": condition,
            "deadline": deadline, "next_step": next_step}
