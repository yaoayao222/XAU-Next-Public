from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from uuid import uuid4


def new_id() -> str:
    return uuid4().hex


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_utc(value: str) -> datetime:
    if not value.endswith("Z"):
        raise ValueError("时间必须为以 Z 结尾的 UTC 时间")
    return datetime.fromisoformat(value)


@dataclass(frozen=True)
class Money:
    amount: Decimal
    currency: str

    def display(self) -> str:
        return str(self.amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
