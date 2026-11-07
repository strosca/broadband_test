"""Zeitstempel: intern immer UTC, sekundengenau und als ISO 8601 mit Zeitzone."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo


def now_utc() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def iso_utc(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="seconds")


def iso_local(moment: datetime, zone: ZoneInfo) -> str:
    return moment.astimezone(zone).isoformat(timespec="seconds")


def parse_iso(text: str) -> datetime:
    return datetime.fromisoformat(text)


def parse_user_time(text: str, zone: ZoneInfo, *, end_of_day: bool = False) -> datetime:
    """Liest Datum oder Datum mit Uhrzeit in lokaler Zeit.

    Mit end_of_day zählt ein reines Datum bis zum Ende dieses Tages, damit
    `--to 2026-11-30` den 30. November einschließt.
    """
    try:
        day = date.fromisoformat(text)
    except ValueError:
        moment = datetime.fromisoformat(text)
        return moment.replace(tzinfo=zone) if moment.tzinfo is None else moment
    if end_of_day:
        day += timedelta(days=1)
    return datetime(day.year, day.month, day.day, tzinfo=zone)


def format_local(moment: datetime, zone: ZoneInfo) -> str:
    return moment.astimezone(zone).strftime("%d.%m.%Y %H:%M:%S")


def format_duration(seconds: float) -> str:
    total = int(round(seconds))
    days, rest = divmod(total, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    prefix = f"{days} d " if days else ""
    return f"{prefix}{hours:02d}:{minutes:02d}:{secs:02d}"
