"""Timestamps: RFC 3339, UTC, exactly millisecond precision, Z suffix (19 §1.2)."""

from datetime import datetime, timezone
import re

_TS = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z")


class TimeError(ValueError):
    """A timestamp is not in the model's form."""


def format_ts(dt: datetime) -> str:
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise TimeError("naive datetimes are not allowed; supply a timezone")
    u = dt.astimezone(timezone.utc)
    return f"{u:%Y-%m-%dT%H:%M:%S}.{u.microsecond // 1000:03d}Z"


def parse_ts(text: str) -> datetime:
    if not isinstance(text, str) or not _TS.fullmatch(text):
        raise TimeError(f"not a model timestamp: {text!r}")
    try:
        return datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError as e:
        raise TimeError(str(e)) from None
