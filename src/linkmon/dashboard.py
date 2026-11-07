"""Lokales Webinterface zur Verfügbarkeit: dunkles Layout, aktualisiert sich automatisch."""

from __future__ import annotations

import json
import logging
import threading
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
import plotly.graph_objects as go
from dash import Dash, Input, Output, dash_table, dcc, html
from plotly.subplots import make_subplots

from linkmon.config import Config
from linkmon.levels import LEVEL_LABELS, LEVEL_ORDER
from linkmon.monitor import CONNECTION_LABELS, META_CONNECTION, monitor_gap_threshold
from linkmon.quality import (
    INTERNET,
    LEGACY_SPEEDTEST_MARKER,
    SampleCache,
    TrackStats,
    all_track_stats,
    bucket_size,
    bucket_table,
    days_between,
    find_track,
    hour_matrix,
    latest_internet_ms,
    load_samples,
    ping_tracks,
)
from linkmon.stats import Coverage, Outage, compute_coverage, load_outages, total_seconds
from linkmon.store import Store
from linkmon.timeutil import format_duration, format_local, iso_utc, now_utc, parse_iso

# Dunkle Stufen der geprüften Palette. Die Berichte nutzen die hellen Stufen aus plots.py.
COLOR_INK = "#f0efec"
COLOR_INK_2 = "#c3c2b7"
COLOR_INK_3 = "#898781"
COLOR_GRID = "#2c2c2a"
COLOR_AXIS = "#383835"
COLOR_SURFACE = "#1a1a19"
COLOR_RAISED = "#232322"
COLOR_GAP = "#55544f"
# Kategoriale Reihenfolge: Die Farbe folgt der Strecke (Internet, Provider-Hop, Heimnetz ...), nie dem Rang.
SERIES_COLORS = ("#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9")
LEVEL_COLORS_DARK = {"line": "#e66767", "router": "#c98500", "local": "#9085e9", "dns": "#d55181", "https": "#008300"}
# Blaue Rampe für Mengen: dunkel nahe der Fläche heißt wenig, hell heißt viel.
SEQUENTIAL_DARK = [[0.0, "#0d366b"], [0.35, "#1c5cab"], [0.7, "#3987e5"], [1.0, "#b7d3f6"]]
TABLE_OUTAGE_FILL = "#2c1c1b"
TABLE_GAP_FILL = "#232322"

FONT = '"Inter", "Segoe UI Variable Text", "Segoe UI", system-ui, sans-serif'
RANGES = {"6h": timedelta(hours=6), "24h": timedelta(hours=24), "7d": timedelta(days=7), "30d": timedelta(days=30)}
DEFAULT_RANGE = "24h"
TOGGLE_LOG = "log"
HEAT_LOSS = "loss_pct"
HEAT_PING = "p95"
KIND_OUTAGE = "Ausfall"
KIND_GAP = "Messlücke"
_CACHE = SampleCache()


def _number(value: Any, digits: int) -> str:
    if value is None or pd.isna(value):
        return "–"
    return f"{value:.{digits}f}".replace(".", ",")


def _ms(value: Any) -> str:
    return "–" if value is None or pd.isna(value) else f"{_number(value, 0)} ms"


def _naive(moment: datetime, zone: ZoneInfo) -> datetime:
    return moment.astimezone(zone).replace(tzinfo=None)


def _naive_index(index: pd.DatetimeIndex, zone: ZoneInfo) -> pd.DatetimeIndex:
    return index.tz_convert(zone).tz_localize(None)


def _transparent(color: str, alpha: float) -> str:
    red, green, blue = (int(color[index:index + 2], 16) for index in (1, 3, 5))
    return f"rgba({red},{green},{blue},{alpha})"


def series_color(position: int) -> str:
    return SERIES_COLORS[position % len(SERIES_COLORS)]


def _base_layout() -> dict[str, Any]:
    return {
        "paper_bgcolor": "rgba(0,0,0,0)",
        "plot_bgcolor": "rgba(0,0,0,0)",
        "separators": ",.",
        "font": {"family": FONT, "color": COLOR_INK_2, "size": 12},
        "hoverlabel": {"bgcolor": COLOR_RAISED, "bordercolor": COLOR_AXIS, "align": "left",
                       "font": {"family": FONT, "color": COLOR_INK, "size": 12}},
    }


def empty_figure(text: str, height: int = 320) -> go.Figure:
    figure = go.Figure()
    figure.update_layout(height=height, xaxis={"visible": False}, yaxis={"visible": False},
                         annotations=[{"text": text, "showarrow": False, "font": {"color": COLOR_INK_3, "size": 14}}],
                         **_base_layout())
    return figure


def _time_axes(figure: go.Figure) -> None:
    # Das Format folgt dem Zoom, sonst stünde bei kurzen Zeiträumen mehrfach dieselbe Minute da.
    figure.update_xaxes(gridcolor=COLOR_GRID, linecolor=COLOR_AXIS, showgrid=False, ticks="outside", ticklen=8,
                        tickcolor="rgba(0,0,0,0)", tickformatstops=[
                            {"dtickrange": [None, 59999], "value": "%H:%M:%S"},
                            {"dtickrange": [60000, 43199999], "value": "%d.%m. %H:%M"},
                            {"dtickrange": [43200000, None], "value": "%d.%m."},
                        ])
    figure.update_yaxes(gridcolor=COLOR_GRID, linecolor=COLOR_AXIS, zeroline=False,
                        title_font={"color": COLOR_INK_3, "size": 11})


def _timeline_rows(outages: list[Outage], gaps: list[tuple[datetime, datetime]]) -> list[str]:
    present = {outage.level for outage in outages}
    rows = [LEVEL_LABELS[level] for level in LEVEL_ORDER if level in present]
    if gaps:
        rows.append(KIND_GAP)
    return rows


def _timeline_trace(label: str, intervals: list[tuple[datetime, datetime, str]], color: str, zone: ZoneInfo,
                    minimum: timedelta) -> go.Scatter:
    """Zeichnet Intervalle als dicke Linienstücke in einer Zeile. Sehr kurze bleiben mit Mindestlänge sichtbar."""
    xs: list[datetime | None] = []
    texts: list[str | None] = []
    for begin, finish, text in intervals:
        finish = max(finish, begin + minimum)
        xs += [_naive(begin, zone), _naive(finish, zone), None]
        texts += [text, text, None]
    return go.Scatter(x=xs, y=[label if x is not None else None for x in xs], mode="lines", name=label,
                      line={"color": color, "width": 14}, hovertext=texts, hoverinfo="text", showlegend=False,
                      connectgaps=False)


def _add_timeline(figure: go.Figure, outages: list[Outage], gaps: list[tuple[datetime, datetime]],
                  zone: ZoneInfo, span_s: float) -> None:
    """Oberste Zeile: jeder Ausfall in der Zeile seiner Ebene, dazu die Messlücken."""
    minimum = timedelta(seconds=max(span_s * 0.002, 1))
    for level in LEVEL_ORDER:
        items = [outage for outage in outages if outage.level == level]
        if not items:
            continue
        label = LEVEL_LABELS[level]
        intervals = []
        for outage in items:
            suffix = " (läuft noch)" if outage.ongoing else ("" if outage.confirmed else " (Ende unbestätigt)")
            intervals.append((outage.start, outage.end,
                              f"<b>{label}</b><br>{format_local(outage.start, zone)} bis "
                              f"{format_local(outage.end, zone)}{suffix}<br>Dauer "
                              f"{format_duration(outage.duration_s)}<br>{outage.detail}"))
        figure.add_trace(_timeline_trace(label, intervals, LEVEL_COLORS_DARK[level], zone, minimum), row=1, col=1)
    if gaps:
        intervals = [(begin, finish, f"<b>Messlücke</b> (Tool lief nicht)<br>{format_local(begin, zone)} bis "
                                     f"{format_local(finish, zone)}<br>Dauer "
                                     f"{format_duration((finish - begin).total_seconds())}")
                     for begin, finish in gaps]
        figure.add_trace(_timeline_trace(KIND_GAP, intervals, COLOR_GAP, zone, minimum), row=1, col=1)


def _band(times: pd.DatetimeIndex, low: pd.Series, high: pd.Series) -> tuple[list[Any], list[Any]]:
    """Umriss eines Bandes, je zusammenhängendem Abschnitt eine eigene Fläche. Über Lücken wird nicht gefüllt."""
    xs: list[Any] = []
    ys: list[Any] = []
    valid = (low.notna() & high.notna()).to_numpy()
    start = None
    for index in range(len(valid) + 1):
        inside = index < len(valid) and valid[index]
        if inside and start is None:
            start = index
        elif not inside and start is not None:
            segment = slice(start, index)
            xs += list(times[segment]) + list(times[segment][::-1]) + [None]
            ys += list(high.iloc[segment]) + list(low.iloc[segment][::-1]) + [None]
            start = None
    return xs, ys


def build_figure(samples: pd.DataFrame, outages: list[Outage], gaps: list[tuple[datetime, datetime]],
                 start: datetime, end: datetime, zone: ZoneInfo, *, log_latency: bool = False,
                 revision: str = "linkmon") -> go.Figure:
    timeline = _timeline_rows(outages, gaps)
    top_share = 0.06 + 0.035 * max(len(timeline), 1)
    figure = make_subplots(rows=3, cols=1, shared_xaxes=True, vertical_spacing=0.07,
                           row_heights=[top_share, (1 - top_share) * 0.6, (1 - top_share) * 0.4],
                           subplot_titles=("Ausfälle", "Ping (Median je Intervall)", "Paketverlust"))
    for heading in figure.layout.annotations:
        heading.update(x=0, xanchor="left", font={"size": 12, "color": COLOR_INK_3, "family": FONT})

    span_s = (end - start).total_seconds()
    _add_timeline(figure, outages, gaps, zone, span_s)
    size = bucket_size(start, end)
    minutes = size / pd.Timedelta(minutes=1)
    size_text = f"{minutes / 60:g} h" if minutes >= 60 else f"{minutes:g} min"
    figure.layout.annotations[1].update(text=f"Ping (Median je {size_text})")
    figure.layout.annotations[2].update(text=f"Paketverlust (Anteil je {size_text})")

    for position, track in enumerate(ping_tracks(samples)):
        color = series_color(position)
        table = bucket_table(samples, track, size)
        times = _naive_index(table.index, zone)
        extra = table[["p95", "max", "loss_pct", "sent"]].to_numpy()
        if track.key == INTERNET:
            # Band vom Median bis P95: zeigt die Spitzen, ohne eine zweite Linie in die Legende zu bringen.
            band_x, band_y = _band(times, table["median"], table["p95"])
            figure.add_trace(go.Scatter(x=band_x, y=band_y, mode="lines", fill="toself", line={"width": 0},
                                        fillcolor=_transparent(color, 0.18), hoverinfo="skip", showlegend=False,
                                        legendgroup=track.key), row=2, col=1)
        figure.add_trace(go.Scatter(
            x=times, y=table["median"], name=track.label, mode="lines", legendgroup=track.key,
            line={"color": color, "width": 2}, connectgaps=False, customdata=extra,
            hovertemplate="%{y:.0f} ms, P95 %{customdata[0]:.0f}, max %{customdata[1]:.0f} ms<extra>"
                          + track.label + "</extra>",
        ), row=2, col=1)
        figure.add_trace(go.Scatter(
            x=times, y=table["loss_pct"], name=track.label, mode="lines", line={"color": color, "width": 2, "shape": "hv"},
            legendgroup=track.key, showlegend=False, connectgaps=False, customdata=extra,
            hovertemplate="%{y:.1f} % von %{customdata[3]:.0f} Pings<extra>" + track.label + "</extra>",
        ), row=3, col=1)

    shapes: list[dict[str, Any]] = []
    for outage in outages:
        color = LEVEL_COLORS_DARK.get(outage.level, COLOR_INK_2)
        for axis in ("y2", "y3"):
            shapes.append({"type": "rect", "xref": "x", "yref": f"{axis} domain", "y0": 0, "y1": 1,
                           "x0": _naive(outage.start, zone), "x1": _naive(outage.end, zone),
                           "fillcolor": color, "opacity": 0.18, "line": {"width": 0}, "layer": "below"})
    for begin, finish in gaps:
        for axis in ("y2", "y3"):
            shapes.append({"type": "rect", "xref": "x", "yref": f"{axis} domain", "y0": 0, "y1": 1,
                           "x0": _naive(begin, zone), "x1": _naive(finish, zone),
                           "fillcolor": COLOR_GAP, "opacity": 0.45, "line": {"width": 0}, "layer": "below"})

    height = 640 + 22 * len(timeline)
    figure.update_layout(
        height=height, margin={"l": 54, "r": 20, "t": 44, "b": 30}, hovermode="x unified", uirevision=revision,
        shapes=shapes,
        legend={"orientation": "h", "yanchor": "bottom", "y": 1.03, "x": 0, "bgcolor": "rgba(0,0,0,0)",
                "font": {"color": COLOR_INK_2, "size": 12}},
        **_base_layout(),
    )
    _time_axes(figure)
    figure.update_xaxes(range=[_naive(start, zone), _naive(end, zone)])
    figure.update_yaxes(row=1, col=1, type="category", categoryorder="array", categoryarray=timeline or [" "],
                        autorange="reversed", showgrid=False, fixedrange=True, tickfont={"size": 11})
    if log_latency:
        ticks = [1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000]
        figure.update_yaxes(title_text="ms", type="log", tickvals=ticks, ticktext=[str(t) for t in ticks],
                            row=2, col=1)
    else:
        figure.update_yaxes(title_text="ms", rangemode="tozero", row=2, col=1)
    figure.update_yaxes(title_text="%", rangemode="tozero", row=3, col=1)
    if not timeline:
        figure.add_annotation(text="Keine Ausfälle und Messlücken im Zeitraum", xref="x domain", yref="y domain",
                              x=0.5, y=0.5, showarrow=False, font={"color": COLOR_INK_3, "size": 12}, row=1, col=1)
    if samples.empty:
        figure.add_annotation(text="Im gewählten Zeitraum gibt es keine Prüfungen", xref="paper", yref="paper",
                              x=0.5, y=0.45, showarrow=False, font={"color": COLOR_INK_3, "size": 14})
    return figure


def build_heatmap(samples: pd.DataFrame, start: datetime, end: datetime, zone: ZoneInfo, value: str) -> go.Figure:
    track = find_track(samples, INTERNET)
    if track is None or samples.empty:
        return empty_figure("Keine Prüfungen im Zeitraum", height=360)
    days = days_between(start, end, zone)
    matrix = hour_matrix(samples, track, zone, value, days)
    unit, digits = ("%", 2) if value == HEAT_LOSS else ("ms", 0)
    label = "Paketverlust" if value == HEAT_LOSS else "Ping P95"
    labels = [f"{day[8:10]}.{day[5:7]}." for day in matrix.columns]
    hours = [f"{hour:02d}:00" for hour in matrix.index]
    figure = go.Figure(go.Heatmap(
        z=matrix.to_numpy(dtype=float), x=labels, y=hours, colorscale=SEQUENTIAL_DARK, zmin=0, xgap=2, ygap=2,
        hovertemplate=f"%{{x}} %{{y}}<br>{label}: %{{z:.{digits}f}} {unit}<extra></extra>",
        colorbar={"title": {"text": unit, "font": {"color": COLOR_INK_3, "size": 11}}, "thickness": 10,
                  "outlinewidth": 0, "tickfont": {"color": COLOR_INK_3, "size": 11}},
    ))
    figure.update_layout(height=360, margin={"l": 54, "r": 10, "t": 10, "b": 30}, **_base_layout())
    figure.update_xaxes(showgrid=False, linecolor=COLOR_AXIS, type="category")
    figure.update_yaxes(showgrid=False, autorange="reversed", type="category", dtick=3)
    return figure


def table_rows(outages: list[Outage], gaps: list[tuple[datetime, datetime]], zone: ZoneInfo) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for outage in outages:
        suffix = " (läuft noch)" if outage.ongoing else ("" if outage.confirmed else " (Ende unbestätigt)")
        rows.append({
            "sort": iso_utc(outage.start), "art": KIND_OUTAGE, "ebene": LEVEL_LABELS.get(outage.level, outage.level),
            "level": outage.level, "beginn": outage.start.astimezone(zone).strftime("%Y-%m-%d %H:%M:%S"),
            "ende": outage.end.astimezone(zone).strftime("%Y-%m-%d %H:%M:%S") + suffix,
            "dauer": format_duration(outage.duration_s), "dauer_s": outage.duration_s, "befund": outage.detail,
        })
    for begin, finish in gaps:
        rows.append({
            "sort": iso_utc(begin), "art": KIND_GAP, "ebene": "", "level": "",
            "beginn": begin.astimezone(zone).strftime("%Y-%m-%d %H:%M:%S"),
            "ende": finish.astimezone(zone).strftime("%Y-%m-%d %H:%M:%S"),
            "dauer": format_duration((finish - begin).total_seconds()), "dauer_s": (finish - begin).total_seconds(),
            "befund": "Tool lief nicht oder Rechner schlief: weder verfügbar noch ausgefallen",
        })
    rows.sort(key=lambda item: item["sort"], reverse=True)
    return rows


def filter_rows(rows: list[dict[str, Any]], choice: str) -> list[dict[str, Any]]:
    if choice == "outages":
        return [row for row in rows if row["art"] == KIND_OUTAGE]
    if choice == "line":
        return [row for row in rows if row["level"] == "line"]
    if choice == "gaps":
        return [row for row in rows if row["art"] == KIND_GAP]
    return rows


TABLE_COLUMNS = [
    {"name": "Beginn (lokal)", "id": "beginn"}, {"name": "Ende", "id": "ende"}, {"name": "Dauer", "id": "dauer"},
    {"name": "Art", "id": "art"}, {"name": "Ebene", "id": "ebene"}, {"name": "Befund", "id": "befund"},
]


def _range_bounds(choice: str, custom_start: str | None, custom_end: str | None, zone: ZoneInfo,
                  now: datetime) -> tuple[datetime, datetime]:
    if choice == "custom" and custom_start and custom_end:
        begin = datetime.fromisoformat(custom_start[:10]).replace(tzinfo=zone)
        finish = datetime.fromisoformat(custom_end[:10]).replace(tzinfo=zone) + timedelta(days=1)
        return begin, finish
    finish = now + timedelta(seconds=1)
    if choice in RANGES:
        return finish - RANGES[choice], finish
    return datetime(2000, 1, 1, tzinfo=UTC), finish


CHECK_LABELS = {"lan": "Heimnetz", "isp": "Provider-Hop", "ext": "Internet", "dns": "DNS", "https": "HTTPS",
                "interface": "Netzwerkkarte"}


def current_condition(last_results: str | None, open_levels: list[str]) -> tuple[str, str]:
    """Fasst die letzte Prüfrunde für den schnellen Blick zusammen: Überschrift und Details."""
    if last_results is None:
        return "Unbekannt", "Noch keine Prüfung"
    results: dict[str, list[Any]] = json.loads(last_results)
    results.pop(LEGACY_SPEEDTEST_MARKER, None)
    failed = sorted({CHECK_LABELS.get(name.split(":")[0], name) for name, item in results.items() if not item[0]})
    note = f"{len(results) - sum(1 for item in results.values() if not item[0])} von {len(results)} Prüfungen in Ordnung"
    if failed:
        note += ". Gestört: " + ", ".join(failed)
    if open_levels:
        return "Ausfall: " + ", ".join(LEVEL_LABELS.get(level, level) for level in open_levels), note
    return ("Prüfung gestört" if failed else "Alles in Ordnung"), note


def condition_tone(condition: str) -> tuple[str, str]:
    """Liefert CSS-Ton und Symbol: Der Zustand ist nie allein an der Farbe erkennbar."""
    if condition == "Alles in Ordnung":
        return "tone-good", "✓"
    if condition.startswith("Ausfall"):
        return "tone-critical", "✕"
    if condition == "Prüfung gestört":
        return "tone-warning", "!"
    return "tone-unknown", "?"


def age_text(seconds: float) -> str:
    seconds = max(seconds, 0)
    if seconds < 90:
        return f"vor {seconds:.0f} s"
    if seconds < 5400:
        return f"vor {seconds / 60:.0f} min"
    if seconds < 129600:
        return f"vor {seconds / 3600:.0f} h"
    return f"vor {seconds / 86400:.0f} d"


def _kpi(label: str, value: str, unit: str = "", note: str = "") -> html.Div:
    return html.Div([
        html.Div(label, className="kpi-label"),
        html.Div([value, html.Span(unit, className="kpi-unit")] if unit else value, className="kpi-value"),
        html.Div(note, className="kpi-note"),
    ], className="card kpi")


def _fact(label: str, value: str, live: bool | None = None) -> html.Div:
    content: list[Any] = [value]
    if live is not None:
        content.insert(0, html.Span(className="dot dot-live" if live else "dot"))
    return html.Div([html.Div(label, className="fact-label"), html.Div(content, className="fact-value")])


def connection_text(meta: str | None) -> tuple[str, bool]:
    """Liefert Text und ob über WLAN gemessen wird."""
    if not meta:
        return "–", False
    info = json.loads(meta)
    kind = info.get("connection_type") or "unbekannt"
    text = CONNECTION_LABELS.get(kind, kind)
    if info.get("link_mbps"):
        text += f", {info['link_mbps']:.0f} Mbit/s"
    return text, kind == "wlan"


def _last_outage_text(store: Store, now: datetime) -> str:
    outages = store.outages()
    if not outages:
        return "Noch keiner"
    last = outages[-1]
    label = LEVEL_LABELS.get(last["level"], last["level"])
    if last["end_utc"] is None:
        return f"{label}, läuft seit {age_text((now - parse_iso(last['start_utc'])).total_seconds())[4:]}"
    return f"{label}, {age_text((now - parse_iso(last['end_utc'])).total_seconds())}"


def build_overview(store: Store, samples: pd.DataFrame, stats: list[TrackStats], outages: list[Outage],
                   coverage: Coverage, config: Config, now: datetime) -> list[Any]:
    threshold = monitor_gap_threshold(config.reachability.interval_s)
    last = store.last_cycle_time()
    condition, condition_note = "Unbekannt", "Der Messdienst läuft nicht. Starten mit: linkmon run"
    service_value, service_live = "Keine Daten", False
    ping_now = "–"
    if last is not None:
        age = (now - parse_iso(last)).total_seconds()
        service_live = age <= threshold
        service_value = f"Aktiv, geprüft {age_text(age)}" if service_live else f"Gestoppt, zuletzt {age_text(age)}"
        if service_live:
            newest = store.latest_cycle()
            results = newest["results"] if newest else None
            condition, condition_note = current_condition(results, [row["level"] for row in store.open_outages()])
            ping_now = _ms(latest_internet_ms(results))
    connection, on_wlan = connection_text(store.get_meta(META_CONNECTION))

    tone, icon = condition_tone(condition)
    hero = html.Div([
        html.Div([
            html.Div(icon, className="hero-badge", **{"aria-hidden": "true"}),
            html.Div([html.Div(condition, className="hero-title"), html.Div(condition_note, className="hero-note")]),
        ], className="hero-main"),
        html.Div([
            _fact("Messdienst", service_value, service_live),
            _fact("Ping ins Internet", ping_now),
            _fact("Verbindung", connection),
            _fact("Letzter Ausfall", _last_outage_text(store, now)),
        ], className="hero-facts"),
    ], className=f"card hero {tone}")

    overview: list[Any] = [hero]
    if on_wlan:
        overview.append(html.Div([
            html.Span("!", className="callout-icon"),
            html.Span("Gemessen wird über WLAN. Aussetzer des WLANs erscheinen als Störung im Heimnetz, Funkstörungen "
                      "erhöhen Ping und Verlust. Für einen Nachweis gegenüber dem Anbieter den Rechner per Kabel "
                      "direkt an den Router anschließen."),
        ], className="callout"))

    line_down = total_seconds([(o.start, o.end) for o in outages if o.level == "line"])
    availability = 100 * (1 - line_down / coverage.monitored_s) if coverage.monitored_s > 0 else None
    line_count = sum(1 for o in outages if o.level == "line")
    internet = next((item for item in stats if item.track.key == INTERNET), None)
    overview.append(html.Div([
        _kpi("Verfügbarkeit Leitung", _number(availability, 2), "%" if availability is not None else "",
             f"Überwacht: {format_duration(coverage.monitored_s)}"),
        _kpi("Ausfälle", str(len(outages)), "",
             f"Davon Leitung: {line_count}, zusammen {format_duration(line_down)}"),
        _kpi("Ping ins Internet", _number(internet.median_ms if internet else None, 0), "ms",
             f"Median · P95 {_ms(internet.p95_ms if internet else None)}"),
        _kpi("Paketverlust", _number(internet.loss_pct if internet else None, 2), "%",
             f"{internet.lost} von {internet.sent} Pings unbeantwortet" if internet else "Keine Prüfungen"),
        _kpi("Jitter", _number(internet.jitter_ms if internet else None, 1), "ms",
             "Schwankung zwischen aufeinanderfolgenden Pings"),
    ], className="kpis"))
    return overview


def track_table(stats: list[TrackStats], samples: pd.DataFrame) -> html.Table:
    """Je Strecke eine Zeile. Wo der Verlust zuerst auftaucht, liegt meist die Ursache."""
    colors = {track.key: series_color(position) for position, track in enumerate(ping_tracks(samples))}
    head = html.Thead(html.Tr([html.Th(text) for text in
                               ("Strecke", "Prüfungen", "Verlust", "Median", "P95", "Max", "Jitter")]))
    rows = []
    for item in stats:
        color = colors.get(item.track.key)
        name: list[Any] = [html.Span(className="swatch", style={"background": color})] if color else \
            [html.Span(className="swatch swatch-empty")]
        name.append(item.track.label)
        rows.append(html.Tr([
            html.Td(name, className="track-name"), html.Td(f"{item.sent:,}".replace(",", ".")),
            html.Td(f"{_number(item.loss_pct, 2)} %"), html.Td(_ms(item.median_ms)), html.Td(_ms(item.p95_ms)),
            html.Td(_ms(item.max_ms)), html.Td(_ms(item.jitter_ms) if item.track.is_ping else "–"),
        ]))
    if not rows:
        rows = [html.Tr(html.Td("Keine Prüfungen im Zeitraum", colSpan=7))]
    return html.Table([head, html.Tbody(rows)], className="stats-table")


def start_in_background(config: Config) -> str | None:
    """Startet das Webinterface im Hintergrund. Gibt die Adresse zurück, oder None, wenn der Port belegt ist."""
    from werkzeug.serving import make_server

    logging.getLogger("werkzeug").setLevel(logging.WARNING)  # Sonst füllen die Abrufe das Log.
    app = create_app(config)
    host, port = config.dashboard.host, config.dashboard.port
    try:
        server = make_server(host, port, app.server, threaded=True)
    except (OSError, SystemExit):  # werkzeug beendet bei belegtem Port sonst den ganzen Prozess.
        return None
    threading.Thread(target=server.serve_forever, name="web-interface", daemon=True).start()
    return f"http://{host}:{port}"


def _table() -> dash_table.DataTable:
    cell_border = f"1px solid {COLOR_GRID}"
    return dash_table.DataTable(
        id="table", columns=TABLE_COLUMNS, sort_action="native", filter_action="native", page_size=15,
        filter_options={"case": "insensitive", "placeholder_text": "Filtern …"},
        style_table={"overflowX": "auto"}, style_as_list_view=True,
        style_cell={"fontFamily": FONT, "fontSize": "13px", "padding": "9px 12px", "textAlign": "left",
                    "backgroundColor": COLOR_SURFACE, "color": COLOR_INK, "border": "0", "borderBottom": cell_border,
                    "maxWidth": "380px", "whiteSpace": "normal"},
        style_header={"backgroundColor": COLOR_SURFACE, "color": COLOR_INK_3, "fontWeight": 500, "fontSize": "12px",
                      "borderBottom": f"1px solid {COLOR_AXIS}"},
        style_filter={"backgroundColor": COLOR_RAISED, "color": COLOR_INK, "borderBottom": cell_border},
        style_data_conditional=[
            {"if": {"filter_query": '{level} = "line"'}, "backgroundColor": TABLE_OUTAGE_FILL},
            {"if": {"filter_query": f'{{art}} = "{KIND_GAP}"'}, "backgroundColor": TABLE_GAP_FILL,
             "color": COLOR_INK_2},
            {"if": {"state": "active"}, "backgroundColor": COLOR_RAISED, "border": "0", "borderBottom": cell_border},
            {"if": {"state": "selected"}, "backgroundColor": COLOR_RAISED, "border": "0", "borderBottom": cell_border},
        ],
    )


def _segmented(component_id: str, value: str, options: list[tuple[str, str]]) -> dcc.RadioItems:
    return dcc.RadioItems(id=component_id, value=value, className="segmented", inline=True,
                          options=[{"label": label, "value": item} for label, item in options])


def _card_head(title: str, *extra: Any, note_id: str | None = None) -> html.Div:
    heading: list[Any] = [html.H2(title, className="card-title")]
    if note_id:
        heading.append(html.Div(id=note_id, className="card-note"))
    return html.Div([html.Div(heading), *extra], className="card-head")


def load_view(config: Config, store: Store, start: datetime, end: datetime, now: datetime,
              cache: SampleCache | None = _CACHE) -> dict[str, Any]:
    """Lädt alles für einen Zeitraum. Vor der ersten Prüfung gab es das Tool noch nicht: keine Messlücke."""
    earliest = store.first_cycle_time()
    first = max(start, parse_iso(earliest)) if earliest else start
    finish = min(end, now)
    if finish <= first:
        first = finish - timedelta(hours=1)
    samples = load_samples(store, first, finish, cache)
    stamps = [moment.to_pydatetime() for moment in samples["ts"]] if not samples.empty else []
    threshold = monitor_gap_threshold(config.reachability.interval_s)
    coverage = compute_coverage(stamps, first, finish, threshold, config.zone)
    outages = load_outages(store, first, finish, now=now)
    gaps = coverage.gaps if coverage.cycles else []
    return {"samples": samples, "coverage": coverage, "outages": outages, "gaps": gaps, "start": first,
            "end": finish, "stats": all_track_stats(samples)}


def create_app(config: Config) -> Dash:
    app = Dash(__name__, title="Verfügbarkeit", update_title=None,
               meta_tags=[{"name": "viewport", "content": "width=device-width, initial-scale=1"},
                          {"name": "theme-color", "content": "#0d0d0d"}])
    zone = config.zone
    app.layout = html.Div([
        html.Header([
            html.Div([html.Div("↯", className="brand-mark", **{"aria-hidden": "true"}),
                      html.Div("Verfügbarkeit", className="brand-title")], className="brand"),
            html.Div([
                html.Div(dcc.DatePickerRange(id="dates", display_format="DD.MM.YYYY", first_day_of_week=1,
                                             start_date_placeholder_text="Von", end_date_placeholder_text="Bis"),
                         id="dates-wrap", className="dates-wrap", style={"display": "none"}),
                _segmented("range", DEFAULT_RANGE, [("6 Std.", "6h"), ("24 Std.", "24h"), ("7 Tage", "7d"),
                                                    ("30 Tage", "30d"), ("Alles", "all"), ("Von/bis", "custom")]),
            ], className="controls"),
        ], className="topbar"),
        html.Div(id="overview"),
        html.Section([
            _card_head("Verlauf", dcc.Checklist(id="options", value=[TOGGLE_LOG], className="toggles", inline=True, options=[
                {"label": "Ping logarithmisch", "value": TOGGLE_LOG}])),
            dcc.Graph(id="chart", config={"displayModeBar": False, "responsive": True}, figure=empty_figure("Lade …")),
            html.Div("Ziehen zum Zoomen, Doppelklick setzt zurück. Klick auf einen Legendeneintrag blendet die Strecke "
                     "aus. Farbige Flächen: Ausfälle, graue Flächen: Messlücken.", className="chart-hint"),
        ], className="card chart-card"),
        html.Div([
            html.Section([
                _card_head("Je Strecke"),
                html.Div(id="tracks", className="stats-wrap"),
                html.Div("Steigt der Verlust erst ab dem Provider-Hop, liegt die Ursache hinter deinem Router. Steigt er "
                         "schon im Heimnetz, eher beim WLAN oder Router.", className="chart-hint"),
            ], className="card"),
            html.Section([
                _card_head("Nach Tageszeit", _segmented("heat", HEAT_LOSS, [("Paketverlust", HEAT_LOSS),
                                                                             ("Ping P95", HEAT_PING)])),
                dcc.Graph(id="heatmap", config={"displayModeBar": False, "responsive": True},
                          figure=empty_figure("Lade …", height=360), style={"height": "360px"}),
                html.Div("Strecke Internet, je Stunde. Leere Zellen: keine Prüfung.", className="chart-hint"),
            ], className="card"),
        ], className="grid-2"),
        html.Section([
            _card_head("Ausfälle und Messlücken",
                       _segmented("table-filter", "outages", [("Ausfälle", "outages"), ("Nur Leitung", "line"),
                                                              ("Messlücken", "gaps"), ("Alle", "all")]),
                       note_id="table-count"),
            html.Div(_table(), className="table-wrap"),
        ], className="card table-card"),
        dcc.Interval(id="tick", interval=config.dashboard.refresh_s * 1000),
    ], className="shell")

    @app.callback(Output("dates-wrap", "style"), Input("range", "value"))
    def toggle_dates(choice: str) -> dict[str, str]:
        return {"display": "block"} if choice == "custom" else {"display": "none"}

    @app.callback(
        Output("chart", "figure"), Output("heatmap", "figure"), Output("overview", "children"),
        Output("tracks", "children"), Output("table", "data"), Output("table-count", "children"),
        Input("tick", "n_intervals"), Input("range", "value"), Input("dates", "start_date"),
        Input("dates", "end_date"), Input("options", "value"), Input("heat", "value"), Input("table-filter", "value"),
    )
    def refresh(_tick: int | None, choice: str, custom_start: str | None, custom_end: str | None, options: list[str],
                heat: str, table_filter: str) -> tuple[Any, ...]:
        if not Store.exists(config.store_dir):
            message = "Noch keine Daten. Starte die Messung mit: linkmon run"
            return empty_figure(message), empty_figure(message, height=360), [], [], [], ""
        now = now_utc()
        start, end = _range_bounds(choice, custom_start, custom_end, zone, now)
        store = Store(config.store_dir, readonly=True)
        view = load_view(config, store, start, end, now)
        overview = build_overview(store, view["samples"], view["stats"], view["outages"], view["coverage"], config, now)
        # Der Zoom bleibt beim Aktualisieren erhalten und wird erst bei einem neuen Zeitraum zurückgesetzt.
        revision = f"{choice}-{custom_start}-{custom_end}"
        figure = build_figure(view["samples"], view["outages"], view["gaps"], view["start"], view["end"], zone,
                              log_latency=TOGGLE_LOG in options, revision=revision)
        heatmap = build_heatmap(view["samples"], view["start"], view["end"], zone, heat)
        rows = filter_rows(table_rows(view["outages"], view["gaps"], zone), table_filter)
        return figure, heatmap, overview, track_table(view["stats"], view["samples"]), rows, f"{len(rows)} Einträge"

    return app
