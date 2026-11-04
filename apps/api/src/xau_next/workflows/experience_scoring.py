"""Predeclared experiment-only views of the existing objective Bid M1 evaluator."""

from __future__ import annotations

import copy
from collections import Counter
from decimal import Decimal
from pathlib import Path

from xau_next.domains.review.public import compute_prediction
from xau_next.shared.errors import DomainError
from xau_next.shared.values import parse_utc, utc_now

from .experience_experiment import digest, load_pair, load_plan, method_hashes, read, save


def score_window(version, market, minutes, start, as_of, neutral_band, scope):
    original = version["answer"]
    horizon = next(h for h in original["horizons"] if h["horizon_minutes"] == minutes)
    metadata = {
        "scope": scope,
        "horizon_minutes": minutes,
        "start": start,
        "end": horizon["end_at"],
        "original_answer_sha256": digest(original),
    }
    if start is None:
        return {**metadata, "state": "unscorable", "reason": "共同实际可用时间未知。", "metrics": []}
    start = max((start, original["market_cutoff"]), key=parse_utc)
    metadata["start"] = start
    if parse_utc(start) >= parse_utc(horizon["end_at"]):
        return {**metadata, "state": "unscorable", "reason": "实际可用后没有剩余窗口。", "metrics": []}
    derived = copy.deepcopy(version)
    selected = next(h for h in derived["answer"]["horizons"] if h["horizon_minutes"] == minutes)
    derived["answer"]["market_cutoff"] = start
    phase_indices, phases, points, point_indices = [], [], [], []
    for index, phase in enumerate(selected["phases"]):
        if parse_utc(phase["end_at"]) > parse_utc(start):
            phases.append({**phase, "start_at": max((start, phase["start_at"]), key=parse_utc)})
            phase_indices.append(index)
    for index, point in enumerate(selected["turning_points"]):
        if parse_utc(point["latest_at"]) >= parse_utc(start):
            points.append({**point, "earliest_at": max((start, point["earliest_at"]), key=parse_utc)})
            point_indices.append(index)
    selected.update(phases=phases, turning_points=points)
    if start != original["market_cutoff"]:
        # Baseline direction uses the original anchor. Reusing it with a later actual
        # delta would silently change its meaning, so only full-window baseline is scored.
        derived["baselines"] = []
        metadata["baseline_scope"] = "unsupported_after_available_original_anchor"
    try:
        review = compute_prediction(
            derived, market, horizon_minutes=minutes, as_of=as_of, neutral_band=neutral_band
        ).to_dict()["review"]
    except DomainError as error:
        return {**metadata, "state": "unscorable", "reason": error.code, "metrics": []}
    metrics = review["metrics"]
    lookup = {m["name"]: m for m in metrics if m.get("segment_index") is None}
    for edge in ("low", "high"):
        actual = lookup.get("actual_" + edge)
        if actual and actual["state"] == "available" and actual["value"] is not None:
            metrics.append(
                {
                    "name": ("lower" if edge == "low" else "upper") + "_boundary_absolute_error",
                    "value": str(abs(Decimal(horizon["expected_range"][edge]) - Decimal(actual["value"]))),
                    "state": "available",
                    "unit": "price",
                    "reason": None,
                    "observed_label": None,
                    "segment_index": None,
                }
            )
    # Preserve original phase/turn indices when early segments are clipped away.
    for metric in metrics:
        index = metric.get("segment_index")
        if index is not None:
            indices = phase_indices if metric["name"].startswith("phase.") else point_indices
            metric["segment_index"] = indices[index]
    direction = next((m for m in metrics if m["name"] == "direction_correct"), None)
    state = "scored" if direction and direction["state"] == "available" else "direction_unscorable"
    return {
        **metadata,
        "state": state,
        "coverage": review["coverage"],
        "outcome": review["outcome"],
        "reasons": review["reasons"],
        "evaluated_start": review["evaluated_start"],
        "evaluated_end": review["evaluated_end"],
        "metrics": metrics,
    }


def score_pair(root, sample_id, market, *, as_of, clock=utc_now):
    root = Path(root)
    plan, _frozen = load_pair(root, sample_id)
    folder = root / sample_id
    if plan["method_hashes"] != method_hashes():
        raise DomainError("EXPERIMENT_METHOD_CHANGED", "预登记的评分实现已变化，不能重解释旧结果。", 409)
    base = read(folder / "base-request.json")["context"]["market"]
    for field in ("symbol", "account_scope_id", "source_kind"):
        if market.get(field) != base.get(field):
            raise DomainError(
                "EXPERIMENT_MARKET_MISMATCH", "评价行情必须沿用冻结资料的来源、账户和品种。", 409
            )
    if parse_utc(as_of) > parse_utc(clock()):
        raise DomainError("EXPERIMENT_FUTURE_AS_OF", "评价时刻不能晚于当前实际时刻。", 409)
    results = {
        arm: read(folder / (arm + "-result.json")) if (folder / (arm + "-result.json")).exists() else {}
        for arm in ("A", "B")
    }
    available = [results[a].get("available_at") for a in ("A", "B")]
    common = max(available, key=parse_utc) if all(available) else None
    scored = {}
    for arm in ("A", "B"):
        record = results[arm]
        if record.get("state") != "succeeded":
            scored[arm] = [
                {
                    "horizon_minutes": n,
                    "scope": scope,
                    "state": "unscorable",
                    "reason": record.get("state", "not_run"),
                    "metrics": [],
                }
                for n in plan["horizon_minutes"]
                for scope in plan["scopes"]
            ]
            continue
        raw = (folder / (arm + "-answer.json")).read_bytes()
        if digest(raw) != record["answer_sha256"]:
            raise DomainError("EXPERIMENT_ANSWER_CHANGED", "原回答哈希不符。", 409)
        answer = read(folder / (arm + "-answer.json"))
        version = {
            "answer": answer,
            "available_at": record["available_at"],
            "version_id": plan["experiment_id"] + ":" + sample_id + ":" + arm,
            "forecast_id": answer["forecast_id"],
            "baselines": read(folder / (arm + "-context.json"))
            .get("statistics", {})
            .get("frozen_baselines", []),
        }
        starts = {
            "full_descriptive": answer["market_cutoff"],
            "after_own_available": record["available_at"],
            "after_common_available": common,
        }
        scored[arm] = [
            score_window(version, market, n, starts[scope], as_of, plan["neutral_band"], scope)
            for n in plan["horizon_minutes"]
            for scope in plan["scopes"]
        ]
    identity = "score-" + digest({"market": market, "as_of": as_of})[:20]
    result = {
        "score_method_version": plan["score_method_version"],
        "method_hashes": plan["method_hashes"],
        "sample_id": sample_id,
        "cohort": plan["cohort"],
        "source_kind": market["source_kind"],
        "as_of": as_of,
        "recorded_at": clock(),
        "common_available_at": common,
        "actual_market_sha256": digest(market),
        "arms": scored,
    }
    destination = folder / identity
    if destination.exists():
        raise DomainError("EXPERIMENT_SCORE_EXISTS", "该行情及评价时刻已有评分，保留原证据。", 409)
    save(destination / "market.json", market)
    save(destination / "scores.json", result)
    return result


def groups(metadata):
    """Conservative overlap components, not a statistical assertion of independence."""
    values = list(metadata.items())
    parent = list(range(len(values)))

    def find(i):
        while parent[i] != i:
            i = parent[i]
        return i

    for i, (_, a) in enumerate(values):
        for j, (_, b) in enumerate(values[:i]):
            shared = (
                a["technical_repeat_group"] == b["technical_repeat_group"]
                or a["original_window_id"] == b["original_window_id"]
            )
            overlap = a["source_group"] == b["source_group"] and max(
                parse_utc(a["market_cutoff"]), parse_utc(b["market_cutoff"])
            ) < min(parse_utc(a["window_end"]), parse_utc(b["window_end"]))
            if shared or overlap:
                parent[find(i)] = find(j)
    return {sample: "block-" + str(find(i) + 1) for i, (sample, _) in enumerate(values)}


def report(root, *, clock=utc_now):
    root = Path(root)
    plan = load_plan(root)
    if plan["method_hashes"] != method_hashes():
        raise DomainError(
            "EXPERIMENT_METHOD_CHANGED", "固定方法实现已变化，需使用原候选读取和汇总；原结果未改写。", 409
        )
    samples, metadata, rows = [], {}, []
    for selected in plan["samples"]:
        sample_id = selected["sample_id"]
        folder = root / sample_id
        frozen = (folder / "frozen.json").exists()
        if frozen:
            _, meta = load_pair(root, sample_id)
            metadata[sample_id] = meta
        # Predeclared latest recorded evidence revision is used; all revisions remain on disk.
        revisions = [read(p) for p in folder.glob("score-*/scores.json")]
        latest = max(revisions, key=lambda v: parse_utc(v["recorded_at"])) if revisions else None
        sample = {**selected, "frozen": frozen, "score_revisions": len(revisions), "arms": {}}
        if frozen:
            sample["source_identity"] = {
                key: metadata[sample_id][key]
                for key in (
                    "original_window_id",
                    "time_block",
                    "technical_repeat_group",
                    "source_group",
                    "source_kind",
                    "market_cutoff",
                    "window_end",
                )
            }
        for arm in ("A", "B"):
            result_path = folder / (arm + "-result.json")
            result = (
                read(result_path)
                if result_path.exists()
                else {"state": "outcome_unknown" if (folder / "started.json").exists() else "not_run"}
            )
            sample["arms"][arm] = result
            for n in plan["horizon_minutes"]:
                for scope in plan["scopes"]:
                    score = (
                        next(
                            (
                                s
                                for s in latest["arms"][arm]
                                if s["horizon_minutes"] == n and s["scope"] == scope
                            ),
                            None,
                        )
                        if latest
                        else None
                    )
                    rows.append(
                        {
                            "sample_id": sample_id,
                            "arm": arm,
                            "cohort": plan["cohort"],
                            "horizon_minutes": n,
                            "scope": scope,
                            "state": score["state"] if score else "unscorable",
                            "reason": score.get("reason") if score else "no_score",
                            "metrics": score["metrics"] if score else [],
                            "engineering_state": result["state"],
                        }
                    )
        samples.append(sample)
    blocks = groups(metadata)
    for row in rows:
        row["overlap_group"] = blocks.get(row["sample_id"])
    engineering = {}
    for arm in ("A", "B"):
        outcomes = [s["arms"][arm] for s in samples]
        engineering[arm] = {
            "registered_denominator": len(samples),
            "states": dict(Counter(r["state"] for r in outcomes)),
            "first_schema_pass": sum(r.get("schema_valid", False) for r in outcomes),
            "first_business_pass": sum(r.get("business_valid", False) for r in outcomes),
            "experience_errors": sum(r.get("experience_error", False) for r in outcomes),
            "errors": dict(Counter(r["error_code"] for r in outcomes if r.get("error_code"))),
            "latency_seconds": [r.get("latency_seconds") for r in outcomes],
            "known_input_tokens": sum(r.get("input_tokens") or 0 for r in outcomes),
            "known_output_tokens": sum(r.get("output_tokens") or 0 for r in outcomes),
            "usage_unknown": sum(
                r.get("input_tokens") is None or r.get("output_tokens") is None for r in outcomes
            ),
            "estimated_cost_subtotal": str(
                sum(
                    (Decimal(r["estimated_cost"]) for r in outcomes if r.get("estimated_cost") is not None),
                    Decimal(0),
                )
            ),
            "cost_unknown": sum(r.get("estimated_cost") is None for r in outcomes),
            "billing_cost_status": "unknown; estimates use reported tokens and configured unit prices, not invoices",
            "currency": plan["configuration"].get("price_currency"),
        }
    aggregates = []
    for arm in ("A", "B"):
        for n in plan["horizon_minutes"]:
            for scope in plan["scopes"]:
                selected = [
                    r for r in rows if (r["arm"], r["horizon_minutes"], r["scope"]) == (arm, n, scope)
                ]
                metrics = {}
                for row in selected:
                    for m in row["metrics"]:
                        name = m["name"]
                        if m["state"] == "available" and m["value"] is not None:
                            metrics.setdefault(name, []).append(Decimal(m["value"]))
                counts = metrics.get("direction_correct", [])
                aggregates.append(
                    {
                        "arm": arm,
                        "cohort": plan["cohort"],
                        "horizon_minutes": n,
                        "scope": scope,
                        "registered_denominator": len(selected),
                        "direction_scorable": len(counts),
                        "direction_correct": str(sum(counts)),
                        "direction_accuracy": str(sum(counts) / len(counts)) if counts else None,
                        "direction_unscorable": len(selected) - len(counts),
                        "metric_means": {
                            k: {"mean": str(sum(v) / len(v)), "scorable": len(v)} for k, v in metrics.items()
                        },
                        "sample_unit": "raw paired observations; overlaps and technical repeats are NOT independent samples",
                    }
                )
    paired = []
    for n in plan["horizon_minutes"]:
        for scope in plan["scopes"]:
            wins, losses, ties, absent, block_deltas = 0, 0, 0, 0, {}
            for sample in plan["samples"]:
                observations = [
                    next(
                        r
                        for r in rows
                        if (r["sample_id"], r["arm"], r["horizon_minutes"], r["scope"])
                        == (sample["sample_id"], arm, n, scope)
                    )
                    for arm in ("A", "B")
                ]
                directions = [
                    next(
                        (
                            Decimal(m["value"])
                            for m in r["metrics"]
                            if m["name"] == "direction_correct"
                            and m["state"] == "available"
                            and m["value"] is not None
                        ),
                        None,
                    )
                    for r in observations
                ]
                if any(d is None for d in directions):
                    absent += 1
                    continue
                difference = directions[0] - directions[1]
                wins += difference > 0
                losses += difference < 0
                ties += difference == 0
                block_deltas.setdefault(blocks[sample["sample_id"]], []).append(difference)
            paired.append(
                {
                    "horizon_minutes": n,
                    "scope": scope,
                    "registered_pairs": plan["pair_count"],
                    "A_correct_B_wrong": wins,
                    "A_wrong_B_correct": losses,
                    "ties": ties,
                    "incomplete_pairs": absent,
                    "overlap_group_mean_differences": {
                        k: str(sum(v) / len(v)) for k, v in block_deltas.items()
                    },
                }
            )
    result = {
        "method_version": plan["method_version"],
        "score_method_version": plan["score_method_version"],
        "plan_sha256": digest(plan),
        "generated_at": clock(),
        "cohort": plan["cohort"],
        "registered_pairs": plan["pair_count"],
        "registered_arms": plan["planned_requests"],
        "frozen_pairs": len(metadata),
        "overlap_components_upper_bound": len(set(blocks.values())),
        "sample_groups": blocks,
        "engineering": engineering,
        "aggregates": aggregates,
        "samples": samples,
        "prediction_rows": rows,
        "paired_direction": paired,
        "unsupported_metrics": plan["unsupported_metrics"],
        "estimand": plan["estimand"],
        "conclusion": "待真实对照验证；合成数据和小样本流程结果不证明预测改善。",
    }
    lines = [
        "# 历史经验配对实验报告",
        "",
        f"类别：{plan['cohort']}；预登记 {plan['pair_count']} 对 / {plan['planned_requests']} 次。",
        "",
        plan["estimand"],
        "",
        "全部预登记样本计入分母；未执行、失败、未知均保留。",
        "",
        "|组|结构首次通过/登记|业务首次通过/登记|状态|经验字段错误|",
        "|---|---|---|---|---|",
    ]
    for arm, e in engineering.items():
        lines.append(
            f"|{arm}|{e['first_schema_pass']}/{e['registered_denominator']}|{e['first_business_pass']}/{e['registered_denominator']}|{e['states']}|{e['experience_errors']}|"
        )
    lines += [
        "",
        "|组|周期分钟|评价范围|方向正确/可评分/登记|区间下界误差|上界误差|覆盖|预测宽度|",
        "|---|---|---|---|---|---|---|---|",
    ]
    for a in aggregates:
        m = a["metric_means"]
        get = lambda key, metrics=m: metrics.get(key, {}).get("mean", "未知")
        lines.append(
            f"|{a['arm']}|{a['horizon_minutes']}|{a['scope']}|{a['direction_correct']}/{a['direction_scorable']}/{a['registered_denominator']}|{get('lower_boundary_absolute_error')}|{get('upper_boundary_absolute_error')}|{get('range_contained')}|{get('predicted_range_width')}|"
        )
    lines += [
        "",
        f"冻结资料的重叠连通组上界：{len(set(blocks.values()))}；不宣称统计独立。JSON保留原窗口、时间块、技术重复身份、逐案例指标、延迟、用量和未知费用。",
        "",
        "不支持：" + "、".join(plan["unsupported_metrics"]) + "。裁剪后的基准方向因原锚点不同不评分。",
        "",
        result["conclusion"],
    ]
    destination = root / ("report-" + digest(result)[:20])
    save(destination / "report.json", result)
    save(destination / "REPORT.md", "\n".join(lines).encode())
    return destination, result
