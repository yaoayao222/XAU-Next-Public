"""Explicit interpretation of broker wall-clock encoded epochs; never inferred from price ticks."""

import re
from datetime import UTC, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .protocol import BridgeFailure, stamp


class Timebase:
    def __init__(self, configured: str | None):
        self.configured = configured
        self.zone = UTC
        if configured in {None, "UTC", "Etc/UTC"}:
            return
        fixed = re.fullmatch(r"UTC([+-])(\d{2}):(\d{2})", configured)
        if fixed:
            hours, minutes = int(fixed.group(2)), int(fixed.group(3))
            if hours > 14 or minutes > 59:
                raise BridgeFailure("BROKER_TIMEZONE_INVALID", "服务器时区偏移无效")
            offset = timedelta(hours=hours, minutes=minutes) * (1 if fixed.group(1) == "+" else -1)
            self.zone = timezone(offset)
        else:
            try:
                self.zone = ZoneInfo(configured)
            except ZoneInfoNotFoundError as exc:
                raise BridgeFailure(
                    "BROKER_TIMEZONE_INVALID", "服务器时区不可用，请使用有效时区或 UTC±HH:MM"
                ) from exc

    def event_time(self, seconds: float) -> str:
        raw = datetime.fromtimestamp(seconds, UTC)
        local = raw.replace(tzinfo=self.zone)
        if local.replace(fold=0).utcoffset() != local.replace(fold=1).utcoffset():
            raise BridgeFailure("TIMEBASE_AMBIGUOUS", "服务器时间位于夏令时切换歧义区间，未猜测先后")
        return stamp(local.astimezone(UTC))

    def query_time(self, instant: datetime) -> datetime:
        return instant.astimezone(self.zone).replace(tzinfo=UTC)
