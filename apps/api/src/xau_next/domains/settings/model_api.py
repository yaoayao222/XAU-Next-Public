"""Model configuration and check history, separate from trading settings and packets."""

import hashlib
from contextlib import nullcontext
from decimal import Decimal

from xau_next.platform.credentials.public import PREFIX
from xau_next.platform.model_gateway.public import PROBE_VERSION, normalize_base
from xau_next.shared.errors import DomainError
from xau_next.shared.values import new_id


def defaults():
    return {
        "enabled": False,
        "provider": "章鱼烧的小店",
        "base_url": "https://api.zyuou.com/v1",
        "protocol": "responses",
        "model_id": "gpt-6.1-sol",
        "reasoning_effort": "high",
        "output_format": "json_schema",
        "stream": False,
        "timeout_seconds": 300,
        "max_output_tokens": 12000,
        "max_response_bytes": 2000000,
        "max_answer_bytes": 2000000,
        "daily_call_limit": 10,
        "daily_budget": None,
        "input_price_per_million": None,
        "output_price_per_million": None,
        "price_currency": None,
        "price_source": None,
    }


class ModelSettingsService:
    def __init__(self, db, credentials, clock):
        self.db, self.credentials, self.clock = db, credentials, clock
        self.docs = db.documents("settings")
        with db.write() as uow:
            if not self.docs.get(uow, "model_settings", "current"):
                self.docs.insert(
                    uow, "model_settings", "current", {"values": defaults(), "credential_ref": None}
                )
            # Preserve the legacy current row/revision and credential reference.
            # No secret is read or copied into a profile.
            if not self.docs.get(uow, "model_profile_selection", "current"):
                current = self.snapshot(scope=uow)
                self.docs.insert(uow, "model_profile", "default", {
                    "name": current["values"]["provider"].strip()[:80] or "原配置",
                    "values": current["values"], "credential_ref": current["credential_ref"],
                    "config_revision": current["revision"],
                })
                self.docs.insert(uow, "model_profile_selection", "current", {"profile_id": "default"})
                self._remember_profile(uow)
                for kind in ("models", "usage", "probe"):
                    prior = self.docs.get(uow, "model_check_latest", kind)
                    if prior:
                        self._put(uow, "model_profile_check", "default/" + kind, {"result": prior["result"]})
                prior = self.docs.get(uow, "model_usage_success", "latest")
                if prior:
                    self._put(uow, "model_profile_usage", "default", {"result": prior["result"]})

    def _put(self, uow, kind, id, value):
        previous = self.docs.get(uow, kind, id)
        if previous:
            self.docs.update(uow, kind, id, value, previous["revision"])
        else:
            self.docs.insert(uow, kind, id, value)

    def _remember_profile(self, uow):
        current = self.snapshot(scope=uow)
        selected = self.docs.get(uow, "model_profile_selection", "current")["profile_id"]
        profile = self.docs.get(uow, "model_profile", selected)
        self._put(uow, "model_profile", selected, {
            "name": profile["name"], "values": current["values"],
            "credential_ref": current["credential_ref"], "config_revision": current["revision"],
        })
        self._put(uow, "model_profile_revision", str(current["revision"]), {"profile_id": selected})

    @staticmethod
    def model_ids(values):
        """Explicit saved catalogue, with legacy families at their known endpoints."""
        if 'model_ids' in values:
            return list(dict.fromkeys([values['model_id'], *values['model_ids']]))
        base=values['base_url']
        extra=(['gpt-6.1-sol','gpt-6-astra'] if base.startswith('http://127.0.0.1:') else
               ['deepseek-flash','deepseek-v4-pro'] if base=='https://api.deepseek.com/v1' else [])
        return list(dict.fromkeys([values['model_id'], *extra]))

    def profiles(self, *, scope=None):
        with nullcontext(scope) if scope else self.db.read() as tx:
            return {
                "settings_revision": self.snapshot(scope=tx)["revision"],
                "active_profile_id": self.docs.get(tx, "model_profile_selection", "current")["profile_id"],
                "items": [{"profile_id": row["id"], "name": row["name"],
                           "base_url": row["values"]["base_url"], "model_id": row["values"]["model_id"],
                           "model_ids": self.model_ids(row["values"]), "protocol":row["values"]["protocol"],
                           "enabled":row["values"]["enabled"], "reasoning_effort":row["values"]["reasoning_effort"]}
                          for row in self.docs.list(tx, "model_profile", limit=1000)],
            }

    def _assert_revision(self, expected, uow):
        if self.snapshot(scope=uow)["revision"] != expected:
            raise DomainError("REVISION_CONFLICT", "模型配置已变化，请刷新后重试。", 409)

    def create_profile(self, command, *, uow):
        self._assert_revision(command["expected_revision"], uow)
        name = command["name"].strip()
        if not name or len(name) > 80:
            raise DomainError("MODEL_PROFILE_INVALID", "请填写1至80字的配置名称。")
        if any(row["name"].casefold() == name.casefold()
               for row in self.docs.list(uow, "model_profile", limit=1000)):
            raise DomainError("MODEL_PROFILE_EXISTS", "配置名称已存在，请选择已有配置或换一个名称。", 409)
        id = new_id()
        values = self._validated_values(command["values"])
        self.docs.insert(uow, "model_profile", id, {
            "name": name, "values": values, "credential_ref": None, "config_revision": None,
        })
        if command.get('activate',True):
            self.activate_profile(id, command["expected_revision"], uow=uow)

    def activate_profile(self, id, expected, *, uow):
        self._assert_revision(expected, uow)
        profile = self.docs.get(uow, "model_profile", id)
        if not profile:
            raise DomainError("MODEL_PROFILE_NOT_FOUND", "找不到该连接配置。", 404)
        if self.docs.get(uow, "model_profile_selection", "current")["profile_id"] == id:
            return
        original_revision = profile["config_revision"]
        proof = self.docs.get(uow, "model_verified", str(original_revision)) if original_revision else None
        self.docs.update(uow, "model_settings", "current", {
            "values": profile["values"], "credential_ref": profile["credential_ref"],
        }, expected)
        self._put(uow, "model_profile_selection", "current", {"profile_id": id})
        self._remember_profile(uow)
        if proof and proof["result"]["status"] == "passed" and proof.get("validation_policy") == PROBE_VERSION:
            revision = self.snapshot(scope=uow)["revision"]
            self.docs.insert(uow, "model_verified", str(revision), {
                "result": proof["result"], "validation_policy": PROBE_VERSION,
                "reused_from_revision": original_revision, "reason": "saved_profile_unchanged_connection",
            })

    def snapshot(self, *, scope=None):
        with nullcontext(scope) if scope else self.db.read() as tx:
            row = self.docs.get(tx, "model_settings", "current")
            return {
                "revision": row["revision"],
                "values": {"max_answer_bytes": 2000000, **row["values"]},
                "credential_ref": row["credential_ref"],
            }

    def comparison_profile(self, profile_id, *, scope=None):
        """Read only saved non-secret values and this application's key reference."""
        with nullcontext(scope) if scope else self.db.read() as tx:
            row=self.docs.get(tx,'model_profile',profile_id)
            if not row:raise DomainError('COMPARISON_PROFILE_MISSING','未找到本组选择的已保存模型配置。',409)
            return {'profile_id':profile_id,'values':{'max_answer_bytes':2000000,**row['values']},
                    'credential_ref':row['credential_ref']}

    def view(self, *, scope=None):
        with nullcontext(scope) if scope else self.db.read() as tx:
            current = self.snapshot(scope=tx)
            profile_id = self.docs.get(tx, "model_profile_selection", "current")["profile_id"]
            selected = []
            for kind in ("models", "usage", "probe"):
                row = self.docs.get(tx, "model_profile_check", profile_id + "/" + kind)
                if row:
                    selected.append(row["result"])
            last = self.docs.get(tx, "model_profile_usage", profile_id)
            verified = self.verified(current, scope=tx)
        return {
            "revision": current["revision"],
            "values": current["values"],
            "key_configured": self.credentials.configured(current["credential_ref"]),
            "checks": selected,
            "last_successful_usage": last["result"] if last else None,
            "capability_verified": verified,
        }

    def verified(self, snapshot, *, scope):
        check = self.docs.get(scope, "model_verified", str(snapshot["revision"]))
        return bool(check and check["result"]["status"] == "passed"
                    and check.get("validation_policy") == PROBE_VERSION)

    def _validated_values(self, supplied):
        values = dict(supplied)
        values["base_url"] = normalize_base(values["base_url"])
        if not values["provider"].strip() or not values["model_id"].strip():
            raise DomainError("MODEL_SETTINGS_INVALID", "中转站和模型标识不能为空。")
        if values['enabled'] and values['model_id']=='待选择模型':
            raise DomainError('MODEL_SETTINGS_INVALID','先读取模型列表并选择准确模型编号，再启用分析入口。')
        for field in ("daily_budget", "input_price_per_million", "output_price_per_million"):
            if values[field] is not None and Decimal(values[field]) < 0:
                raise DomainError("MODEL_SETTINGS_INVALID", "预算和本站价格不能为负数。")
        priced = all(
            values[k] is not None
            for k in ("input_price_per_million", "output_price_per_million", "price_currency", "price_source")
        )
        if values["daily_budget"] is not None and not priced:
            raise DomainError(
                "MODEL_PRICE_UNKNOWN", "设置金额限制前，请填写本站价格、币种及价格来源；未知时使用次数限制。"
            )
        if 'model_ids' in values:
            models=values['model_ids']
            if len(models)>100 or any(not isinstance(m,str) or not m.strip() or m!=m.strip() or len(m)>200 for m in models):
                raise DomainError('MODEL_SETTINGS_INVALID','模型ID须逐字填写，不能包含首尾空格。')
            values['model_ids']=list(dict.fromkeys(models))
        return values

    def update(self, command, *, uow):
        values = self._validated_values(command["values"])
        current = self.snapshot(scope=uow)
        self.docs.update(
            uow,
            "model_settings",
            "current",
            {"values": values, "credential_ref": current["credential_ref"]},
            command["expected_revision"],
        )
        self._remember_profile(uow)
        # A budget-only edit does not change the tested protocol capability. Keep the
        # original probe identity/time/revision; this is not a new generation test.
        budget_fields = {
            "timeout_seconds",
            "max_output_tokens",
            "max_response_bytes",
            "max_answer_bytes",
            "daily_call_limit",
            "daily_budget",
            "input_price_per_million",
            "output_price_per_million",
            "price_currency",
            "price_source",
        }
        changed = {key for key in values if values[key] != current["values"].get(key)}
        previous = self.docs.get(uow, "model_verified", str(current["revision"]))
        if changed <= budget_fields and previous and previous["result"]["status"] == "passed":
            updated = self.snapshot(scope=uow)
            self.docs.insert(
                uow,
                "model_verified",
                str(updated["revision"]),
                {
                    "result": previous["result"],
                    "validation_policy": previous.get("validation_policy"),
                    "reused_from_revision": current["revision"],
                    "reason": "budget_only_change_same_credential_and_protocol",
                },
            )

    def set_credential(self, command, nonce):
        # The key never reaches idempotency bodies, documents, events, or diagnostic text.
        key = command["key"]
        if any(ord(c) < 33 or ord(c) > 126 for c in key):
            raise DomainError("MODEL_KEY_INVALID", "密钥不能含空格、换行或非 ASCII 字符。")
        fingerprint = hashlib.sha256(str(self.db.data_root).encode()).hexdigest()[:20]
        reference = PREFIX + fingerprint + "/" + hashlib.sha256(nonce.encode()).hexdigest()
        with self.db.write() as uow:
            prior = self.docs.get(uow, "model_credential_command", reference)
            if prior:
                return
            current = self.snapshot(scope=uow)
            if current["revision"] != command["expected_revision"]:
                raise DomainError("VERSION_CONFLICT", "设置已经变化，请刷新后重新录入。", 409)
            self.credentials.write(reference, key)
            self.docs.update(
                uow,
                "model_settings",
                "current",
                {"values": current["values"], "credential_ref": reference},
                current["revision"],
            )
            self.docs.insert(uow, "model_credential_command", reference, {"credential_ref": reference})
            self._remember_profile(uow)

    def record_check(self, result, *, uow, validation_policy=None):
        pointer = self.docs.get(uow, "model_profile_revision", str(result["config_revision"]))
        if pointer:
            profile_id = pointer["profile_id"]
            self._put(uow, "model_profile_check", profile_id + "/" + result["kind"], {"result": result})
            if result["kind"] == "usage" and result["status"] == "passed":
                self._put(uow, "model_profile_usage", profile_id, {"result": result})
        self.docs.insert(uow, "model_check", new_id(), {"kind": result["kind"], "result": result})
        prior = self.docs.get(uow, "model_check_latest", result["kind"])
        if prior:
            self.docs.update(uow, "model_check_latest", result["kind"], {"result": result}, prior["revision"])
        else:
            self.docs.insert(uow, "model_check_latest", result["kind"], {"result": result})
        if result["kind"] == "usage" and result["status"] == "passed":
            previous = self.docs.get(uow, "model_usage_success", "latest")
            if previous:
                self.docs.update(
                    uow, "model_usage_success", "latest", {"result": result}, previous["revision"]
                )
            else:
                self.docs.insert(uow, "model_usage_success", "latest", {"result": result})
        if result["kind"] == "probe":
            proof = {"result": result, "validation_policy": validation_policy}
            identity = str(result["config_revision"])
            previous = self.docs.get(uow, "model_verified", identity)
            if previous:
                self.docs.update(uow, "model_verified", identity, proof, previous["revision"])
            else:
                self.docs.insert(uow, "model_verified", identity, proof)
