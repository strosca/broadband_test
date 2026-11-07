from __future__ import annotations

import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from conftest import results, utc

from linkmon.config import Config
from linkmon.quality import INTERNET, parse_cycles
from linkmon.stats import (
    Coverage,
    Outage,
    compute_coverage,
    compute_summary,
    load_coverage,
    load_outages,
    merge_intervals,
    split_by_day,
    total_seconds,
)
from linkmon.store import Store

BERLIN = ZoneInfo("Europe/Berlin")


def test_coverage_counts_only_monitored_time_and_reports_gaps() -> None:
    start = utc(2026, 11, 1, 10)
    stamps = [start + timedelta(seconds=3 * i) for i in range(101)]  # 300 s überwacht
    after_gap = stamps[-1] + timedelta(hours=1)
    stamps += [after_gap + timedelta(seconds=3 * i) for i in range(101)]  # nochmals 300 s
    end = stamps[-1]
    coverage = compute_coverage(stamps, start, end, 30, BERLIN)
    assert coverage.monitored_s == 600
    assert coverage.gaps == [(start + timedelta(seconds=300), after_gap)]


def test_coverage_marks_leading_and_trailing_gaps() -> None:
    start = utc(2026, 11, 1, 10)
    stamps = [start + timedelta(hours=1), start + timedelta(hours=1, seconds=3)]
    coverage = compute_coverage(stamps, start, start + timedelta(hours=3), 30, BERLIN)
    assert coverage.gaps[0] == (start, stamps[0])
    assert coverage.gaps[-1] == (stamps[-1], start + timedelta(hours=3))


def test_no_heartbeats_means_no_coverage() -> None:
    start = utc(2026, 11, 1)
    coverage = compute_coverage([], start, start + timedelta(days=1), 30, BERLIN)
    assert coverage.monitored_s == 0 and coverage.cycles == 0


def test_merge_overlapping_intervals() -> None:
    t = utc(2026, 11, 1)
    intervals = [(t, t + timedelta(minutes=10)), (t + timedelta(minutes=5), t + timedelta(minutes=15)),
                 (t + timedelta(hours=1), t + timedelta(hours=2))]
    assert len(merge_intervals(intervals)) == 2
    assert total_seconds(intervals) == 15 * 60 + 3600


def test_split_by_day_uses_local_midnight() -> None:
    begin = datetime(2026, 11, 1, 23, 30, tzinfo=BERLIN)
    days = split_by_day(begin, begin + timedelta(hours=1), BERLIN)
    assert days == {"2026-11-01": 1800.0, "2026-11-02": 1800.0}


def test_split_by_day_on_daylight_saving_change() -> None:
    begin = datetime(2026, 10, 25, 0, 0, tzinfo=BERLIN)  # Dieser Tag hat 25 Stunden.
    end = datetime(2026, 10, 26, 0, 0, tzinfo=BERLIN)
    assert split_by_day(begin, end, BERLIN) == {"2026-10-25": 25 * 3600.0}


def _summary(outages: list[Outage], monitored: float = 3600.0):
    start = utc(2026, 11, 1, 10)
    end = start + timedelta(hours=1)
    rows = [{"ts_utc": (start + timedelta(minutes=minute)).isoformat(), "results": json.dumps(results(ext_a=value))}
            for minute, value in ((5, 20.0), (6, None), (7, 40.0))]
    coverage = Coverage(monitored, [], {"2026-11-01": monitored}, 1200)
    return compute_summary(parse_cycles(rows), outages, coverage, start, end, BERLIN)


def test_availability_counts_only_line_outages() -> None:
    start = utc(2026, 11, 1, 10)
    outages = [
        Outage("line", "", start, start + timedelta(minutes=6), True),
        Outage("router", "", start + timedelta(minutes=10), start + timedelta(minutes=40), True),
        Outage("dns", "", start + timedelta(minutes=45), start + timedelta(minutes=50), True),
    ]
    summary = _summary(outages)
    assert summary.availability_pct == pytest.approx(90.0)
    assert summary.line_downtime_s == 360
    assert summary.outage_count["line"] == 1
    assert summary.outage_count["router"] == 1
    assert summary.outage_duration_s["router"] == 1800


def test_availability_is_unknown_without_monitoring() -> None:
    assert _summary([], monitored=0.0).availability_pct is None


def test_summary_carries_quality_per_track() -> None:
    internet = _summary([]).track(INTERNET)
    assert internet is not None
    assert (internet.sent, internet.lost) == (6, 1)
    assert internet.max_ms == 40.0


def test_day_rows_combine_outages_and_quality() -> None:
    start = utc(2026, 11, 1, 10)
    summary = _summary([Outage("line", "", start, start + timedelta(minutes=6), True),
                        Outage("dns", "", start + timedelta(minutes=8), start + timedelta(minutes=9), True)])
    day = summary.days[0]
    assert day.day == "2026-11-01"
    assert (day.outages, day.line_outages, day.line_downtime_s) == (2, 1, 360)
    assert day.availability_pct == pytest.approx(90.0)
    assert day.loss_pct == pytest.approx(100 / 6)
    assert day.ping_p50 == 22.0


def test_coverage_from_store(config: Config, seeded) -> None:
    store, start, end = seeded
    coverage = load_coverage(store, start, end, 90, config.zone)
    assert coverage.gaps and coverage.monitored_s == pytest.approx(46 * 3600, abs=60)


def test_outages_are_clipped_to_range(config: Config, store: Store) -> None:
    start = utc(2026, 11, 1, 10)
    store.open_outage(None, "line", "x", start - timedelta(hours=1))
    store.close_outage("line", start + timedelta(minutes=10), True)
    outages = load_outages(store, start, start + timedelta(hours=1))
    assert outages[0].start == start
    assert outages[0].duration_s == 600


def test_open_outage_ends_at_last_heartbeat_and_is_marked(config: Config, store: Store) -> None:
    start = utc(2026, 11, 1, 10)
    session = store.start_session(start, "h", "o", "p", "v", "c")
    store.open_outage(session, "line", "x", start)
    store.add_cycle(session, start + timedelta(minutes=2), "line", {})
    outage = load_outages(store, start, start + timedelta(hours=1))[0]
    assert outage.ongoing and not outage.confirmed
    assert outage.duration_s == 120


