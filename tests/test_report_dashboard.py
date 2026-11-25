from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest
from conftest import results, session

from linkmon.cli import main
from linkmon.config import Config
from linkmon.dashboard import (
    HEAT_LOSS,
    HEAT_PING,
    KIND_GAP,
    _band,
    SEVERITY_FLOOR,
    _range_bounds,
    build_figure,
    build_outage_figure,
    build_heatmap,
    create_app,
    filter_rows,
    load_view,
    loss_severity,
    outage_list,
    outage_summary,
    ping_limits,
    ping_severity,
    severity_tone,
    table_rows,
    track_table,
)
from linkmon.plots import FIGURES, PlotContext
from linkmon.quality import load_samples
from linkmon.report import build_report, collect_warnings, number
from linkmon.stats import Coverage, compute_coverage, compute_summary, load_outages
from linkmon.store import Store


def _context(config: Config, store: Store, start: datetime, end: datetime) -> PlotContext:
    samples = load_samples(store, start, end)
    stamps = [moment.to_pydatetime() for moment in samples["ts"]]
    coverage = compute_coverage(stamps, start, end, 90, config.zone)
    return PlotContext(samples, load_outages(store, start, end), coverage, start, end, config.zone)


def test_report_creates_pdf_figures_and_checksum(config: Config, seeded: tuple[Store, datetime, datetime],
                                                 tmp_path: Path) -> None:
    store, start, end = seeded
    result = build_report(config, store, start, end, tmp_path / "bericht")
    assert result.pdf.stat().st_size > 50_000
    assert result.pdf.read_bytes().startswith(b"%PDF")
    for name in FIGURES:
        for suffix in (".pdf", ".svg", ".png"):
            assert (tmp_path / "bericht" / "abbildungen" / f"{name}{suffix}").stat().st_size > 0
    assert (tmp_path / "bericht" / "rohdaten" / "rohdaten.jsonl").is_file()
    summary = result.summary
    assert summary.outage_count["line"] == 1
    assert summary.line_downtime_s == 1800
    assert summary.coverage_pct is not None and summary.coverage_pct < 100  # Die eingebaute Lücke fehlt.
    assert len(summary.days) == 2
    internet = summary.track("internet")
    assert internet is not None and internet.lost > 0 and internet.p95_ms >= 60


def test_report_works_without_any_data(config: Config, store: Store, tmp_path: Path) -> None:
    start = datetime(2026, 1, 1, tzinfo=config.zone)
    result = build_report(config, store, start, start + timedelta(days=1), tmp_path)
    assert result.pdf.is_file()
    assert result.summary.availability_pct is None


def test_figures_work_with_a_single_round(config: Config, store: Store) -> None:
    start = datetime(2026, 11, 1, tzinfo=config.zone)
    store.add_cycle(session(store, start), start + timedelta(hours=1), None, results(ext_a=None, ext_b=None))
    context = _context(config, store, start, start + timedelta(days=1))
    for builder in FIGURES.values():
        builder(context)


def test_figures_work_without_rounds(config: Config, store: Store) -> None:
    start = datetime(2026, 11, 1, tzinfo=config.zone)
    context = _context(config, store, start, start + timedelta(days=1))
    for builder in FIGURES.values():
        builder(context)


def test_warnings_name_wlan_gaps_and_unconfirmed_outages(config: Config) -> None:
    start = datetime(2026, 11, 1, tzinfo=config.zone)
    end = start + timedelta(days=1)
    summary = compute_summary(load_samples_empty(), [], Coverage(0, [], {}, 0), start, end, config.zone)
    sessions = [{"connection_type": "wlan"}, {"connection_type": None}]
    text = " ".join(collect_warnings(summary, sessions, []))
    assert "WLAN" in text
    assert "nicht erfasst" in text
    assert "keine Prüfungen" in text


def load_samples_empty():
    from linkmon.quality import parse_cycles

    return parse_cycles([])


def test_number_format_is_german() -> None:
    assert number(99.5, 1, " %") == "99,5 %"
    assert number(None) == "–"
    assert number(float("nan")) == "–"


def _view(config: Config, store: Store, start: datetime, end: datetime):
    return load_view(config, store, start, end, end, cache=None)


def test_outage_figure_marks_every_outage_and_gap(config: Config, seeded: tuple[Store, datetime, datetime]) -> None:
    store, start, end = seeded
    view = _view(config, store, start, end)
    figure = build_outage_figure(view["outages"], view["gaps"], view["start"], view["end"], config.zone)
    names = {trace.name for trace in figure.data}
    assert {"Leitung (Vodafone)", "Router/LAN", "Nur DNS", KIND_GAP} <= names
    marks = next(trace for trace in figure.data if trace.name == "Leitung (Vodafone)")
    assert marks.mode == "markers" and "Dauer 30 min" in marks.hovertext[0]
    # Grün nur, wo gemessen wurde: Die eingebaute Lücke unterbricht die Zeile.
    ok = next(trace for trace in figure.data if trace.name == "Leitung (Vodafone) ok")
    assert list(ok.x).count(None) == 2


def test_outage_figure_is_compact_and_static_on_mobile(config: Config) -> None:
    start = datetime(2026, 11, 1, tzinfo=config.zone)
    end = start + timedelta(days=1)
    figure = build_outage_figure([], [], start, end, config.zone, mobile=True)
    assert figure.layout.dragmode is False
    assert figure.layout.xaxis.fixedrange is True
    assert list(figure.layout.yaxis.categoryarray) == ["Leitung"]  # Leitung steht immer da, auch ohne Ausfall.


def test_detail_figure_shows_chosen_tracks_only(config: Config, seeded: tuple[Store, datetime, datetime]) -> None:
    store, start, end = seeded
    view = _view(config, store, start, end)
    args = (view["samples"], view["outages"], view["gaps"], view["start"], view["end"], config.zone)
    figure = build_figure(*args)
    lines = {trace.name for trace in figure.data if trace.type == "scatter" and trace.mode == "lines" and trace.name}
    assert lines == {"Internet (1.1.1.1, 8.8.8.8)"}
    strips = [trace for trace in figure.data if trace.type == "heatmap"]
    assert len(strips) == 1
    figure = build_figure(*args, selected=["internet", "lan:192.168.0.1"])
    assert len([trace for trace in figure.data if trace.type == "heatmap"]) == 2
    # Abends steigt der Ping von 21 auf 61 ms: Diese Punkte tragen die Ampelfarbe.
    raised = next(trace for trace in figure.data if trace.name == "Internet (1.1.1.1, 8.8.8.8) erhöht")
    assert min(raised.marker.color) > 0
    rects = [shape for shape in figure.layout.shapes if shape.type == "rect"]
    assert len(rects) >= 3 + len(view["gaps"])


def test_dashboard_ping_axis_switches(config: Config, seeded: tuple[Store, datetime, datetime]) -> None:
    store, start, end = seeded
    view = _view(config, store, start, end)
    args = (view["samples"], view["outages"], view["gaps"], view["start"], view["end"], config.zone)
    linear = build_figure(*args).layout.yaxis
    assert linear.type != "log" and linear.range[1] >= 61  # Die Abendwerte passen auf die Achse.
    assert build_figure(*args, log_latency=True).layout.yaxis.type == "log"


def test_severity_keeps_zero_green_and_separates_small_values() -> None:
    assert float(loss_severity(0)) == 0
    assert float(loss_severity(0.01)) == SEVERITY_FLOOR  # Jeder Verlust ist sichtbar nicht grün.
    assert float(loss_severity(0.4)) > float(loss_severity(0.05)) + 0.3
    assert float(loss_severity(50)) == 1
    good, bad = ping_limits(17)
    assert 20 < good < 30 and bad >= 90
    assert float(ping_severity(20, good, bad)) == 0
    assert float(ping_severity(80, good, bad)) > 0.7
    assert severity_tone(0.0) == "tone-ok" and severity_tone(0.9) == "tone-bad"


def test_outage_list_is_newest_first_and_capped(config: Config) -> None:
    from linkmon.stats import Outage

    start = datetime(2026, 11, 1, tzinfo=config.zone)
    outages = [Outage("line", "weg", start + timedelta(minutes=i), start + timedelta(minutes=i, seconds=4), True)
               for i in range(12)]
    text = str(outage_list(outages, config.zone, limit=8))
    assert "4 s" in text and "und 4 weitere" in text
    assert text.index("00:11:00") < text.index("00:10:00")
    assert outage_summary(outages) == "12× Leitung · Leitung zusammen 48 s"


def test_band_does_not_bridge_gaps() -> None:
    import pandas as pd

    times = pd.date_range("2026-11-01", periods=6, freq="1min")
    low = pd.Series([1.0, 1.0, None, None, 1.0, 1.0])
    xs, ys = _band(times, low, low + 1)
    assert xs.count(None) == 2  # zwei getrennte Flächen
    assert len(xs) == 2 * (4 + 1)


def test_heatmap_for_both_values(config: Config, seeded: tuple[Store, datetime, datetime]) -> None:
    store, start, end = seeded
    view = _view(config, store, start, end)
    for value in (HEAT_LOSS, HEAT_PING):
        figure = build_heatmap(view["samples"], view["start"], view["end"], config.zone, value)
        assert len(figure.data[0].z) == 24 and len(figure.data[0].z[0]) == 2


def test_table_lists_outages_and_gaps_newest_first(config: Config, seeded: tuple[Store, datetime, datetime]) -> None:
    store, start, end = seeded
    view = _view(config, store, start, end)
    rows = table_rows(view["outages"], view["gaps"], config.zone)
    assert rows[0]["sort"] >= rows[-1]["sort"]
    dns = next(row for row in rows if row["level"] == "dns")
    assert dns["dauer"] == "00:02:00" and "unbestätigt" in dns["ende"]
    assert len(filter_rows(rows, "outages")) == 3
    assert [row["level"] for row in filter_rows(rows, "line")] == ["line"]
    assert all(row["art"] == KIND_GAP for row in filter_rows(rows, "gaps"))
    assert len(filter_rows(rows, "all")) == len(rows)


def test_track_table_lists_every_track(config: Config, seeded: tuple[Store, datetime, datetime]) -> None:
    store, start, end = seeded
    view = _view(config, store, start, end)
    table = track_table(view["stats"], view["samples"])
    body = table.children[1].children
    assert len(body) == 5


def test_dashboard_app_builds_and_custom_range_includes_end_day(config: Config) -> None:
    app = create_app(config)
    assert app.layout is not None
    begin, finish = _range_bounds("custom", "2026-11-01", "2026-11-02", config.zone,
                                  datetime(2026, 11, 5, tzinfo=config.zone))
    assert (finish - begin).days == 2


def test_cli_report_export_and_status(config: Config, seeded: tuple[Store, datetime, datetime], tmp_path: Path,
                                      capsys: pytest.CaptureFixture[str]) -> None:
    arguments = ["--config", str(config.path)]
    code = main([*arguments, "report", "--from", "2026-11-02", "--to", "2026-11-03", "--out", str(tmp_path / "r")])
    assert code == 0
    output = capsys.readouterr().out
    assert "Rohdaten-SHA-256" in output and "Verfügbarkeit Leitung" in output
    assert (tmp_path / "r" / "bericht.pdf").is_file()
    assert main([*arguments, "export", "--from", "2026-11-02", "--to", "2026-11-03", "--out", str(tmp_path / "e")]) == 0
    assert (tmp_path / "e" / "SHA256SUMS.txt").is_file()
    assert main([*arguments, "status"]) == 0
    assert "Prüfrunden: 5520" in capsys.readouterr().out


def test_cli_reports_missing_config_and_bad_range(config: Config, tmp_path: Path,
                                                  capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--config", str(tmp_path / "nope.toml"), "status"]) == 1
    assert "nicht gefunden" in capsys.readouterr().err
    assert main(["--config", str(config.path), "report", "--from", "2026-11-05", "--to", "2026-11-01"]) == 1
    assert "--to" in capsys.readouterr().err
