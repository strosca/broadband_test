from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from conftest import utc

from linkmon.config import Config
from linkmon.dashboard import create_report_file, report_bounds, report_filename
from linkmon.linestats import LineStatistics, measured_interval, plain_duration
from linkmon.report import hop_state, summary_sentences
from linkmon.stats import Outage
from linkmon.store import Store

ZONE = ZoneInfo("Europe/Berlin")


def _line(start: datetime, seconds: float, level: str = "line", detail: str = "") -> Outage:
    return Outage(level, detail, start, start + timedelta(seconds=seconds), True)


def _stamps(start: datetime, end: datetime, step_s: int = 30) -> list[datetime]:
    count = int((end - start).total_seconds() // step_s)
    return [start + timedelta(seconds=step_s * index) for index in range(count)]


def _stats(outages: list[Outage], start: datetime, end: datetime) -> LineStatistics:
    return LineStatistics(outages, _stamps(start, end), 90, ZONE)


def test_only_line_outages_count() -> None:
    start, end = utc(2026, 11, 2), utc(2026, 11, 4)
    stats = _stats([_line(utc(2026, 11, 2, 10), 600), _line(utc(2026, 11, 2, 12), 300, level="router")], start, end)
    total = stats.period("Gesamt", start, end)
    assert total.count == 1
    assert total.downtime_s == 600
    assert total.longest_s == 600
    assert total.coverage_pct is not None and total.coverage_pct > 99
    assert 99.6 < total.availability_pct < 99.8  # 10 Minuten von zwei Tagen


def test_rolling_periods_end_at_report_end_and_skip_longer_ones() -> None:
    start, end = utc(2026, 11, 1), utc(2026, 11, 11)
    stats = _stats([_line(utc(2026, 11, 2, 10), 60), _line(utc(2026, 11, 10, 20), 120)], start, end)
    rows = stats.rolling(start, end)
    assert [row.label for row in rows] == ["Letzte 24 Stunden", "Letzte 7 Tage", "Gesamter Zeitraum"]
    assert rows[0].start == end - timedelta(hours=24) and rows[0].end == end
    assert [row.count for row in rows] == [1, 1, 2]


def test_outage_across_period_border_is_clipped_and_counted_in_both() -> None:
    start, end = utc(2026, 11, 1), utc(2026, 11, 3)
    stats = _stats([_line(utc(2026, 11, 1, 23, 50), 1200)], start, end)
    first = stats.period("a", utc(2026, 11, 1), utc(2026, 11, 2))
    second = stats.period("b", utc(2026, 11, 2), utc(2026, 11, 3))
    assert (first.count, first.downtime_s) == (1, 600)
    assert (second.count, second.downtime_s) == (1, 600)


def test_weeks_run_monday_to_sunday_in_local_time() -> None:
    start = datetime(2026, 11, 4, tzinfo=ZONE)  # Mittwoch
    end = datetime(2026, 11, 18, tzinfo=ZONE)
    stats = _stats([_line(datetime(2026, 11, 9, 0, 30, tzinfo=ZONE), 60)], start, end)
    weeks = stats.weeks(start, end)
    assert [week.label for week in weeks] == ["KW 45/2026", "KW 46/2026", "KW 47/2026"]
    assert weeks[0].start == start.astimezone(UTC)  # Am Rand gekürzt
    assert weeks[1].start == datetime(2026, 11, 9, tzinfo=ZONE).astimezone(UTC)
    assert [week.count for week in weeks] == [0, 1, 0]


def test_days_with_outages_lists_only_affected_days() -> None:
    start, end = utc(2026, 11, 1), utc(2026, 11, 5)
    stats = _stats([_line(datetime(2026, 11, 2, 23, 55, tzinfo=ZONE), 600),
                    _line(datetime(2026, 11, 4, 12, tzinfo=ZONE), 30)], start, end)
    assert [row.label for row in stats.days_with_outages()] == ["02.11.2026", "03.11.2026", "04.11.2026"]


def test_duration_classes_and_daytime_blocks() -> None:
    start, end = utc(2026, 11, 1), utc(2026, 11, 3)
    stats = _stats([
        _line(datetime(2026, 11, 1, 3, tzinfo=ZONE), 20),
        _line(datetime(2026, 11, 1, 20, tzinfo=ZONE), 60),
        _line(datetime(2026, 11, 1, 21, tzinfo=ZONE), 1800),
        _line(datetime(2026, 11, 2, 21, tzinfo=ZONE), 9000),
    ], start, end)
    assert [row.count for row in stats.by_duration()] == [1, 1, 0, 1, 1]
    assert [row.count for row in stats.by_daytime()] == [1, 0, 0, 3]


def test_plain_duration_reads_naturally() -> None:
    assert plain_duration(12) == "12 Sek."
    assert plain_duration(754) == "12 Min. 34 Sek."
    assert plain_duration(3900) == "1 Std. 5 Min."
    assert plain_duration(90061) == "1 Tag 1 Std. 1 Min."
    assert plain_duration(7200) == "2 Std."


def test_hop_state_from_detail() -> None:
    assert hop_state("Erster Provider-Hop erreichbar, externe Ziele nicht") == "erreichbar"
    assert hop_state("Erster Provider-Hop und externe Ziele nicht erreichbar") == "nicht erreichbar"
    assert hop_state("Externe Ziele nicht erreichbar, Router erreichbar") == "nicht geprüft"


def test_summary_names_count_duration_and_longest(config: Config) -> None:
    start, end = utc(2026, 11, 1), utc(2026, 11, 3)
    stats = _stats([_line(utc(2026, 11, 1, 10), 120), _line(utc(2026, 11, 2, 18), 3900)], start, end)
    text = " ".join(summary_sentences(stats.period("Gesamt", start, end), config))
    assert "<b>2-mal</b>" in text
    assert "1 Std. 7 Min." in text  # zusammen
    assert "dauerte <b>1 Std. 5 Min.</b> und begann am 02.11.2026 um 19:00 Uhr" in text
    assert ".." not in text
    assert "Vodafone" in text


def test_summary_without_outages(config: Config) -> None:
    start, end = utc(2026, 11, 1), utc(2026, 11, 2)
    text = " ".join(summary_sentences(_stats([], start, end).period("Gesamt", start, end), config))
    assert "kein Ausfall" in text


def test_report_bounds_for_all_start_at_first_check(config: Config, seeded: tuple[Store, datetime, datetime]) -> None:
    store, start, end = seeded
    now = end + timedelta(days=3)
    first, last = report_bounds(config, store, "all", None, None, now)
    assert first == start
    assert last == now
    custom = report_bounds(config, store, "custom", "2026-11-02", "2026-11-03", now)
    assert custom == (start, end)
    assert report_filename(*custom, config.zone) == "stoerungsprotokoll_2026-11-02_bis_2026-11-03.pdf"


def test_web_report_is_built_and_kept_on_the_device(config: Config, seeded: tuple[Store, datetime, datetime]) -> None:
    _, start, end = seeded
    pdf, filename = create_report_file(config, "custom", "2026-11-02", "2026-11-03", end + timedelta(days=1))
    assert pdf.read_bytes().startswith(b"%PDF")
    assert Path(config.data_dir / "berichte") in pdf.parents
    assert (pdf.parent / "rohdaten" / "rohdaten.jsonl").is_file()
    assert filename.endswith("_bis_2026-11-03.pdf")


def test_interval_comes_from_the_data_not_the_config(config: Config) -> None:
    start = utc(2026, 11, 1)
    stamps = _stamps(start, start + timedelta(hours=1), step_s=3) + [start + timedelta(hours=5)]
    assert measured_interval(stamps) == 3
    assert measured_interval([start]) is None
    stats = _stats([], start, start + timedelta(days=1))
    text = " ".join(summary_sentences(stats.period("Gesamt", start, start + timedelta(days=1)), config, 30.0))
    assert "alle 30 Sekunden" in text and "ab etwa 90 Sekunden" in text
