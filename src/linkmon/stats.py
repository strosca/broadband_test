"""Kennzahlen für Bericht und Webinterface: überwachte Zeit, Ausfälle, Verfügbarkeit."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

from linkmon.levels import LEVEL_ORDER, Level
from linkmon.quality import INTERNET, TrackStats, all_track_stats, day_table, find_track
from linkmon.store import Store
from linkmon.timeutil import parse_iso


@dataclass(frozen=True)
class Outage:
    level: str
    detail: str
    start: datetime
    end: datetime
    confirmed: bool
    ongoing: bool = False

    @property
    def duration_s(self) -> float:
        return (self.end - self.start).total_seconds()


@dataclass(frozen=True)
class Coverage:
    """Wann das Tool tatsächlich hingesehen hat. Nur diese Zeit zählt für die Verfügbarkeit."""

    monitored_s: float
    gaps: list[tuple[datetime, datetime]]
    monitored_by_day: dict[str, float] = field(default_factory=dict)
    cycles: int = 0


@dataclass(frozen=True)
class DayRow:
    day: str
    monitored_s: float
    outages: int
    line_outages: int
    line_downtime_s: float
    availability_pct: float | None
    ping_p50: float | None
    ping_p95: float | None
    loss_pct: float | None


@dataclass(frozen=True)
class Summary:
    start: datetime
    end: datetime
    period_s: float
    monitored_s: float
    coverage_pct: float | None
    availability_pct: float | None
    line_downtime_s: float
    outage_count: dict[str, int]
    outage_duration_s: dict[str, float]
    tracks: list[TrackStats]
    days: list[DayRow]

    def track(self, key: str) -> TrackStats | None:
        return next((item for item in self.tracks if item.track.key == key), None)


def load_outages(store: Store, start: datetime, end: datetime, *, now: datetime | None = None) -> list[Outage]:
    """Lädt Ausfälle und schneidet sie auf den Zeitraum zu. Laufende enden beim letzten Herzschlag."""
    rows = store.outages(start, end)
    start = start.astimezone(UTC)
    end = end.astimezone(UTC)
    last = store.last_cycle_time()
    fallback = parse_iso(last) if last else (now or datetime.now(UTC))
    outages: list[Outage] = []
    for row in rows:
        ongoing = row["end_utc"] is None
        raw_end = fallback if ongoing else parse_iso(row["end_utc"])
        begin = max(parse_iso(row["start_utc"]), start)
        finish = min(raw_end, end)
        if finish < begin:
            finish = begin
        outages.append(Outage(row["level"], row["detail"] or "", begin, finish, bool(row["end_confirmed"]), ongoing))
    return outages


def compute_coverage(timestamps: list[datetime], start: datetime, end: datetime, gap_threshold_s: float,
                     zone: ZoneInfo) -> Coverage:
    """Summiert die überwachte Zeit aus den Herzschlägen und findet Messlücken."""
    start = start.astimezone(UTC)
    end = end.astimezone(UTC)
    gaps: list[tuple[datetime, datetime]] = []
    by_day: dict[str, float] = {}
    if not timestamps:
        return Coverage(0.0, [(start, end)], {}, 0)
    monitored = 0.0
    if (timestamps[0] - start).total_seconds() > gap_threshold_s:
        gaps.append((start, timestamps[0]))
    for current, following in zip(timestamps, timestamps[1:]):
        delta = (following - current).total_seconds()
        if delta > gap_threshold_s:
            gaps.append((current, following))
        else:
            monitored += delta
            day = current.astimezone(zone).strftime("%Y-%m-%d")
            by_day[day] = by_day.get(day, 0.0) + delta
    if (end - timestamps[-1]).total_seconds() > gap_threshold_s:
        gaps.append((timestamps[-1], end))
    return Coverage(monitored, gaps, by_day, len(timestamps))


def load_coverage(store: Store, start: datetime, end: datetime, gap_threshold_s: float, zone: ZoneInfo) -> Coverage:
    stamps = [parse_iso(stamp) for stamp in store.cycle_times(start, end)]
    return compute_coverage(stamps, start, end, gap_threshold_s, zone)


def merge_intervals(intervals: list[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    merged: list[tuple[datetime, datetime]] = []
    for begin, finish in sorted(intervals):
        if merged and begin <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], finish))
        else:
            merged.append((begin, finish))
    return merged


def total_seconds(intervals: list[tuple[datetime, datetime]]) -> float:
    return sum((finish - begin).total_seconds() for begin, finish in merge_intervals(intervals))


def split_by_day(begin: datetime, finish: datetime, zone: ZoneInfo) -> dict[str, float]:
    """Verteilt ein Zeitintervall auf lokale Kalendertage.

    Gerechnet wird in UTC: Python subtrahiert Zeiten derselben Zone nach Wanduhr, sonst
    fehlte am Tag der Zeitumstellung eine Stunde.
    """
    result: dict[str, float] = {}
    cursor = begin.astimezone(UTC)
    finish = finish.astimezone(UTC)
    while cursor < finish:
        local = cursor.astimezone(zone)
        following_day = local.date() + timedelta(days=1)
        midnight = datetime(following_day.year, following_day.month, following_day.day, tzinfo=zone)
        step_end = min(midnight.astimezone(UTC), finish)
        key = local.strftime("%Y-%m-%d")
        result[key] = result.get(key, 0.0) + (step_end - cursor).total_seconds()
        cursor = step_end
    return result


def availability(monitored_s: float, downtime_s: float) -> float | None:
    if monitored_s <= 0:
        return None
    return max(0.0, 100.0 * (1 - downtime_s / monitored_s))


def compute_summary(samples: pd.DataFrame, outages: list[Outage], coverage: Coverage, start: datetime,
                    end: datetime, zone: ZoneInfo) -> Summary:
    line_downtime = total_seconds([(o.start, o.end) for o in outages if o.level == Level.LINE])
    outage_count = {level: 0 for level in LEVEL_ORDER}
    duration_lists: dict[str, list[tuple[datetime, datetime]]] = {level: [] for level in LEVEL_ORDER}
    for outage in outages:
        outage_count[outage.level] = outage_count.get(outage.level, 0) + 1
        duration_lists.setdefault(outage.level, []).append((outage.start, outage.end))
    period = (end.astimezone(UTC) - start.astimezone(UTC)).total_seconds()
    return Summary(
        start=start,
        end=end,
        period_s=period,
        monitored_s=coverage.monitored_s,
        coverage_pct=100 * coverage.monitored_s / period if period > 0 and coverage.cycles else None,
        availability_pct=availability(coverage.monitored_s, line_downtime),
        line_downtime_s=line_downtime,
        outage_count=outage_count,
        outage_duration_s={level: total_seconds(items) for level, items in duration_lists.items()},
        tracks=all_track_stats(samples),
        days=compute_days(samples, outages, coverage, zone),
    )


def compute_days(samples: pd.DataFrame, outages: list[Outage], coverage: Coverage, zone: ZoneInfo) -> list[DayRow]:
    down_by_day: dict[str, float] = {}
    for begin, finish in merge_intervals([(o.start, o.end) for o in outages if o.level == Level.LINE]):
        for day, seconds in split_by_day(begin, finish, zone).items():
            down_by_day[day] = down_by_day.get(day, 0.0) + seconds
    starts: dict[str, int] = {}
    line_starts: dict[str, int] = {}
    for outage in outages:
        day = outage.start.astimezone(zone).strftime("%Y-%m-%d")
        starts[day] = starts.get(day, 0) + 1
        if outage.level == Level.LINE:
            line_starts[day] = line_starts.get(day, 0) + 1
    internet = find_track(samples, INTERNET)
    quality = day_table(samples, internet, zone) if internet else pd.DataFrame()

    rows: list[DayRow] = []
    for day in sorted(set(coverage.monitored_by_day) | set(down_by_day) | set(starts)):
        monitored = coverage.monitored_by_day.get(day, 0.0)
        downtime = down_by_day.get(day, 0.0)
        values = quality.loc[day] if day in quality.index else None
        rows.append(DayRow(
            day=day,
            monitored_s=monitored,
            outages=starts.get(day, 0),
            line_outages=line_starts.get(day, 0),
            line_downtime_s=downtime,
            availability_pct=availability(monitored, downtime),
            ping_p50=_value(values, "median"),
            ping_p95=_value(values, "p95"),
            loss_pct=_value(values, "loss_pct"),
        ))
    return rows


def _value(row: pd.Series | None, column: str) -> float | None:
    if row is None or pd.isna(row[column]):
        return None
    return float(row[column])


def is_number(value: float | None) -> bool:
    return value is not None and not math.isnan(value)
