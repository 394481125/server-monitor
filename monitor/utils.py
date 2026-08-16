from __future__ import annotations

import json
import math
import re
from datetime import UTC, datetime, timedelta
from typing import Any


def utc_now() -> datetime:
    return datetime.now(UTC)


def utc_iso(value: datetime | None = None) -> str:
    return (value or utc_now()).astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def future_iso(**kwargs: float) -> str:
    return utc_iso(utc_now() + timedelta(**kwargs))


def json_load(value: str | None, default: Any = None) -> Any:
    if value is None:
        return default
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


def json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def clamp_page(value: Any, default: int = 1) -> int:
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return default


def clamp_page_size(value: Any, default: int = 20, maximum: int = 200) -> int:
    try:
        return min(maximum, max(1, int(value)))
    except (TypeError, ValueError):
        return default


def paged(total: int, page: int, page_size: int, items: list[Any]) -> dict[str, Any]:
    return {
        "items": items,
        "page": page,
        "page_size": page_size,
        "total": total,
        "pages": math.ceil(total / page_size) if total else 0,
    }


def command_summary(command: str, limit: int = 160) -> str:
    compact = re.sub(r"\s+", " ", command).strip()
    return compact if len(compact) <= limit else compact[: limit - 1] + "..."
