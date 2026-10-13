"""時刻ユーティリティ。DB には naive UTC で保存し、業務判断は JST で行う。

テストでは `set_now()` で現在時刻を固定できる。
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

JST = ZoneInfo("Asia/Tokyo")
_fixed: datetime | None = None


def set_now(dt: datetime | None) -> None:
    """テスト用: 現在時刻を固定する（tz 付き datetime を渡す）。None で解除。"""
    global _fixed
    _fixed = dt


def now_jst() -> datetime:
    if _fixed is not None:
        return _fixed.astimezone(JST)
    return datetime.now(JST)


def utcnow() -> datetime:
    """naive UTC（DB 保存用）。"""
    return now_jst().astimezone(timezone.utc).replace(tzinfo=None)


def jst_to_utc_naive(dt: datetime) -> datetime:
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def utc_naive_to_jst(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc).astimezone(JST)


def at_jst(d: date, hhmm: str) -> datetime:
    h, m = (int(x) for x in hhmm.split(":"))
    return datetime.combine(d, time(h, m), tzinfo=JST)


def today_jst() -> date:
    return now_jst().date()


def tomorrow_jst() -> date:
    return today_jst() + timedelta(days=1)


WEEKDAYS_JA = "月火水木金土日"


def fmt_date_ja(d: date) -> str:
    return f"{d.month}月{d.day}日({WEEKDAYS_JA[d.weekday()]})"


def fmt_dt_ja(dt: datetime | None) -> str:
    if dt is None:
        return "-"
    if dt.tzinfo is None:
        dt = utc_naive_to_jst(dt)
    return dt.astimezone(JST).strftime("%Y-%m-%d %H:%M")
