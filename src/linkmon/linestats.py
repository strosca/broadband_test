"""Kennzahlen nur für Ausfälle der Leitung, also der Strecke, für die der Anbieter verantwortlich ist.

Reine Rechenlogik ohne Dateizugriff: Bericht und Tests nutzen sie gleichermaßen.
"""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from linkmon.levels import Level
from linkmon.stats import Outage, availability, compute_coverage, merge_intervals

# Obergrenzen in Sekunden. Die Grenzen liegen so, wie man über Ausfälle spricht.
DURATION_CLASSES: list[tuple[str, float]] = [
    ("unter 1 Minute", 60),
    ("1 bis 5 Minuten", 300),
    ("5 bis 30 Minuten", 1800),
    ("30 Minuten bis 2 Stunden", 7200),
    ("über 2 Stunden", float("inf")),
]

DAYTIME_BLOCKS: list[tuple[str, int, int]] = [
    ("Nacht (0 bis 6 Uhr)", 0, 6),
    ("Vormittag (6 bis 12 Uhr)", 6, 12),
    ("Nachmittag (12 bis 18 Uhr)", 12, 18),
    ("Abend (18 bis 24 Uhr)", 18, 24),
]

ROLLING_PERIODS: list[tuple[str, timedelta]] = [
    ("Letzte 24 Stunden", timedelta(hours=24)),
    ("Letzte 7 Tage", timedelta(days=7)),
    ("Letzte 30 Tage", timedelta(days=30)),
]


def line_outages(outages: list[Outage]) -> list[Outage]:
    return sorted((outage for outage in outages if outage.level == Level.LINE), key=lambda outage: outage.start)


@dataclass(frozen=True)
class PeriodStats:
    label: str
    start: datetime
    end: datetime
    monitored_s: float
    coverage_pct: float | None
    count: int
    downtime_s: float
    longest: Outage | None
    availability_pct: float | None

    @property
    def longest_s(self) -> float:
        return self.longest.duration_s if self.longest else 0.0


@dataclass(frozen=True)
class ClassRow:
    label: str
    count: int
    downtime_s: float


def _clip(outage: Outage, start: datetime, end: datetime) -> Outage | None:
    begin = max(outage.start, start)
    finish = min(outage.end, end)
    if finish <= begin and not (outage.start == outage.end and start <= outage.start < end):
        return None
    return Outage(outage.level, outage.detail, begin, max(finish, begin), outage.confirmed, outage.ongoing)


class LineStatistics:
    """Rechnet Leitungsausfälle und überwachte Zeit für beliebige Teilzeiträume aus."""

    def __init__(self, outages: list[Outage], stamps: list[datetime], gap_threshold_s: float, zone: ZoneInfo) -> None:
        self.outages = line_outages(outages)
        self.stamps = sorted(moment.astimezone(UTC) for moment in stamps)
        self.gap_threshold_s = gap_threshold_s
        self.zone = zone

    def period(self, label: str, start: datetime, end: datetime) -> PeriodStats:
        start = start.astimezone(UTC)
        end = end.astimezone(UTC)
        low = bisect_left(self.stamps, start)
        high = bisect_left(self.stamps, end)
        coverage = compute_coverage(self.stamps[low:high], start, end, self.gap_threshold_s, self.zone)
        clipped = [item for item in (_clip(outage, start, end) for outage in self.outages) if item is not None]
        downtime = sum((finish - begin).total_seconds()
                       for begin, finish in merge_intervals([(item.start, item.end) for item in clipped]))
        span = (end - start).total_seconds()
        return PeriodStats(
            label=label,
            start=start,
            end=end,
            monitored_s=coverage.monitored_s,
            coverage_pct=100 * coverage.monitored_s / span if span > 0 and coverage.cycles else None,
            count=len(clipped),
            downtime_s=downtime,
            longest=max(clipped, key=lambda item: item.duration_s, default=None),
            availability_pct=availability(coverage.monitored_s, downtime),
        )

    def rolling(self, start: datetime, end: datetime) -> list[PeriodStats]:
        """Die letzten 24 h, 7 und 30 Tage bis zum Ende des Zeitraums, dazu der ganze Zeitraum.

        Ein gleitender Zeitraum erscheint nur, wenn er kürzer als der ganze Berichtszeitraum ist.
        """
        span = end - start
        rows = [self.period(label, end - length, end) for label, length in ROLLING_PERIODS if length < span]
        rows.append(self.period("Gesamter Zeitraum", start, end))
        return rows

    def weeks(self, start: datetime, end: datetime) -> list[PeriodStats]:
        """Kalenderwochen von Montag bis Sonntag in lokaler Zeit, am Rand auf den Zeitraum gekürzt."""
        rows: list[PeriodStats] = []
        local = start.astimezone(self.zone)
        monday = local.date() - timedelta(days=local.weekday())
        while True:
            week_start = datetime(monday.year, monday.month, monday.day, tzinfo=self.zone)
            following = monday + timedelta(days=7)
            week_end = datetime(following.year, following.month, following.day, tzinfo=self.zone)
            if week_start >= end:
                break
            year, number, _ = monday.isocalendar()
            rows.append(self.period(f"KW {number:02d}/{year}", max(week_start, start), min(week_end, end)))
            monday = following
        return rows

    def days_with_outages(self) -> list[PeriodStats]:
        """Nur Tage, an denen die Leitung ausfiel. Gezählt wird ein Ausfall an jedem Tag, den er berührt."""
        days: set[date] = set()
        for outage in self.outages:
            cursor = outage.start.astimezone(self.zone).date()
            last = outage.end.astimezone(self.zone).date()
            while cursor <= last:
                days.add(cursor)
                cursor += timedelta(days=1)
        rows = []
        for day in sorted(days):
            following = day + timedelta(days=1)
            rows.append(self.period(f"{day:%d.%m.%Y}", datetime(day.year, day.month, day.day, tzinfo=self.zone),
                                    datetime(following.year, following.month, following.day, tzinfo=self.zone)))
        return [row for row in rows if row.count]

    def by_duration(self) -> list[ClassRow]:
        rows = []
        lower = 0.0
        for label, upper in DURATION_CLASSES:
            items = [outage for outage in self.outages if lower <= outage.duration_s < upper]
            rows.append(ClassRow(label, len(items), sum(outage.duration_s for outage in items)))
            lower = upper
        return rows

    def by_daytime(self) -> list[ClassRow]:
        """Nach der Uhrzeit des Beginns. So fallen etwa Ausfälle zur abendlichen Hauptlast auf."""
        rows = []
        for label, first, last in DAYTIME_BLOCKS:
            items = [outage for outage in self.outages if first <= outage.start.astimezone(self.zone).hour < last]
            rows.append(ClassRow(label, len(items), sum(outage.duration_s for outage in items)))
        return rows


def plain_duration(seconds: float) -> str:
    """Dauer zum Vorlesen: „1 Std. 5 Min.“ statt „01:05:00“."""
    total = int(round(seconds))
    if total < 60:
        return f"{total} Sek."
    days, rest = divmod(total, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    parts = []
    if days:
        parts.append(f"{days} {'Tag' if days == 1 else 'Tage'}")
    if hours:
        parts.append(f"{hours} Std.")
    if minutes:
        parts.append(f"{minutes} Min.")
    if secs and not days and not hours:
        parts.append(f"{secs} Sek.")
    return " ".join(parts)
