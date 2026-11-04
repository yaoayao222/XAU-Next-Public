"""Plans own eligibility and estimates, never broker positions or execution."""

from contextlib import nullcontext

from xau_next.shared.errors import DomainError
from xau_next.shared.snapshots import Snapshot
from xau_next.shared.values import new_id, utc_now

from .advice import entry_advice, position_advice, rule_check, rule_now
from .calculation import calculate
from .lifecycle import TERMINAL, advance

FIELDS = (
    "plan_id",
    "version_id",
    "available_at",
    "lifecycle",
    "lifecycle_at",
    "definition",
    "revision",
    "manual_observations",
    "activation_observed_at",
)
MARKET_HEADER_FIELDS = (
    "snapshot_id",
    "source_kind",
    "account_scope_id",
    "symbol",
    "market_cutoff",
    "captured_at",
    "quote_status",
    "quote",
    "contract_status",
    "contract",
    "revision",
)
EVALUATION_INPUT_FIELDS = (
    "wire",
    "input",
    "plan_snapshot",
    "portfolio_snapshot",
    "settings_snapshot",
)
EVALUATION_STORAGE_FORMAT = "plan-evaluation/2"


def data(value):
    return value.to_dict() if isinstance(value, Snapshot) else value


def public_record(value):
    result = {k: value.get(k) if k == "activation_observed_at" else value[k] for k in FIELDS}
    for key in ("entry_timing", "invalidation_phase", "scalp_preferences", "scalp_suitability"):
        if key in value:
            result[key] = value[key]
    return result


def _market_inputs(market):
    """Freeze calculation facts and name the immutable full snapshot, without copying its series."""
    header = {key: market[key] for key in MARKET_HEADER_FIELDS if key in market}
    reference = {
        "domain": "market",
        "kind": "snapshot",
        **{
            key: market.get(key)
            for key in ("snapshot_id", "revision", "account_scope_id", "symbol", "source_kind")
        },
    }
    return {"market_snapshot_ref": reference, "market_snapshot_header": header}


class PlansService:
    def __init__(self, db, clock=utc_now):
        self.db, self.clock, self.store = db, clock, db.documents("plans")

    entry_advice = staticmethod(entry_advice)
    position_advice = staticmethod(position_advice)
    rule_now = staticmethod(rule_now)
    rule_check = staticmethod(rule_check)

    def last_action(self, plan_id, *, scope):
        row = self.store.get(scope, "action_advice", plan_id)
        return row["wire"] if row else None

    def remember_action(self, advice, *, uow):
        """Append a revision only when the decision changes, not on every quote tick."""
        row = self.store.get(uow, "action_advice", advice["plan_id"])
        old = row["wire"] if row else None
        if old and (old["state"], old["reasons"]) == (advice["state"], advice["reasons"]):
            return old, False, row["revision"]
        payload = {"wire": advice}
        if row:
            self.store.update(uow, "action_advice", advice["plan_id"], payload, row["revision"], state=advice["state"])
        else:
            self.store.insert(uow, "action_advice", advice["plan_id"], payload, parent_id=advice["version_id"], state=advice["state"])
        return old, True, row["revision"] + 1 if row else 1

    def register(self, drafts, *, uow, market_identity=None):
        result = []
        for item in drafts:
            draft = data(item)
            id = draft["plan_id"]
            existing = self.store.get(uow, "plan", id)
            if existing:
                result.append(Snapshot.of(public_record(existing)))
                continue
            value = {
                "plan_id": id,
                "version_id": draft["version_id"],
                "available_at": draft["available_at"],
                "definition": draft["definition"],
                "lifecycle": "WAITING_TRIGGER",
                "lifecycle_at": draft["available_at"],
                "manual_observations": [],
                "activation_observed_at": None,
            }
            if market_identity:
                value["account_scope_id"] = market_identity["account_scope_id"]
                value["symbol"] = market_identity["symbol"]
            if "entry_timing" in draft:
                value["entry_timing"] = draft["entry_timing"]
            for key in ("scalp_preferences", "scalp_suitability"):
                if key in draft:
                    value[key] = draft[key]
            value = advance(value, None, self.clock())
            saved = self.store.insert(
                uow,
                "plan",
                id,
                value,
                parent_id=value["version_id"],
                state=value["lifecycle"],
                available_at=value["available_at"],
            )
            result.append(Snapshot.of(public_record(saved)))
        return tuple(result)

    def get_plan(self, plan_id, *, scope=None):
        with nullcontext(scope) if scope is not None else self.db.read() as reader:
            value = self.store.get(reader, "plan", plan_id)
            if value is None:
                raise DomainError("PLAN_NOT_FOUND", "没有找到指定方案。", 404)
            return Snapshot.of(public_record(value))

    def list_plans(self, version_id=None, *, scope=None, limit=1000):
        with nullcontext(scope) if scope is not None else self.db.read() as reader:
            values = self.store.list(reader, "plan", parent_id=version_id, limit=limit)
            return tuple(Snapshot.of(public_record(v)) for v in values)

    def supersede(self, version_id, *, uow, at=None):
        for item in self.list_plans(version_id, scope=uow):
            value = item.to_dict()
            if value["lifecycle"] not in TERMINAL:
                value.update(lifecycle="SUPERSEDED", lifecycle_at=at or self.clock())
                existing = self.store.get(uow, "plan", value["plan_id"])
                self.store.update(
                    uow,
                    "plan",
                    value["plan_id"],
                    {**existing, **value},
                    value["revision"],
                    state="SUPERSEDED",
                )

    def refresh_lifecycles(self, market, *, uow, now=None, only_changed=False):
        market = data(market)
        output = []
        # Only live heads need scanning. Each state has an indexed database query.
        for state in ("WAITING_TRIGGER", "ACTIVE"):
            for value in self.store.list(uow, "plan", state=state, limit=1000):
                if (
                    market
                    and value.get("account_scope_id")
                    and (
                        value["account_scope_id"] != market["account_scope_id"]
                        or value["symbol"] != market["symbol"]
                    )
                ):
                    continue
                changed = advance(value, market, now or self.clock())
                did_change = (
                    changed["lifecycle"] != value["lifecycle"]
                    or changed["lifecycle_at"] != value["lifecycle_at"]
                )
                if did_change:
                    changed = self.store.update(
                        uow, "plan", value["plan_id"], changed, value["revision"], state=changed["lifecycle"]
                    )
                if did_change or not only_changed:
                    output.append(Snapshot.of(public_record(changed)))
        return tuple(output)

    def confirm_observation(self, plan_id, command, *, uow=None):
        with nullcontext(uow) if uow is not None else self.db.write() as writer:
            record = self.get_plan(plan_id, scope=writer).to_dict()
            if record["revision"] != command["expected_plan_revision"]:
                raise DomainError("VERSION_CONFLICT", "方案已变化，请刷新后重新确认。", 409)
            role = command["condition_role"]
            if record["definition"][role]["kind"] != "manual_observation":
                raise DomainError("CONDITION_NOT_MANUAL", "这个条件由结构化行情识别，不接受人工替代确认。")
            if record["lifecycle"] in TERMINAL:
                raise DomainError("PLAN_NOT_ACTIVE", "方案已终止，不能追加生效条件。", 409)
            observation = {
                "observation_id": new_id(),
                "plan_id": plan_id,
                "condition_role": role,
                "recorded_at": self.clock(),
                "note": command["note"],
            }
            record["manual_observations"].append(observation)
            changed = advance(record, None, self.clock())
            existing = self.store.get(writer, "plan", plan_id)
            self.store.update(
                writer,
                "plan",
                plan_id,
                {**existing, **changed},
                record["revision"],
                state=changed["lifecycle"],
            )
            return Snapshot.of(observation)

    def preview_evaluation(self, plan_id, command, *, market, portfolio, settings, scope=None):
        record = self.get_plan(plan_id, scope=scope).to_dict()
        return Snapshot.of(
            calculate(
                record, data(market), data(portfolio), data(settings), command, evaluated_at=self.clock()
            )
        )

    def evaluate_plan(self, plan_id, command, *, market, portfolio, settings, margin_fact=None, uow=None):
        market, portfolio, settings = data(market), data(portfolio), data(settings)
        with nullcontext(uow) if uow is not None else self.db.write() as writer:
            plan = self.get_plan(plan_id, scope=writer).to_dict()
            internal = self.store.get(writer, "plan", plan_id)
            if internal.get("account_scope_id") and (
                internal["account_scope_id"] != market["account_scope_id"]
                or internal["symbol"] != market["symbol"]
            ):
                raise DomainError("ACCOUNT_MISMATCH", "该方案与当前行情不属于同一账户来源和品种。", 409)
            if portfolio and portfolio["account_scope_id"] != market["account_scope_id"]:
                raise DomainError("ACCOUNT_MISMATCH", "金额评估的账户事实与行情来源不一致。", 409)
            if (
                command["market_snapshot_id"] != market["snapshot_id"]
                or command["settings_revision"] != settings["revision"]
            ):
                raise DomainError("INPUT_IDENTITY_MISMATCH", "评估输入与指定快照或配置版本不一致。", 409)
            at = self.clock()
            refreshed = advance(plan, market, at)
            if refreshed["lifecycle"] != plan["lifecycle"]:
                plan = self.store.update(
                    writer,
                    "plan",
                    plan_id,
                    {**internal, **refreshed},
                    plan["revision"],
                    state=refreshed["lifecycle"],
                )
            result = {
                "evaluation_id": new_id(),
                "plan_id": plan_id,
                "market_snapshot_id": market["snapshot_id"],
                "portfolio_snapshot_id": portfolio["snapshot_id"] if portfolio else None,
                "settings_revision": settings["revision"],
                "evaluated_at": at,
                **calculate(
                    plan, market, portfolio, settings, command, evaluated_at=at, margin_fact=margin_fact
                ),
            }
            self.store.insert(
                writer,
                "evaluation",
                result["evaluation_id"],
                {
                    "storage_format": EVALUATION_STORAGE_FORMAT,
                    "wire": result,
                    "settings_snapshot": settings,
                    **_market_inputs(market),
                    "portfolio_snapshot": portfolio,
                    "plan_snapshot": plan,
                    "input": command,
                },
                parent_id=plan_id,
                available_at=at,
            )
            return Snapshot.of(result)

    def get_evaluation_inputs(self, evaluation_id, *, scope=None):
        """Return detached saved inputs; only the workflow resolves the full market reference.

        Legacy inline-market records are projected at read time. This operation never
        edits their payloads, revisions, original snapshots or recorded timestamps.
        """
        with nullcontext(scope) if scope is not None else self.db.read() as reader:
            record = self.store.get(reader, "evaluation", evaluation_id)
            if record is None:
                raise DomainError("EVALUATION_NOT_FOUND", "没有找到指定金额评估。", 404)
            if "market_snapshot_header" in record and "market_snapshot_ref" in record:
                frozen_market = {
                    "market_snapshot_header": record["market_snapshot_header"],
                    "market_snapshot_ref": record["market_snapshot_ref"],
                }
                stored_format = record.get("storage_format", EVALUATION_STORAGE_FORMAT)
            elif isinstance(record.get("market_snapshot"), dict):
                frozen_market = _market_inputs(record["market_snapshot"])
                stored_format = "plan-evaluation/1"
            else:
                raise DomainError("EVALUATION_INPUTS_UNAVAILABLE", "该评估缺少已保存的行情输入与引用。", 409)
            return Snapshot.of(
                {
                    "storage_format": stored_format,
                    **{key: record[key] for key in EVALUATION_INPUT_FIELDS},
                    **frozen_market,
                }
            )

    def list_evaluations(self, plan_id, *, scope=None, limit=50, cursor=None):
        with nullcontext(scope) if scope is not None else self.db.read() as reader:
            values = self.store.list(reader, "evaluation", parent_id=plan_id, limit=limit + 1, cursor=cursor)
            return Snapshot.of(
                {
                    "items": [v["wire"] for v in values[:limit]],
                    "next_cursor": values[limit - 1]["id"] if len(values) > limit else None,
                }
            )
