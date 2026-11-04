from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from contextlib import nullcontext
from decimal import Decimal

from xau_next.shared.errors import DomainError
from xau_next.shared.snapshots import Snapshot
from xau_next.shared.values import parse_utc, utc_now

from .common import data, dec, digest
from .experience_statistics import forecast_statistics
from .experience_selection import select_cases


class ReviewService:
    """Only this service writes review-owned documents; pure workers return prepared results."""

    def __init__(self, db, clock=utc_now):
        self.db, self.clock, self.store = db, clock, db.documents("review")

    @staticmethod
    def _refresh_key(kind, target_id, version_id=None, horizon=None):
        return digest([kind, target_id, version_id, horizon])

    def refresh_needed(self, kind, target_id, *, version_id=None, horizon=None,
                       evidence_token=None, scope=None):
        """Clock time and new capture identities cannot unblock missing historical evidence."""
        with nullcontext(scope) if scope is not None else self.db.read() as reader:
            records = self.store.list(reader, "review", parent_id=target_id, state=kind, limit=1000)
            matching = [r["wire"] for r in records
                        if r["wire"]["inputs"].get("forecast_version_id") == version_id
                        and r["wire"]["inputs"].get("horizon_minutes") == horizon]
            if not matching:
                return True
            if kind != "actual_trade" and any(
                    r["outcome"] not in {"DATA_INCOMPLETE", "AMBIGUOUS"} for r in matching):
                return False
            if kind == "reference_plan" and any(r["method_version"] == "bidask_reference/3" for r in matching):
                return False  # The current method has no historical eligibility trajectory to rebuild.
            key = self._refresh_key(kind, target_id, version_id, horizon)
            checked = self.store.get(reader, "refresh_checkpoint", key)
            return evidence_token is not None and evidence_token != (checked or {}).get("evidence_token")

    @staticmethod
    def _result_signature(review):
        value = deepcopy(review)
        for key in ("available_at", "revision", "source_refs"):
            value.pop(key, None)
        value.get("inputs", {}).pop("market_snapshot_ids", None)
        return digest(value)

    def commit_refresh(self, prepared, *, uow, evidence_token=None):
        """One item is a durable checkpoint. New capture identities alone do not create revisions."""
        prepared = data(prepared)
        draft = prepared["review"]
        head = self.store.get(uow, "review", draft["review_id"])
        signature = self._result_signature(draft)
        if head and self._result_signature(head["wire"]) == signature:
            result = Snapshot.of(head["wire"])
        else:
            result = self.commit_review(prepared, uow=uow)
        binding = draft["inputs"]
        key = self._refresh_key(draft["target_kind"], draft["target_id"],
                                binding.get("forecast_version_id"), binding.get("horizon_minutes"))
        payload = {"checked_at": self.clock(), "review_id": draft["review_id"],
                   "review_revision": result.to_dict()["revision"],
                   "input_fingerprint": prepared["input_fingerprint"],
                   "result_signature": signature, "source_refs": draft["source_refs"],
                   "evidence_token": evidence_token}
        old = self.store.get(uow, "refresh_checkpoint", key)
        if old:
            self.store.update(uow, "refresh_checkpoint", key, payload, old["revision"])
        else:
            self.store.insert(uow, "refresh_checkpoint", key, payload, parent_id=draft["target_id"])
        return result

    def refresh_generation(self, kind, target_id, *, scope):
        checked = self.store.get(scope, "refresh_checkpoint", self._refresh_key(kind, target_id))
        return checked["revision"] if checked else 0

    def commit_review(self, prepared, *, uow=None):
        prepared = data(prepared)
        draft = prepared["review"]
        review_id = draft["review_id"]
        fingerprint = prepared["input_fingerprint"]
        pointer_id = digest({"review": review_id, "fingerprint": fingerprint})
        with nullcontext(uow) if uow is not None else self.db.write() as tx:
            existing = self.store.get(tx, "fingerprint", pointer_id)
            if existing:
                record = self.store.get(tx, "revision", existing["revision_document_id"])
                return Snapshot.of(record["wire"])
            now = self.clock()
            if parse_utc(now) < parse_utc(prepared["evidence_available_at"]):
                raise DomainError("REVIEW_NOT_DUE", "复盘提交时间不能早于所用证据的实际可用时间。", 409)
            head = self.store.get(tx, "review", review_id)
            revision = head["wire"]["revision"] + 1 if head else 1
            wire = {**draft, "available_at": now, "revision": revision}
            revision_id = f"{review_id}-r{revision:08d}"
            payload = {
                "wire": wire,
                "input_fingerprint": fingerprint,
                "evidence_available_at": prepared["evidence_available_at"],
            }
            self.store.insert(
                tx,
                "revision",
                revision_id,
                payload,
                parent_id=wire["target_id"],
                state=wire["target_kind"],
                available_at=now,
            )
            if head:
                self.store.update(
                    tx, "review", review_id, payload, head["revision"], state=wire["target_kind"]
                )
            else:
                self.store.insert(
                    tx,
                    "review",
                    review_id,
                    payload,
                    parent_id=wire["target_id"],
                    state=wire["target_kind"],
                    available_at=now,
                )
            self.store.insert(
                tx,
                "fingerprint",
                pointer_id,
                {"review_id": review_id, "revision_document_id": revision_id},
                parent_id=review_id,
                available_at=now,
            )
            return Snapshot.of(wire)

    def get_review(self, review_id, *, revision=None, scope=None):
        with nullcontext(scope) if scope is not None else self.db.read() as reader:
            kind, id = (
                ("review", review_id) if revision is None else ("revision", f"{review_id}-r{revision:08d}")
            )
            record = self.store.get(reader, kind, id)
            if record is None:
                raise DomainError("REVIEW_NOT_FOUND", "没有找到指定复盘或修订。", 404)
            return Snapshot.of(record["wire"])

    def list_reviews(
        self, *, target_kind=None, target_id=None, revisions="latest", limit=50, cursor=None, scope=None
    ):
        if target_kind not in {None, "forecast", "reference_plan", "actual_trade"} or revisions not in {
            "latest",
            "all",
        }:
            raise DomainError("REVIEW_FILTER_INVALID", "复盘筛选参数不正确。")
        if not 1 <= limit <= 100:
            raise DomainError("PAGE_SIZE_INVALID", "每页数量必须为 1 到 100。")
        with nullcontext(scope) if scope is not None else self.db.read() as reader:
            try:
                values = self.store.list(
                    reader,
                    "review" if revisions == "latest" else "revision",
                    parent_id=target_id,
                    state=target_kind,
                    limit=limit + 1,
                    cursor=cursor,
                )
            except ValueError as error:
                raise DomainError("CURSOR_INVALID", "复盘分页游标无效。") from error
            return Snapshot.of(
                {
                    "items": [item["wire"] for item in values[:limit]],
                    "next_cursor": values[limit - 1]["id"] if len(values) > limit else None,
                }
            )

    def get_context(self, cutoff, *, scope=None, include_candidates=False):
        """Select latest *available by cutoff* revision of every review identity."""
        parse_utc(cutoff)
        with nullcontext(scope) if scope is not None else self.db.read() as reader:
            latest = {}
            cursor = None
            while True:
                values = self.store.list(
                    reader, "revision", limit=1000, cursor=cursor, available_before=cutoff
                )
                for item in values:
                    review = item["wire"]
                    old = latest.get(review["review_id"])
                    if old is None or old["revision"] < review["revision"]:
                        latest[review["review_id"]] = review
                if len(values) < 1000:
                    break
                cursor = values[-1]["id"]
        groups = {}
        for kind in ("forecast", "reference_plan", "actual_trade"):
            records = [item for item in latest.values() if item["target_kind"] == kind]
            groups[kind] = self._summary(records)
        all_refs = [
            f"{item['review_id']}-r{item['revision']:08d}"
            for item in sorted(latest.values(), key=lambda item: item["review_id"])
        ]
        recent = sorted(
            latest.values(),
            key=lambda item: (parse_utc(item["available_at"]), item["review_id"]),
            reverse=True,
        )[:80]
        refs = [f"{item['review_id']}-r{item['revision']:08d}" for item in recent]
        available_at = max((item["available_at"] for item in latest.values()), key=parse_utc, default=cutoff)
        result = {
            "schema_version": "experience/2",
            "cutoff": cutoff,
            "available_at": available_at,
            "period_start": min(
                (item["window_start"] for item in latest.values()), key=parse_utc, default=None
            ),
            "period_end": max((item["window_end"] for item in latest.values()), key=parse_utc, default=None),
            "groups": groups,
            "forecast_statistics": forecast_statistics(list(latest.values())),
            "review_refs": refs,
            "review_reference_total": len(all_refs),
            "omitted_reference_count": len(all_refs) - len(refs),
            "source_set_sha256": digest(all_refs),
            "reference_policy": "latest_available_80_refs_complete_aggregate/1",
            "cases": self._cases(recent),
            "notes": [
                "分母为截止前已完成的复盘身份；各方法、版本、窗口分别计数，不把未导入请求计为预测。",
                "描述性预测、参考模拟、实际交易分别汇总；未知净值不补零。",
            ],
        }
        if include_candidates:
            # Private detached pool is consumed before a request is frozen; it is never sent to a model.
            result["_case_candidates"] = self._cases(sorted(latest.values(),
                key=lambda item: (parse_utc(item["available_at"]), item["review_id"]), reverse=True), limit=None)
        result["digest_id"] = "exp-" + digest(result)[:32]
        return Snapshot.of(result)

    @staticmethod
    def _cases(recent, limit=8):
        """Keep counterexamples, exact errors and unknowns; never infer a causal story."""
        selected = []
        seen = set()
        for wanted in ("incorrect", "correct", "unknown"):
            for item in recent:
                metric = next((m for m in item["metrics"] if m["name"] == "direction_correct"), None)
                bucket = ("correct" if metric and metric["state"] == "available" and metric.get("value") == "1"
                          else "incorrect" if metric and metric["state"] == "available" and metric.get("value") == "0"
                          else "unknown")
                if bucket == wanted and item["review_id"] not in seen:
                    selected.append(item)
                    seen.add(item["review_id"])
                    break
        selected.extend(item for item in recent if item["review_id"] not in seen)
        wanted_metrics = {"direction", "delta", "direction_correct", "actual_low", "actual_high", "lower_excess",
                          'anchor_price','anchor_at','endpoint_price','endpoint_at','endpoint_max_age_ms',
                          'phase.anchor_price','phase.anchor_at','phase.endpoint_price','phase.endpoint_at',
                          "upper_excess", "range_contained", "range_boundary_absolute_error", "predicted_low", "predicted_high",
                          "predicted_direction", "predicted_range_width", "actual_range_width", "import_delay", "turning_point_hit",
                          "phase.direction_correct", "phase.direction", "phase.delta"}
        return [{"review_ref": f"{item['review_id']}-r{item['revision']:08d}",
                 "target_id": item["target_id"], "target_kind": item["target_kind"],
                 "available_at": item["available_at"], "window_start": item["window_start"],
                 "window_end": item["window_end"], "method_version": item["method_version"],
                 "outcome": item["outcome"], "inputs": item["inputs"], "source_kind": item.get("source_kind", "unknown"),
                 "coverage": item["coverage"], "evaluated_start": item["evaluated_start"], "evaluated_end": item["evaluated_end"],
                 "reasons": item["reasons"],
                 "metrics": [m for m in item["metrics"] if m["name"] in wanted_metrics or m["name"].startswith("baseline.")],
                 "metric_selection": "direction-range-phase-errors-and-baselines/1; other metrics remain in the original review",
                 "interpretation": ("旧方法为完整M1裁剪方向，不等同冻结Bid至原端点方向命中。"
                    if item['method_version'] in {'prediction_review/1','prediction_review/2'} else '')
                    + "指标为既有客观记录；错误原因未经因果验证，不自动形成交易规则。"}
                for item in (selected if limit is None else selected[:limit])]

    def enrich_context(self, context, predictions, *, selection_target=None):
        value = deepcopy(data(context))
        pool = value.pop("_case_candidates", None)
        if selection_target is not None:
            cases, selection, eligible = select_cases(pool if pool is not None else value.get("cases", []),
                                                     predictions, selection_target, value["cutoff"])
            value["cases"], value["case_selection"] = cases, selection
            value["forecast_statistics"] = forecast_statistics(eligible)
            value["forecast_statistics"]["scope"] = "same_source_as_current_request"
            value["forecast_statistics"]["notes"].append("本表仅统计本次账户、品种与资料类型已核实一致的历史预测；三类全库总数仍见groups。")
            selected_refs = [c["review_ref"] for c in cases]
            value["review_refs"] = list(dict.fromkeys(selected_refs + value["review_refs"]))[:80]
            value["omitted_reference_count"] = value["review_reference_total"] - len(value["review_refs"])
            value["reference_policy"] = "selected_cases_plus_latest_refs_80_complete_aggregate/2"
            value["notes"].append("groups保持截止前全库三类汇总；forecast_statistics和8例为本次同源预测范围，不能用8例估算胜率。")
        elif pool is not None:
            raise DomainError("EXPERIENCE_TARGET_REQUIRED", "候选经验池须经过明确的本次周期与来源筛选后才能冻结。", 422)
        for case in value.get("cases", []):
            version_id = case["inputs"].get("forecast_version_id")
            version = predictions.get(version_id)
            if version and parse_utc(version["available_at"]) <= parse_utc(value["cutoff"]):
                horizon = next((h for h in version["answer"]["horizons"]
                                if h["horizon_minutes"] == case["inputs"].get("horizon_minutes")), None)
                summarized_horizon = None
                if horizon:
                    summarized_horizon = {**horizon, "phases": [{**p, "note": p.get("note", "")[:160]} for p in horizon["phases"]]}
                case["original_prediction"] = {"summary": version["answer"]["summary"][:600],
                                                "horizon": summarized_horizon,
                                                "summary_policy": "summary-first-600-phase-note-first-160/1; original answer preserved"}
        value.pop("digest_id", None)
        value["digest_id"] = "exp-" + digest(value)[:32]
        return Snapshot.of(value)

    @staticmethod
    def _summary(records):
        outcomes = defaultdict(int)
        methods = defaultdict(int)
        scopes = defaultdict(int)
        known_money = defaultdict(lambda: {"amount": Decimal(0), "count": 0})
        unknown = not_triggered = scorable = 0
        for record in records:
            outcomes[record["outcome"]] += 1
            methods[record["method_version"]] += 1
            scopes[record["inputs"]["evaluation_scope"]] += 1
            if record["outcome"] in {"DATA_INCOMPLETE", "AMBIGUOUS"}:
                unknown += 1
            elif record["outcome"] in {"NOT_TRIGGERED", "EXPIRED_WITHOUT_ENTRY"}:
                not_triggered += 1
            elif record["outcome"] == "EVALUATED":
                scorable += int(
                    any(
                        item["state"] == "available"
                        and item["name"] in {"direction_correct", "range_contained"}
                        for item in record["metrics"]
                    )
                )
            elif record["net_pnl"] is not None:
                scorable += 1
            else:
                unknown += 1
            if record["net_pnl"] is not None:
                money = record["net_pnl"]
                known_money[money["currency"]]["amount"] += dec(money["amount"])
                known_money[money["currency"]]["count"] += 1
        return {
            "total": len(records),
            "unique_targets": len({item["target_id"] for item in records}),
            "scorable": scorable,
            "unknown": unknown,
            "not_triggered_or_not_entered": not_triggered,
            "unscorable": len(records) - scorable - unknown - not_triggered,
            "outcomes": dict(outcomes),
            "methods": dict(methods),
            "evaluation_scopes": dict(scopes),
            "known_net_totals": {
                currency: {"amount": str(value["amount"]), "known_count": value["count"]}
                for currency, value in known_money.items()
            },
            "net_unknown_or_not_applicable": sum(item["net_pnl"] is None for item in records),
        }
