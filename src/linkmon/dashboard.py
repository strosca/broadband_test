"""Lokales Webinterface zur Verfügbarkeit: dunkles Layout, aktualisiert sich automatisch."""

from __future__ import annotations

import json
import logging
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from dash import Dash, Input, Output, State, dash_table, dcc, html, no_update
from dash.exceptions import PreventUpdate
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
    BUCKET_STEPS_MIN,
    ping_tracks,
    track_stats,
)
from linkmon.linestats import dropouts_from_samples
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
LEVEL_COLORS_DARK = {"line": "#e66767", "router": "#c98500", "local": "#9085e9", "dns_home": "#3fa7d6",
                     "dns": "#d55181", "https": "#4fc3d9"}  # HTTPS nicht grün: Grün heißt hier „alles ok“.
# Ampel für Ping und Verlust: Grün heißt normal, darüber beginnt die Skala sprunghaft bei Gelb.
# So ist 0 % Verlust sofort von 0,1 % zu unterscheiden, und 20 ms nicht mit 80 ms zu verwechseln.
COLOR_OK = "#2f9e44"
SEVERITY_FLOOR = 0.12
SEVERITY_SCALE = [[0.0, COLOR_OK], [SEVERITY_FLOOR, COLOR_OK], [SEVERITY_FLOOR, "#e3c23b"],
                  [0.5, "#f08c2b"], [1.0, "#e5484d"]]
# Verlust: ab 0,05 % beginnt Gelb, ab 5 % ist es voll rot.
LOSS_LOW_PCT = 0.05
LOSS_HIGH_PCT = 5.0
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
# Die Live-Prüfung liest nur das Ende der Messdatei und die kleine Ausfalldatei. Sie läuft deshalb
# jede Sekunde, die ganze Seite wird aber nur neu berechnet, wenn sich der Zustand ändert.
LIVE_POLL_MS = 1000
_CACHE = SampleCache()
# Unterhalb dieser Breite gilt die Seite als Handyansicht: kompakte Grafiken, kein Ziehen zum Zoomen.
MOBILE_MAX_PX = 720
SHORT_LEVEL_LABELS = {"line": "Leitung", "router": "Router", "local": "Lokal", "dns_home": "Pi-hole",
                      "dns": "DNS", "https": "HTTPS", KIND_GAP: "Lücke"}
OUTAGE_LIST_LIMIT = 8


def loss_severity(values: Any) -> Any:
    """0 bleibt 0 (grün). Jeder Verlust darüber landet mindestens bei SEVERITY_FLOOR, logarithmisch bis 1."""
    pct = np.asarray(values, dtype=float)
    span = np.log10(LOSS_HIGH_PCT / LOSS_LOW_PCT)
    with np.errstate(divide="ignore", invalid="ignore"):
        scaled = np.clip(np.log10(pct / LOSS_LOW_PCT) / span, 0, 1)
    result = np.where(pct > 0, SEVERITY_FLOOR + (1 - SEVERITY_FLOOR) * scaled, 0.0)
    return np.where(np.isnan(pct), np.nan, result)


def ping_limits(median_ms: float | None) -> tuple[float, float]:
    """Grenze für „normal“ und für „voll rot“, gemessen am üblichen Ping der Strecke.

    Normal ist der Median plus etwas Luft (mindestens 5 ms). Bei 17 ms Median also bis etwa 25 ms grün,
    ab etwa 100 ms rot. Für das Heimnetz mit 1 ms ist schon 10 ms auffällig.
    """
    median = median_ms if median_ms and median_ms > 0 else 20.0
    good = median + max(5.0, 0.5 * median)
    return good, max(4 * good, good + 60)


def ping_severity(values: Any, good: float, bad: float) -> Any:
    ms = np.asarray(values, dtype=float)
    with np.errstate(divide="ignore", invalid="ignore"):
        scaled = np.clip(np.log(ms / good) / np.log(bad / good), 0, 1)
    result = np.where(ms > good, SEVERITY_FLOOR + (1 - SEVERITY_FLOOR) * scaled, 0.0)
    return np.where(np.isnan(ms), np.nan, result)


def severity_tone(severity: float | None) -> str:
    """Grob in drei Stufen für Kacheln und Tabelle."""
    if severity is None or np.isnan(severity):
        return "tone-none"
    if severity <= 0:
        return "tone-ok"
    return "tone-warn" if severity < 0.5 else "tone-bad"


def short_duration(seconds: float) -> str:
    total = int(round(seconds))
    if total < 60:
        return f"{total} s"
    if total < 3600:
        minutes, secs = divmod(total, 60)
        return f"{minutes} min {secs} s" if secs else f"{minutes} min"
    hours, rest = divmod(total, 3600)
    return f"{hours} h {rest // 60} min"


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


def _time_axes(figure: go.Figure, mobile: bool = False) -> None:
    # Das Format folgt dem Zoom, sonst stünde bei kurzen Zeiträumen mehrfach dieselbe Minute da.
    # Am Handy zweizeilig und mit wenigen Ticks: Schräge Beschriftungen fressen dort die halbe Höhe.
    joiner = "<br>" if mobile else " "
    figure.update_xaxes(gridcolor=COLOR_GRID, linecolor=COLOR_AXIS, showgrid=False, ticks="outside", ticklen=6,
                        tickcolor="rgba(0,0,0,0)", tickangle=0, nticks=5 if mobile else None,
                        tickfont={"size": 10 if mobile else 12}, tickformatstops=[
                            {"dtickrange": [None, 59999], "value": "%H:%M:%S"},
                            {"dtickrange": [60000, 43199999], "value": f"%H:%M{joiner}%d.%m." if mobile
                             else "%d.%m. %H:%M"},
                            {"dtickrange": [43200000, None], "value": "%d.%m."},
                        ])
    figure.update_yaxes(gridcolor=COLOR_GRID, linecolor=COLOR_AXIS, zeroline=False,
                        title_font={"color": COLOR_INK_3, "size": 11})


def _interaction(mobile: bool) -> dict[str, Any]:
    """Am Handy nichts ziehen oder zoomen: Wischen soll die Seite scrollen, Tippen zeigt den Wert."""
    if mobile:
        return {"dragmode": False, "hovermode": "closest"}
    return {"dragmode": "zoom", "hovermode": "closest"}


def _timeline_rows(outages: list[Outage], gaps: list[tuple[datetime, datetime]]) -> list[str]:
    """Die Leitung steht immer oben, auch ohne Ausfall: Eine durchgehend grüne Zeile ist die gute Nachricht."""
    present = {outage.level for outage in outages} | {"line"}
    rows = [level for level in LEVEL_ORDER if level in present]
    if gaps:
        rows.append(KIND_GAP)
    return rows


def _covered(start: datetime, end: datetime, gaps: list[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    """Zeit, in der gemessen wurde: der Zeitraum ohne die Messlücken."""
    pieces: list[tuple[datetime, datetime]] = []
    cursor = start
    for begin, finish in sorted(gaps):
        if begin > cursor:
            pieces.append((cursor, min(begin, end)))
        cursor = max(cursor, finish)
    if cursor < end:
        pieces.append((cursor, end))
    return pieces


def _outage_text(outage: Outage, zone: ZoneInfo) -> str:
    suffix = " (läuft noch)" if outage.ongoing else ("" if outage.confirmed else " (Ende unbestätigt)")
    return (f"<b>{LEVEL_LABELS.get(outage.level, outage.level)}</b><br>{format_local(outage.start, zone)}<br>"
            f"Dauer {short_duration(outage.duration_s)}{suffix}<br>{outage.detail}")


# Mögliche Abstände der Zeitachse, gewählt wird der kleinste mit höchstens TIMELINE_TICKS Marken.
TICK_STEPS_H = (1, 2, 3, 6, 12, 24, 48, 96, 168, 336, 720, 1440, 2880)
TIMELINE_TICKS = 4


def _timeline_ticks(start: datetime, end: datetime, zone: ZoneInfo) -> list[tuple[float, str]]:
    span_h = max((end - start).total_seconds() / 3600, 1 / 60)
    step = next((hours for hours in TICK_STEPS_H if span_h / hours <= TIMELINE_TICKS), TICK_STEPS_H[-1])
    local = start.astimezone(zone)
    first = local.replace(minute=0, second=0, microsecond=0)
    if step >= 24:
        first = first.replace(hour=0)
    else:
        first = first.replace(hour=first.hour - first.hour % step)
    ticks = []
    moment = first
    while moment <= end:
        share = 100 * (moment - start).total_seconds() / (end - start).total_seconds()
        if 4 <= share <= 96:  # Am Rand würde die Beschriftung über die Leiste hinausragen.
            text = f"{moment:%d.%m.}" if step >= 24 else f"{moment:%H:%M}\n{moment:%d.%m.}"
            ticks.append((share, text))
        # Über die Zeitzone rechnen, damit Marken bei der Zeitumstellung auf runden Stunden bleiben.
        moment = (moment.astimezone(UTC) + timedelta(hours=step)).astimezone(zone)
    return ticks


def outage_timeline(outages: list[Outage], gaps: list[tuple[datetime, datetime]], start: datetime,
                    end: datetime, zone: ZoneInfo) -> html.Div:
    """Wann war was weg, als reines HTML: kommt mit der ersten Antwort und braucht kein Plotly.

    Je Ebene eine Zeile, grün solange gemessen wurde. Jeder Ausfall ist mindestens 3 Pixel breit, damit auch
    Aussetzer von Sekunden bei 30 Tagen sichtbar bleiben. Details beim Darüberfahren und in der Liste darunter.
    """
    total = max((end - start).total_seconds(), 1.0)

    def place(begin: datetime, finish: datetime) -> dict[str, str]:
        left = max(0.0, (begin - start).total_seconds() / total * 100)
        right = min(100.0, (finish - start).total_seconds() / total * 100)
        return {"left": f"{left:.3f}%", "width": f"{max(right - left, 0):.3f}%"}

    covered = _covered(start, end, gaps)
    rows = []
    for row in _timeline_rows(outages, gaps):
        marks: list[Any] = []
        if row == KIND_GAP:
            label = KIND_GAP
            for begin, finish in gaps:
                marks.append(html.Div(className="tl-hit tl-gap", style=place(begin, finish),
                                      title=f"Messlücke {begin.astimezone(zone):%d.%m. %H:%M}, "
                                            f"{short_duration((finish - begin).total_seconds())}"))
        else:
            label = LEVEL_LABELS.get(row, row)
            marks += [html.Div(className="tl-ok", style=place(begin, finish)) for begin, finish in covered]
            color = LEVEL_COLORS_DARK.get(row, COLOR_INK_2)
            for outage in (item for item in outages if item.level == row):
                marks.append(html.Div(
                    className="tl-hit", style={**place(outage.start, outage.end), "background": color},
                    title=f"{label} {outage.start.astimezone(zone):%d.%m. %H:%M:%S}, "
                          f"{short_duration(outage.duration_s)}"))
        rows.append(html.Div([
            html.Div([html.Span(SHORT_LEVEL_LABELS.get(row, row), className="tl-short"),
                      html.Span(label, className="tl-long")], className="tl-label"),
            html.Div(marks, className="tl-track"),
        ], className="tl-row", **{"data-row": row}))
    axis = html.Div([html.Div([html.Span(line) for line in text.split("\n")], className="tl-tick",
                              style={"left": f"{share:.3f}%"})
                     for share, text in _timeline_ticks(start, end, zone)], className="tl-axis")
    rows.append(html.Div([html.Div(className="tl-label"), axis], className="tl-row tl-axis-row"))
    return html.Div(rows, className="timeline", role="img",
                    **{"aria-label": "Zeitleiste der Ausfälle: " + outage_summary(outages)})


def outage_summary(outages: list[Outage]) -> str:
    if not outages:
        return "Keine Ausfälle"
    counts = [(level, sum(1 for outage in outages if outage.level == level)) for level in LEVEL_ORDER]
    parts = [f"{count}× {SHORT_LEVEL_LABELS.get(level, level)}" for level, count in counts if count]
    line = total_seconds([(o.start, o.end) for o in outages if o.level == "line"])
    text = ", ".join(parts)
    return text + (f" · Leitung zusammen {short_duration(line)}" if line else "")


def outage_list(outages: list[Outage], zone: ZoneInfo, limit: int = OUTAGE_LIST_LIMIT) -> html.Div:
    """Die letzten Ausfälle als kurze Liste: am Handy lesbarer als jede Tabelle."""
    if not outages:
        return html.Div("Im gewählten Zeitraum lief alles.", className="outage-empty")
    items = []
    for outage in sorted(outages, key=lambda item: item.start, reverse=True)[:limit]:
        color = LEVEL_COLORS_DARK.get(outage.level, COLOR_INK_2)
        state = "läuft noch" if outage.ongoing else short_duration(outage.duration_s)
        items.append(html.Li([
            html.Span(className="outage-mark", style={"background": color}),
            html.Div([
                html.Div([html.Span(LEVEL_LABELS.get(outage.level, outage.level), className="outage-level"),
                          html.Span(state, className="outage-duration" + (" is-live" if outage.ongoing else ""))],
                         className="outage-top"),
                html.Div(f"{outage.start.astimezone(zone):%d.%m. %H:%M:%S}", className="outage-time"),
                html.Div(outage.detail, className="outage-detail"),
            ], className="outage-body"),
        ], className="outage-item"))
    rest = len(outages) - limit
    children: list[Any] = [html.Ul(items, className="outage-list")]
    if rest > 0:
        children.append(html.Div(f"und {rest} weitere, alle in der Tabelle unter Details", className="outage-more"))
    return html.Div(children)


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


def track_options(samples: pd.DataFrame) -> list[dict[str, Any]]:
    """Strecken zum Ein- und Ausblenden. Das Farbfeld ersetzt die Legende, die am Handy alles verdeckt."""
    return [{"label": html.Span([html.Span(className="swatch", style={"background": series_color(position)}),
                                 track.label]), "value": track.key}
            for position, track in enumerate(ping_tracks(samples))]


def _strip_size(start: datetime, end: datetime, columns: int) -> pd.Timedelta:
    span_min = max((end - start).total_seconds() / 60, 1)
    step = next((step for step in BUCKET_STEPS_MIN if span_min / step <= columns), BUCKET_STEPS_MIN[-1])
    return pd.Timedelta(minutes=step)


def build_figure(samples: pd.DataFrame, outages: list[Outage], gaps: list[tuple[datetime, datetime]],
                 start: datetime, end: datetime, zone: ZoneInfo, *, selected: list[str] | None = None,
                 log_latency: bool = False, mobile: bool = False, revision: str = "linkmon") -> go.Figure:
    """Ping und Verlust der gewählten Strecken.

    Ping linear: Seltene Spitzen würden die Achse strecken, deshalb endet sie knapp über dem Üblichen,
    und was darüber liegt, steht als Dreieck am oberen Rand. Punkte über dem Normalwert tragen die Ampelfarbe.
    Verlust als Farbstreifen je Strecke: grün bei 0, sonst Ampel. Auch 0,2 % fallen so sofort auf.
    """
    every = ping_tracks(samples)
    chosen = [(position, track) for position, track in enumerate(every)
              if selected is None or track.key in selected]
    if selected is None:
        chosen = [(position, track) for position, track in chosen if track.key == INTERNET] or chosen[:1]
    strips = max(len(chosen), 1)
    ping_px = 230 if mobile else 330
    strip_px = 24 * strips + 20
    figure = make_subplots(rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.0,
                           row_heights=[ping_px, strip_px + 40])
    size = bucket_size(start, end)
    minutes = size / pd.Timedelta(minutes=1)
    size_text = f"{minutes / 60:g} h" if minutes >= 60 else f"{minutes:g} min"

    # Der Verluststreifen braucht gröbere Spalten als der Ping: Bei 1440 Minuten auf 350 Pixeln wäre jede
    # Spalte schmaler als ein Pixel, und einzelne Verluste verschwänden im Grün.
    strip_size = _strip_size(start, end, 120 if mobile else 360)
    tables = []
    for position, track in chosen:
        table = bucket_table(samples, track, size)
        values = table["median"].dropna()
        good, bad = ping_limits(float(values.median()) if len(values) else None)
        tables.append((position, track, table, good, bad))

    typical = [float(table["median"].quantile(0.99)) for _, _, table, _, _ in tables
               if table["median"].notna().any()]
    goods = [good for _, _, _, good, _ in tables]
    cap = max(max(goods, default=30.0) * 2.5, max(typical, default=0.0) * 1.15)

    for position, track, table, good, bad in tables:
        color = series_color(position)
        times = _naive_index(table.index, zone)
        extra = table[["p95", "max", "loss_pct", "sent"]].to_numpy()
        hover = ("%{y:.0f} ms, P95 %{customdata[0]:.0f}, max %{customdata[1]:.0f} ms<extra>" + track.label
                 + "</extra>")
        if track.key == INTERNET and len(chosen) == 1:
            band_x, band_y = _band(times, table["median"], table["p95"])
            figure.add_trace(go.Scatter(x=band_x, y=band_y, mode="lines", fill="toself", line={"width": 0},
                                        fillcolor=_transparent(color, 0.15), hoverinfo="skip", showlegend=False),
                             row=1, col=1)
        figure.add_trace(go.Scatter(x=times, y=table["median"], name=track.label, mode="lines",
                                    line={"color": color, "width": 1.5}, connectgaps=False, customdata=extra,
                                    hovertemplate=hover), row=1, col=1)
        severity = ping_severity(table["median"], good, bad)
        high = severity > 0
        if high.any():
            shown = table["median"][high]
            if not log_latency:
                shown = shown.clip(upper=cap)
            figure.add_trace(go.Scatter(
                x=times[high], y=shown, mode="markers", name=track.label + " erhöht", showlegend=False,
                marker={"color": severity[high], "colorscale": SEVERITY_SCALE, "cmin": 0, "cmax": 1, "size": 7,
                        "symbol": np.where(table["median"][high] > cap, "triangle-up", "circle")
                        if not log_latency else "circle",
                        "line": {"width": 0}},
                # Bei gedeckelten Spitzen zeigt y nur den Rand: Der echte Wert steht in text.
                customdata=extra[high], text=[f"{value:.0f}" for value in table["median"][high]],
                hovertemplate=hover.replace("%{y:.0f}", "%{text}"),
            ), row=1, col=1)
        strip = bucket_table(samples, track, strip_size)
        loss = strip["loss_pct"].to_numpy(dtype=float)
        figure.add_trace(go.Heatmap(
            x=_naive_index(strip.index + strip_size / 2, zone), y=[track.label], z=[loss_severity(loss)], customdata=[np.nan_to_num(loss, nan=-1)],
            colorscale=SEVERITY_SCALE, zmin=0, zmax=1, showscale=False, name=track.label + " Verlust",
            hovertemplate="%{x|%d.%m. %H:%M}: %{customdata:.2f} % Verlust<extra>" + track.label + "</extra>", ygap=3,
        ), row=2, col=1)

    if len(chosen) == 1 and tables:
        figure.add_hrect(y0=0, y1=tables[0][3], fillcolor=COLOR_OK, opacity=0.08, line_width=0, layer="below",
                         row=1, col=1)
    for outage in outages:
        figure.add_vrect(x0=_naive(outage.start, zone), x1=_naive(outage.end, zone), row=1, col=1,
                         fillcolor=LEVEL_COLORS_DARK.get(outage.level, COLOR_INK_2), opacity=0.25,
                         line_width=1, line_color=LEVEL_COLORS_DARK.get(outage.level, COLOR_INK_2), layer="below")
    for begin, finish in gaps:
        figure.add_vrect(x0=_naive(begin, zone), x1=_naive(finish, zone), row=1, col=1, fillcolor=COLOR_GAP,
                         opacity=0.4, line_width=0, layer="below")

    figure.update_layout(height=ping_px + strip_px + 70, margin={"l": 44 if mobile else 54, "r": 12, "t": 30, "b": 10},
                         uirevision=revision, showlegend=False, **_interaction(mobile), **_base_layout())
    _time_axes(figure, mobile)
    figure.update_xaxes(range=[_naive(start, zone), _naive(end, zone)], fixedrange=mobile)
    figure.update_xaxes(showticklabels=False, row=1, col=1)
    if log_latency:
        ticks = [1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000]
        figure.update_yaxes(title_text="ms", type="log", tickvals=ticks, ticktext=[str(t) for t in ticks],
                            fixedrange=mobile, row=1, col=1)
    else:
        figure.update_yaxes(title_text="ms", range=[0, cap], fixedrange=mobile, row=1, col=1)
    figure.update_yaxes(row=2, col=1, type="category", showgrid=False, fixedrange=True, autorange="reversed",
                        showticklabels=False)
    figure.add_annotation(text=f"Ping (Median je {size_text})", xref="paper", yref="paper", x=0, y=1, yshift=24,
                          xanchor="left", showarrow=False, font={"size": 12, "color": COLOR_INK_3})
    figure.add_annotation(text="Paketverlust (grün: 0 %)", xref="paper", yref="y2 domain", x=0, y=1, yshift=4,
                          xanchor="left", yanchor="bottom", showarrow=False, font={"size": 12, "color": COLOR_INK_3})
    if samples.empty:
        figure.add_annotation(text="Im gewählten Zeitraum gibt es keine Prüfungen", xref="paper", yref="paper",
                              x=0.5, y=0.55, showarrow=False, font={"color": COLOR_INK_3, "size": 14})
    return figure


def build_heatmap(samples: pd.DataFrame, start: datetime, end: datetime, zone: ZoneInfo, value: str,
                  *, mobile: bool = False) -> go.Figure:
    track = find_track(samples, INTERNET)
    height = 420 if mobile else 360
    if track is None or samples.empty:
        return empty_figure("Keine Prüfungen im Zeitraum", height=height)
    days = days_between(start, end, zone)
    matrix = hour_matrix(samples, track, zone, value, days)
    raw = matrix.to_numpy(dtype=float)
    if value == HEAT_LOSS:
        severity, label, unit, digits = loss_severity(raw), "Paketverlust", "%", 2
        ticks = [(0.0, "0"), (float(loss_severity(0.05)), "0,05"), (float(loss_severity(0.5)), "0,5"),
                 (1.0, "≥5")]
    else:
        stats = track_stats(samples, track)
        good, bad = ping_limits(stats.median_ms)
        severity, label, unit, digits = ping_severity(raw, good, bad), "Ping P95", "ms", 0
        ticks = [(0.0, f"≤{good:.0f}"), (float(ping_severity(2 * good, good, bad)), f"{2 * good:.0f}"),
                 (1.0, f"≥{bad:.0f}")]
    labels = [f"{day[8:10]}.{day[5:7]}." for day in matrix.columns]
    hours = [f"{hour:02d}:00" for hour in matrix.index]
    figure = go.Figure(go.Heatmap(
        z=severity, customdata=raw, x=labels, y=hours, colorscale=SEVERITY_SCALE, zmin=0, zmax=1, xgap=2, ygap=2,
        hovertemplate=f"%{{x}} %{{y}}<br>{label}: %{{customdata:.{digits}f}} {unit}<extra></extra>",
        colorbar={"title": {"text": unit, "font": {"color": COLOR_INK_3, "size": 11}}, "thickness": 8,
                  "outlinewidth": 0, "tickfont": {"color": COLOR_INK_3, "size": 10},
                  "tickvals": [tick for tick, _ in ticks], "ticktext": [text for _, text in ticks]},
    ))
    figure.update_layout(height=height, margin={"l": 46, "r": 6, "t": 10, "b": 30}, dragmode=False,
                         **_base_layout())
    figure.update_xaxes(showgrid=False, linecolor=COLOR_AXIS, type="category", fixedrange=True)
    figure.update_yaxes(showgrid=False, autorange="reversed", type="category", dtick=3, fixedrange=True)
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


def report_bounds(config: Config, store: Store, choice: str, custom_start: str | None, custom_end: str | None,
                  now: datetime) -> tuple[datetime, datetime]:
    """Zeitraum des Berichts zur Auswahl im Webinterface.

    „Alles“ beginnt am Tag der ersten Prüfung statt im Jahr 2000, und kein Bericht reicht in die Zukunft.
    """
    start, end = _range_bounds(choice, custom_start, custom_end, config.zone, now)
    earliest = store.first_cycle_time()
    if earliest and choice == "all":
        local = parse_iso(earliest).astimezone(config.zone)
        start = max(start, local.replace(hour=0, minute=0, second=0, microsecond=0))
    end = min(end, now)
    if end <= start:
        start = end - timedelta(hours=1)
    return start, end


def report_filename(start: datetime, end: datetime, zone: ZoneInfo) -> str:
    last = (end - timedelta(seconds=1)).astimezone(zone)
    return f"stoerungsprotokoll_{start.astimezone(zone):%Y-%m-%d}_bis_{last:%Y-%m-%d}.pdf"


def create_report_file(config: Config, choice: str, custom_start: str | None, custom_end: str | None,
                       now: datetime) -> tuple[Path, str]:
    """Baut den Bericht wie „linkmon report“ und gibt PDF-Pfad und Dateinamen für den Download zurück.

    Alles bleibt zusätzlich auf dem Messgerät unter data/berichte, samt Abbildungen und Rohdaten mit Prüfsumme.
    """
    from linkmon.report import build_report  # Erst beim Klick laden: reportlab braucht sonst unnötig Speicher.

    store = Store(config.store_dir, readonly=True)
    start, end = report_bounds(config, store, choice, custom_start, custom_end, now)
    stamp = now.astimezone(config.zone).strftime("%Y%m%d_%H%M%S")
    result = build_report(config, store, start, end, config.data_dir / "berichte" / f"bericht_{stamp}")
    return result.pdf, report_filename(start, end, config.zone)


CHECK_LABELS = {"lan": "Heimnetz", "isp": "Provider-Hop", "ext": "Internet", "dns": "DNS",
                "dnshome": "DNS Heimnetz (Pi-hole)", "dnsref": "DNS öffentlich", "https": "HTTPS",
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


def live_signature(store: Store) -> str:
    """Fingerabdruck des aktuellen Zustands: gestörte Prüfungen, offene Ausfälle, Zahl der Ausfälle.

    Bleibt er gleich, passiert nichts. Ändert er sich (Störung beginnt, Ausfall wird geloggt, Verbindung ist
    zurück), baut die Seite sofort neu auf, statt bis zur nächsten regulären Aktualisierung zu warten.
    """
    newest = store.latest_cycle()
    failed: list[str] = []
    if newest and newest.get("results"):
        results: dict[str, list[Any]] = json.loads(newest["results"])
        results.pop(LEGACY_SPEEDTEST_MARKER, None)
        failed = sorted(name for name, item in results.items() if not item[0])
    outages = store.outages()
    open_ids = [row["id"] for row in outages if row["end_utc"] is None]
    return json.dumps([failed, open_ids, len(outages)])


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


def _kpi(label: str, value: str, unit: str = "", note: str = "", tone: str = "tone-none") -> html.Div:
    """Kachel mit Ampelpunkt. Die Farbe ergänzt nur: Wert und Text sagen dasselbe auch ohne sie."""
    return html.Div([
        html.Div([html.Span(className="kpi-dot"), label], className="kpi-label"),
        html.Div([value, html.Span(unit, className="kpi-unit")] if unit else value, className="kpi-value"),
        html.Div(note, className="kpi-note"),
    ], className=f"card kpi {tone}")


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
    if availability is None:
        availability_tone = "tone-none"
    else:
        availability_tone = "tone-ok" if line_down == 0 else ("tone-warn" if availability >= 99.9 else "tone-bad")
    outage_tone = "tone-ok" if not outages else ("tone-warn" if line_down < 300 else "tone-bad")
    ping_tone = loss_tone = "tone-none"
    if internet and internet.median_ms is not None and internet.p95_ms is not None:
        # Der Median ist per Definition „normal“: Bewertet wird, wie weit die Spitzen (P95) darüber liegen.
        ping_tone = severity_tone(float(ping_severity(internet.p95_ms, *ping_limits(internet.median_ms))))
    if internet and internet.loss_pct is not None:
        loss_tone = severity_tone(float(loss_severity(internet.loss_pct)))
    overview.append(html.Div([
        _kpi("Verfügbarkeit Leitung", _number(availability, 2), "%" if availability is not None else "",
             f"Überwacht: {format_duration(coverage.monitored_s)}", availability_tone),
        _kpi("Ausfälle", str(len(outages)), "",
             f"Davon Leitung: {line_count}, zusammen {format_duration(line_down)}", outage_tone),
        _kpi("Ping ins Internet", _number(internet.median_ms if internet else None, 0), "ms",
             f"Median · P95 {_ms(internet.p95_ms if internet else None)}", ping_tone),
        _kpi("Paketverlust", _number(internet.loss_pct if internet else None, 2), "%",
             f"{internet.lost} von {internet.sent} Pings unbeantwortet" if internet else "Keine Prüfungen",
             loss_tone),
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
        loss_tone = severity_tone(float(loss_severity(item.loss_pct))) if item.loss_pct is not None else "tone-none"
        p95_tone = "tone-none"
        if item.track.is_ping and item.median_ms is not None and item.p95_ms is not None:
            p95_tone = severity_tone(float(ping_severity(item.p95_ms, *ping_limits(item.median_ms))))
        rows.append(html.Tr([
            html.Td(name, className="track-name"), html.Td(f"{item.sent:,}".replace(",", ".")),
            html.Td(f"{_number(item.loss_pct, 2)} %", className=f"cell-tone {loss_tone}"),
            html.Td(_ms(item.median_ms)), html.Td(_ms(item.p95_ms), className=f"cell-tone {p95_tone}"),
            html.Td(_ms(item.max_ms)), html.Td(_ms(item.jitter_ms) if item.track.is_ping else "–"),
        ]))
    if not rows:
        rows = [html.Tr(html.Td("Keine Prüfungen im Zeitraum", colSpan=7))]
    return html.Table([head, html.Tbody(rows)], className="stats-table")


ALL_INTERFACES = {"0.0.0.0", "", "*"}
WEB_THREADS = 8
# Adapter von WSL, Hyper-V, Docker und VMs: Andere Geräte im Heimnetz erreichen sie nicht.
VIRTUAL_ADAPTERS = ("vethernet", "virtualbox", "vmware", "docker", "br-", "veth", "virbr", "loopback")


def listen_urls(host: str, port: int) -> list[str]:
    """Adressen, unter denen das Webinterface erreichbar ist. Bei 0.0.0.0 jede IPv4-Adresse dieses Geräts."""
    if host not in ALL_INTERFACES:
        return [f"http://{host}:{port}"]
    import socket

    import psutil

    addresses = [item.address for name, items in psutil.net_if_addrs().items() for item in items
                 if item.family == socket.AF_INET and not item.address.startswith(("127.", "169.254."))
                 and not name.lower().startswith(VIRTUAL_ADAPTERS)]
    hostname = socket.gethostname()
    return [f"http://{address}:{port}" for address in sorted(set(addresses))] + [f"http://{hostname}:{port}"]


def warm_cache(config: Config) -> None:
    """Liest die Messdateien der letzten 31 Tage schon beim Start, nicht erst beim ersten Seitenaufruf."""
    try:
        if Store.exists(config.store_dir):
            now = now_utc()
            load_samples(Store(config.store_dir, readonly=True), now - timedelta(days=31), now, _CACHE)
    except Exception:  # Nur eine Beschleunigung: Fehler zeigen sich beim ersten Abruf ohnehin.
        logging.getLogger(__name__).debug("Vorwärmen fehlgeschlagen", exc_info=True)


def make_web_server(config: Config, host: str, port: int) -> Any:
    """Ein richtiger Webserver (waitress) statt des Entwicklungsservers von Flask.

    Er bedient mehrere Geräte gleichzeitig. Ist der Port belegt, wirft er OSError.
    """
    from waitress.server import create_server

    logging.getLogger("waitress").setLevel(logging.WARNING)  # Sonst füllt jeder Abruf das Log.
    app = create_app(config)
    threading.Thread(target=warm_cache, args=(config,), name="web-cache", daemon=True).start()
    return create_server(app.server, host="0.0.0.0" if host in ALL_INTERFACES else host, port=port,
                         threads=WEB_THREADS, ident="linkmon")


def start_in_background(config: Config) -> str | None:
    """Startet das Webinterface im Hintergrund. Gibt die Adresse zurück, oder None, wenn der Port belegt ist."""
    host, port = config.dashboard.host, config.dashboard.port
    try:
        server = make_web_server(config, host, port)
    except OSError:
        return None
    threading.Thread(target=server.run, name="web-interface", daemon=True).start()
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
              cache: SampleCache | None = _CACHE, *, all_stats: bool = True) -> dict[str, Any]:
    """Lädt alles für einen Zeitraum. Vor der ersten Prüfung gab es das Tool noch nicht: keine Messlücke.

    Ohne all_stats rechnet es nur die Strecke Internet für die Kacheln. Die übrigen Strecken braucht erst
    die Detailansicht.
    """
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
    outages = sorted(outages + dropouts_from_samples(samples, outages, threshold), key=lambda item: item.start)
    gaps = coverage.gaps if coverage.cycles else []
    return {"samples": samples, "coverage": coverage, "outages": outages, "gaps": gaps, "start": first,
            "end": finish, "stats": all_track_stats(samples) if all_stats else _internet_stats(samples)}


def _internet_stats(samples: pd.DataFrame) -> list[TrackStats]:
    track = find_track(samples, INTERNET)
    return [track_stats(samples, track)] if track else []


def create_app(config: Config) -> Dash:
    # compress: Die Skripte von Dash sind unkomprimiert über 2 MB groß, gzip spart am Handy Sekunden.
    # suppress_callback_exceptions: Die Details werden erst beim Aufklappen eingehängt.
    app = Dash(__name__, title="Verfügbarkeit", update_title=None, compress=True,
               suppress_callback_exceptions=True, meta_tags=[{"name": "viewport", "content": "width=device-width, initial-scale=1"},
                          {"name": "theme-color", "content": "#0d0d0d"}])
    zone = config.zone
    graph_config = {"displayModeBar": False, "responsive": True, "scrollZoom": False}
    def details_layout() -> list[Any]:
        """Verläufe, Strecken, Tageszeit, Tabelle. Erst beim Aufklappen eingehängt: Vorher lädt der Browser
        weder Plotly (4,7 MB) noch die Tabellenkomponente."""
        return [
            html.Section([
                _card_head("Verlauf", dcc.Checklist(id="options", value=[], className="toggles", inline=True,
                                                    options=[{"label": "Ping logarithmisch", "value": TOGGLE_LOG}])),
                dcc.Checklist(id="track-pick", value=[INTERNET], className="toggles track-pick", inline=True,
                              options=[]),
                dcc.Graph(id="chart", config=graph_config, figure=empty_figure("Lade …")),
                html.Div("Punkte in Ampelfarbe: Ping über dem Üblichen der Strecke, Dreiecke: Spitzen über dem "
                         "Achsenende. Verlust: grün bei 0 %, sonst gelb bis rot. Farbige Flächen: Ausfälle, graue: "
                         "Messlücken. Am PC: Ziehen zoomt, Doppelklick setzt zurück.", className="chart-hint"),
            ], className="card chart-card"),
            html.Div([
                html.Section([
                    _card_head("Je Strecke"),
                    html.Div(id="tracks", className="stats-wrap"),
                    html.Div("Steigt der Verlust erst ab dem Provider-Hop, liegt die Ursache hinter deinem Router. "
                             "Steigt er schon im Heimnetz, eher beim WLAN oder Router.", className="chart-hint"),
                ], className="card"),
                html.Section([
                    _card_head("Nach Tageszeit", _segmented("heat", HEAT_LOSS, [("Paketverlust", HEAT_LOSS),
                                                                                 ("Ping P95", HEAT_PING)])),
                    dcc.Graph(id="heatmap", config=graph_config, figure=empty_figure("Lade …", height=360),
                              style={"height": "360px"}),
                    html.Div("Strecke Internet, je Stunde. Grün: kein Verlust bzw. üblicher Ping. Leere Zellen: "
                             "keine Prüfung.", className="chart-hint"),
                ], className="card"),
            ], className="grid-2"),
            html.Section([
                _card_head("Ausfälle und Messlücken",
                           _segmented("table-filter", "outages", [("Ausfälle", "outages"), ("Nur Leitung", "line"),
                                                                  ("Messlücken", "gaps"), ("Alle", "all")]),
                           note_id="table-count"),
                html.Div(_table(), className="table-wrap"),
            ], className="card table-card"),
        ]

    def initial_state() -> tuple[list[Any], Any, str, Any, str | None]:
        """Die Werte für 24 Stunden gleich in die erste Antwort, ohne zweiten Abruf."""
        if not Store.exists(config.store_dir):
            return [], html.Div(), "", html.Div("Noch keine Daten. Starte die Messung mit: linkmon run",
                                                 className="outage-empty"), None
        try:
            parts = main_view(DEFAULT_RANGE, None, None)
            signature = live_signature(Store(config.store_dir, readonly=True))
        except (OSError, ValueError):  # Datei wird gerade geschrieben: Die Seite füllt sich beim nächsten Abruf.
            return [], html.Div(), "Lade …", html.Div(), None
        return (*parts, signature)

    def serve_layout() -> html.Div:
        overview, timeline, note, listing, signature = initial_state()
        return html.Div([
            dcc.Location(id="url"),
            dcc.Store(id="mobile"),
            # Pro Browser gemerkt: Am PC bleiben die Details offen, am Handy zu, wenn man es so eingestellt hat.
            dcc.Store(id="details-open", storage_type="local", data=False),
            html.Header([
                html.Div([html.Div("↯", className="brand-mark", **{"aria-hidden": "true"}),
                          html.Div("Verfügbarkeit", className="brand-title")], className="brand"),
                html.Div([
                    _segmented("range", DEFAULT_RANGE, [("6 Std.", "6h"), ("24 Std.", "24h"), ("7 Tage", "7d"),
                                                        ("30 Tage", "30d"), ("Alles", "all"), ("Von/bis", "custom")]),
                    html.Div(dcc.DatePickerRange(id="dates", display_format="DD.MM.YYYY", first_day_of_week=1,
                                                 start_date_placeholder_text="Von", end_date_placeholder_text="Bis"),
                             id="dates-wrap", className="dates-wrap", style={"display": "none"}),
                    html.Button([html.Span("↓", className="button-icon", **{"aria-hidden": "true"}), "PDF-Bericht"],
                                id="report-button", className="button", n_clicks=0,
                                title="Störungsprotokoll für den gewählten Zeitraum herunterladen"),
                    dcc.Loading(html.Div(id="report-status", className="report-status", role="status"), type="dot",
                                color="#3987e5", delay_show=150),
                    dcc.Download(id="report-download"),
                ], className="controls"),
            ], className="topbar"),
            html.Div(overview, id="overview"),
            html.Section([
                html.Div([html.Div([html.H2("Wann war etwas weg?", className="card-title"),
                                    html.Div(note, id="outage-note", className="card-note")])], className="card-head"),
                html.Div(timeline, id="outage-timeline", className="timeline-wrap"),
                html.Div(listing, id="outage-list", className="outage-wrap"),
            ], className="card outage-card"),
            html.Button("Details anzeigen", id="details-button", className="details-button", n_clicks=0),
            html.Div(id="details", className="details", hidden=True),
            dcc.Interval(id="tick", interval=config.dashboard.refresh_s * 1000),
            dcc.Interval(id="live", interval=LIVE_POLL_MS),
            dcc.Store(id="live-state", data=signature),
        ], className="shell")

    app.layout = serve_layout  # Funktion: bei jedem Seitenaufruf frisch berechnet

    app.clientside_callback(f"function(_) {{ return window.innerWidth <= {MOBILE_MAX_PX}; }}",
                            Output("mobile", "data"), Input("url", "pathname"))
    app.clientside_callback("function(_, open) { return !open; }", Output("details-open", "data"),
                            Input("details-button", "n_clicks"), State("details-open", "data"),
                            prevent_initial_call=True)
    app.clientside_callback(
        "function(open) { return [!open, open ? 'Details ausblenden' "
        ": 'Details anzeigen · Ping, Verlust je Strecke, Tageszeit, Tabelle']; }",
        Output("details", "hidden"), Output("details-button", "children"), Input("details-open", "data"))

    @app.callback(Output("dates-wrap", "style"), Input("range", "value"))
    def toggle_dates(choice: str) -> dict[str, str]:
        return {"display": "block"} if choice == "custom" else {"display": "none"}

    @app.callback(Output("live-state", "data"), Input("live", "n_intervals"), State("live-state", "data"))
    def watch(_pulse: int | None, previous: str | None) -> Any:
        if not Store.exists(config.store_dir):
            return no_update
        try:
            current = live_signature(Store(config.store_dir, readonly=True))
        except (OSError, ValueError):  # Datei wird gerade geschrieben: beim nächsten Puls erneut.
            return no_update
        return no_update if current == previous else current

    def main_view(choice: str, custom_start: str | None, custom_end: str | None) -> tuple[Any, ...]:
        store, view, now = _view(choice, custom_start, custom_end, all_stats=False)
        overview = build_overview(store, view["samples"], view["stats"], view["outages"], view["coverage"], config, now)
        timeline = outage_timeline(view["outages"], view["gaps"], view["start"], view["end"], zone)
        return overview, timeline, outage_summary(view["outages"]), outage_list(view["outages"], zone)

    def _view(choice: str, custom_start: str | None, custom_end: str | None, *,
              all_stats: bool) -> tuple[Store, dict[str, Any], datetime]:
        now = now_utc()
        start, end = _range_bounds(choice, custom_start, custom_end, zone, now)
        store = Store(config.store_dir, readonly=True)
        return store, load_view(config, store, start, end, now, all_stats=all_stats), now

    @app.callback(
        Output("overview", "children"), Output("outage-timeline", "children"), Output("outage-note", "children"),
        Output("outage-list", "children"),
        Input("tick", "n_intervals"), Input("live-state", "data"), Input("range", "value"),
        Input("dates", "start_date"), Input("dates", "end_date"), prevent_initial_call=True,
    )
    def refresh(_tick: int | None, _live: str | None, choice: str, custom_start: str | None,
                custom_end: str | None) -> tuple[Any, ...]:
        """Das Wichtigste: Zustand, Kacheln und wann etwas ausfiel. Der erste Stand kommt schon mit der Seite."""
        if not Store.exists(config.store_dir):
            return [], html.Div(), "", html.Div("Noch keine Daten. Starte die Messung mit: linkmon run",
                                                 className="outage-empty")
        return main_view(choice, custom_start, custom_end)

    @app.callback(Output("details", "children"), Input("details-open", "data"), State("details", "children"))
    def mount_details(open_: bool | None, children: Any) -> Any:
        return details_layout() if open_ and not children else no_update

    @app.callback(
        Output("chart", "figure"), Output("heatmap", "figure"), Output("tracks", "children"),
        Output("table", "data"), Output("table-count", "children"), Output("track-pick", "options"),
        Output("heatmap", "style"),
        Input("tick", "n_intervals"), Input("live-state", "data"), Input("range", "value"),
        Input("dates", "start_date"), Input("dates", "end_date"), Input("details-open", "data"),
        Input("options", "value"), Input("track-pick", "value"), Input("heat", "value"),
        Input("table-filter", "value"), Input("mobile", "data"),
    )
    def refresh_details(_tick: int | None, _live: str | None, choice: str, custom_start: str | None,
                        custom_end: str | None, open_: bool | None, options: list[str], picked: list[str],
                        heat: str, table_filter: str, mobile: bool | None) -> tuple[Any, ...]:
        """Verläufe, Strecken, Tageszeit und Tabelle: nur berechnet und übertragen, wenn aufgeklappt."""
        if not open_:
            raise PreventUpdate
        if not Store.exists(config.store_dir):
            message = "Noch keine Daten. Starte die Messung mit: linkmon run"
            return empty_figure(message), empty_figure(message, height=360), [], [], "", [], no_update
        _, view, _ = _view(choice, custom_start, custom_end, all_stats=True)
        mobile = bool(mobile)
        revision = f"{choice}-{custom_start}-{custom_end}-{mobile}"
        figure = build_figure(view["samples"], view["outages"], view["gaps"], view["start"], view["end"], zone,
                              selected=picked or [], log_latency=TOGGLE_LOG in (options or []), mobile=mobile,
                              revision=revision)
        heatmap = build_heatmap(view["samples"], view["start"], view["end"], zone, heat, mobile=mobile)
        rows = filter_rows(table_rows(view["outages"], view["gaps"], zone), table_filter)
        return (figure, heatmap, track_table(view["stats"], view["samples"]), rows, f"{len(rows)} Einträge",
                track_options(view["samples"]), {"height": f"{heatmap.layout.height}px"})

    @app.callback(
        Output("report-download", "data"), Output("report-status", "children"),
        Input("report-button", "n_clicks"), State("range", "value"), State("dates", "start_date"),
        State("dates", "end_date"), prevent_initial_call=True,
    )
    def download_report(clicks: int, choice: str, custom_start: str | None,
                        custom_end: str | None) -> tuple[Any, str]:
        if not clicks:
            return no_update, ""
        if not Store.exists(config.store_dir):
            return no_update, "Noch keine Daten für einen Bericht"
        if choice == "custom" and not (custom_start and custom_end):
            return no_update, "Bitte zuerst Von und Bis wählen"
        try:
            pdf, filename = create_report_file(config, choice, custom_start, custom_end, now_utc())
        except Exception:  # Ein Fehler im Bericht darf das Webinterface nicht lahmlegen.
            logging.getLogger(__name__).exception("PDF-Bericht fehlgeschlagen")
            return no_update, "Bericht fehlgeschlagen, Details im Log"
        return dcc.send_file(str(pdf), filename=filename), f"Gespeichert: {filename}"

    return app
