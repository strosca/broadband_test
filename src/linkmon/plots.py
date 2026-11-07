"""Abbildungen für den Bericht, jeweils als PDF, SVG und PNG (300 dpi)."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import matplotlib
import matplotlib.dates as mdates
import numpy as np
import pandas as pd
from matplotlib.axes import Axes
from matplotlib.colors import LinearSegmentedColormap
from matplotlib.figure import Figure
from matplotlib.lines import Line2D
from matplotlib.patches import Patch

from linkmon.levels import LEVEL_COLORS, LEVEL_LABELS, LEVEL_ORDER
from linkmon.quality import INTERNET, Track, bucket_size, bucket_table, days_between, find_track, hour_matrix, ping_tracks
from linkmon.stats import Coverage, Outage

matplotlib.use("Agg")

# Kategoriale Reihenfolge der geprüften Palette (helle Stufen). Die Farbe folgt der Strecke.
SERIES_COLORS = ("#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6")
COLOR_TEXT = "#0b0b0b"
COLOR_MUTED = "#52514e"
COLOR_GRID = "#e1e0d9"
COLOR_SURFACE = "#ffffff"
COLOR_GAP = "#d9d8d2"
COLOR_NO_DATA = "#efeeea"

SEQUENTIAL = LinearSegmentedColormap.from_list("linkmon_blue", ["#e4eefa", "#86b3e8", "#2a78d6", "#123e78"])

STYLE = {
    # DejaVu Sans liegt matplotlib bei: Die Abbildungen sehen auf Windows und Linux gleich aus.
    "font.family": "DejaVu Sans",
    "font.size": 9,
    "axes.edgecolor": COLOR_GRID,
    "axes.labelcolor": COLOR_MUTED,
    "axes.spines.top": False,
    "axes.spines.right": False,
    "axes.grid": True,
    "axes.axisbelow": True,
    "grid.color": COLOR_GRID,
    "grid.linewidth": 0.6,
    "xtick.color": COLOR_MUTED,
    "ytick.color": COLOR_MUTED,
    "xtick.major.size": 0,
    "ytick.major.size": 0,
    "xtick.labelsize": 8,
    "ytick.labelsize": 8,
    "axes.labelsize": 8.5,
    "legend.frameon": False,
    "pdf.fonttype": 42,
    "svg.fonttype": "path",
}


@dataclass(frozen=True)
class PlotContext:
    samples: pd.DataFrame
    outages: list[Outage]
    coverage: Coverage
    start: datetime
    end: datetime
    zone: ZoneInfo

    @property
    def period_text(self) -> str:
        last = self.end - timedelta(seconds=1)
        return f"{self.start.astimezone(self.zone):%d.%m.%Y} bis {last.astimezone(self.zone):%d.%m.%Y}"

    def naive(self, moment: datetime) -> datetime:
        return moment.astimezone(self.zone).replace(tzinfo=None)

    def naive_index(self, index: pd.DatetimeIndex) -> pd.DatetimeIndex:
        return index.tz_convert(self.zone).tz_localize(None)


def series_color(position: int) -> str:
    return SERIES_COLORS[position % len(SERIES_COLORS)]


def _new_figure(title: str, subtitle: str, rows: int = 1, height: float = 4.2,
                height_ratios: list[int] | None = None) -> tuple[Figure, list[Axes]]:
    figure = Figure(figsize=(7.6, height), layout="constrained", facecolor=COLOR_SURFACE)
    axes = figure.subplots(rows, 1, sharex=True, squeeze=False, height_ratios=height_ratios)[:, 0].tolist()
    figure.suptitle(title, x=0.01, ha="left", fontsize=12, fontweight="bold", color=COLOR_TEXT)
    axes[0].set_title(subtitle, loc="left", fontsize=8, color=COLOR_MUTED)
    return figure, axes


def _format_time_axis(ax: Axes, context: PlotContext) -> None:
    begin, finish = context.naive(context.start), context.naive(context.end)
    span_days = (finish - begin).total_seconds() / 86400
    if span_days >= 4:
        # Gleichmäßige Tagesticks: Der Standard-Locator beginnt am Monatsersten neu,
        # sodass sich z. B. 31.10. und 01.11. überlagern.
        step = math.ceil(span_days / 8)
        first = begin.replace(hour=0, minute=0, second=0, microsecond=0)
        if first < begin:
            first += timedelta(days=1)
        ticks = [first + timedelta(days=i) for i in range(0, math.ceil(span_days) + 1, step)]
        ax.set_xticks([tick for tick in ticks if tick <= finish])
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%d.%m."))
    else:
        locator = mdates.AutoDateLocator(minticks=4, maxticks=10)
        ax.xaxis.set_major_locator(locator)
        ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(
            locator,
            formats=["%Y", "%m.%Y", "%d.%m.", "%H:%M", "%H:%M", "%H:%M:%S"],
            zero_formats=["", "%Y", "%d.%m.", "%d.%m.", "%H:%M", "%H:%M"],
            offset_formats=[""] * 6,
        ))
    ax.set_xlim(begin, finish)


def _shade_outages(ax: Axes, context: PlotContext) -> None:
    for outage in context.outages:
        color = LEVEL_COLORS.get(outage.level, COLOR_MUTED)
        # Der feine Rand hält auch kurze Ausfälle sichtbar.
        ax.axvspan(context.naive(outage.start), context.naive(outage.end), facecolor=color, edgecolor=color,
                   alpha=0.18, linewidth=0.6, zorder=0)
    if context.coverage.cycles:
        for begin, finish in context.coverage.gaps:
            ax.axvspan(context.naive(begin), context.naive(finish), facecolor=COLOR_GAP, edgecolor="none",
                       alpha=0.55, zorder=0)


def _outage_legend(context: PlotContext) -> list[Patch]:
    present = {outage.level for outage in context.outages}
    handles = [Patch(facecolor=LEVEL_COLORS[level], alpha=0.35, label=f"Ausfall: {LEVEL_LABELS[level]}")
               for level in LEVEL_ORDER if level in present]
    if context.coverage.cycles and context.coverage.gaps:
        handles.append(Patch(facecolor=COLOR_GAP, alpha=0.8, label="Messlücke (Tool lief nicht)"))
    return handles


def _no_data(ax: Axes, text: str = "Keine Prüfungen im Zeitraum") -> None:
    ax.text(0.5, 0.5, text, transform=ax.transAxes, ha="center", va="center", color=COLOR_MUTED, fontsize=10)


def _tables(context: PlotContext) -> tuple[pd.Timedelta, list[tuple[Track, str, pd.DataFrame]]]:
    size = bucket_size(context.start, context.end)
    result = [(track, series_color(position), bucket_table(context.samples, track, size))
              for position, track in enumerate(ping_tracks(context.samples))]
    return size, result


def _size_text(size: pd.Timedelta) -> str:
    minutes = size / pd.Timedelta(minutes=1)
    return f"{minutes / 60:g} h" if minutes >= 60 else f"{minutes:g} min"


def fig_gantt(context: PlotContext) -> Figure:
    """Abbildung a: Ausfall-Zeitleiste je Ebene, dazu die Messlücken des Tools."""
    span_days = (context.end.astimezone(UTC) - context.start.astimezone(UTC)).total_seconds() / 86400
    subtitle = f"n = {len(context.outages)} Ausfälle  ·  Messzeitraum {context.period_text}"
    figure, (ax,) = _new_figure("Ausfälle nach Ebene", subtitle, height=3.4)
    minimum = span_days * 0.002
    rows: list[tuple[str, list[tuple[float, float]], str]] = []
    for level in LEVEL_ORDER:
        items = [o for o in context.outages if o.level == level]
        bars = [(mdates.date2num(context.naive(o.start)), max(o.duration_s / 86400, minimum)) for o in items]
        rows.append((f"{LEVEL_LABELS[level]} (n = {len(items)})", bars, LEVEL_COLORS[level]))
    if context.coverage.cycles:
        gaps = [(mdates.date2num(context.naive(begin)), max((finish - begin).total_seconds() / 86400, minimum))
                for begin, finish in context.coverage.gaps]
        rows.append((f"Messlücke des Tools (n = {len(gaps)})", gaps, COLOR_GAP))
    for position, (_, bars, color) in enumerate(rows):
        if bars:
            ax.broken_barh(bars, (position - 0.32, 0.64), facecolors=color, linewidth=0)
    ax.set_yticks(range(len(rows)), [row[0] for row in rows], fontsize=7.5)
    ax.set_ylim(len(rows) - 0.5, -0.5)
    ax.grid(axis="y", visible=False)
    ax.spines["left"].set_visible(False)
    _format_time_axis(ax, context)
    ax.set_xlabel("Lokale Zeit (sehr kurze Einträge mit Mindestbreite gezeichnet)")
    if not context.outages:
        _no_data(ax, "Keine Ausfälle im Zeitraum")
    return figure


def fig_ping(context: PlotContext) -> Figure:
    """Abbildung b: Ping je Strecke, Median je Intervall, für das Internet zusätzlich P95."""
    size, tables = _tables(context)
    subtitle = (f"Median je {_size_text(size)}, Band bis P95 (Internet)  ·  n = {len(context.samples)} Prüfrunden  ·  "
                f"Messzeitraum {context.period_text}")
    figure, (ax,) = _new_figure("Ping im Zeitverlauf", subtitle, height=4.2)
    _shade_outages(ax, context)
    handles: list[Line2D | Patch] = []
    peak = 0.0
    for track, color, table in tables:
        times = context.naive_index(table.index)
        if track.key == INTERNET:
            ax.fill_between(times, table["median"], table["p95"], color=color, alpha=0.18, linewidth=0, zorder=2)
            handles.append(Patch(facecolor=color, alpha=0.3, label="Internet: Median bis P95"))
        ax.plot(times, table["median"], color=color, linewidth=1.3, zorder=3)
        handles.append(Line2D([], [], color=color, linewidth=1.6, label=track.label))
        values = table["p95" if track.key == INTERNET else "median"].dropna()
        peak = max(peak, float(values.max()) if len(values) else 0.0)
    if not tables or context.samples.empty:
        _no_data(ax)
    medians = [float(table["median"].median()) for _, _, table in tables if table["median"].notna().any()]
    typical = max(medians) if medians else 1.0
    if peak > 10 * max(typical, 1.0):
        # Einzelne Spitzen würden die Achse sonst so strecken, dass der Normalbetrieb flach am Boden liegt.
        ax.set_yscale("symlog", linthresh=10)
        ax.set_ylabel("ms (logarithmisch ab 10 ms)")
    else:
        ax.set_ylabel("ms")
    ax.set_ylim(bottom=0)
    ax.grid(axis="x", visible=False)
    _format_time_axis(ax, context)
    ax.set_xlabel("Lokale Zeit")
    figure.legend(handles=handles + _outage_legend(context), loc="outside lower center", ncols=3, fontsize=7)
    return figure


def fig_loss(context: PlotContext) -> Figure:
    """Abbildung c: Anteil unbeantworteter Pings je Strecke und Intervall."""
    size, tables = _tables(context)
    subtitle = f"Anteil je {_size_text(size)}  ·  Messzeitraum {context.period_text}"
    figure, (ax,) = _new_figure("Paketverlust im Zeitverlauf", subtitle, height=3.8)
    _shade_outages(ax, context)
    handles: list[Line2D | Patch] = []
    top = 0.0
    for track, color, table in tables:
        times = context.naive_index(table.index)
        ax.step(times, table["loss_pct"], where="post", color=color, linewidth=1.1, zorder=3)
        handles.append(Line2D([], [], color=color, linewidth=1.6, label=track.label))
        values = table["loss_pct"].dropna()
        top = max(top, float(values.max()) if len(values) else 0.0)
    if not tables or context.samples.empty:
        _no_data(ax)
    ax.set_ylim(0, max(5.0, min(100.0, top * 1.1)))
    ax.set_ylabel("Unbeantwortete Pings (%)")
    ax.grid(axis="x", visible=False)
    _format_time_axis(ax, context)
    ax.set_xlabel("Lokale Zeit")
    figure.legend(handles=handles + _outage_legend(context), loc="outside lower center", ncols=3, fontsize=7)
    return figure


def internet_matrix(context: PlotContext, value: str) -> pd.DataFrame:
    days = days_between(context.start, context.end, context.zone)
    track = find_track(context.samples, INTERNET)
    if track is None:
        return pd.DataFrame(np.nan, index=range(24), columns=days)
    return hour_matrix(context.samples, track, context.zone, value, days)


def _heatmap(context: PlotContext, value: str, title: str, label: str) -> Figure:
    matrix = internet_matrix(context, value)
    subtitle = f"Internet, je Stunde  ·  Messzeitraum {context.period_text}"
    figure, (ax,) = _new_figure(title, subtitle, height=4.4)
    ax.grid(False)
    ax.set_facecolor(COLOR_NO_DATA)
    values = matrix.to_numpy(dtype=float)
    finite = values[np.isfinite(values)]
    top = float(finite.max()) if finite.size else 1.0
    mesh = ax.pcolormesh(np.arange(matrix.shape[1] + 1), np.arange(25), np.ma.masked_invalid(values),
                         cmap=SEQUENTIAL, vmin=0, vmax=max(top, 1.0), edgecolors=COLOR_SURFACE, linewidth=0.3)
    ax.set_ylim(24, 0)
    ax.set_yticks(range(0, 25, 3), [f"{hour:02d}:00" for hour in range(0, 25, 3)])
    ax.set_ylabel("Tageszeit (lokal)")
    days = list(matrix.columns)
    step = max(1, len(days) // 14)
    positions = np.arange(0, len(days), step)
    ax.set_xticks(positions + 0.5, [f"{days[i][8:10]}.{days[i][5:7]}." for i in positions], fontsize=7)
    ax.set_xlabel("Tag (grau: keine Prüfung)")
    ax.spines["bottom"].set_visible(False)
    ax.spines["left"].set_visible(False)
    bar = figure.colorbar(mesh, ax=ax, pad=0.01, fraction=0.03)
    bar.set_label(label, fontsize=8)
    bar.outline.set_visible(False)
    bar.ax.tick_params(length=0, labelsize=7)
    return figure


def fig_heatmap_loss(context: PlotContext) -> Figure:
    """Abbildung d: Paketverlust nach Tageszeit und Tag."""
    return _heatmap(context, "loss_pct", "Paketverlust nach Tageszeit", "Unbeantwortete Pings (%)")


def fig_heatmap_ping(context: PlotContext) -> Figure:
    """Abbildung e: Ping-Spitzen (P95) nach Tageszeit und Tag."""
    return _heatmap(context, "p95", "Ping-Spitzen nach Tageszeit", "Ping P95 (ms)")


FIGURES = {
    "a_ausfall_zeitleiste": fig_gantt,
    "b_ping_verlauf": fig_ping,
    "c_paketverlust": fig_loss,
    "d_heatmap_verlust": fig_heatmap_loss,
    "e_heatmap_ping": fig_heatmap_ping,
}


def render_all(context: PlotContext, directory: Path) -> dict[str, Path]:
    """Speichert alle Abbildungen in allen Formaten und gibt die PNG-Pfade zurück."""
    directory.mkdir(parents=True, exist_ok=True)
    pngs: dict[str, Path] = {}
    with matplotlib.rc_context(STYLE):
        for name, builder in FIGURES.items():
            figure = builder(context)
            figure.savefig(directory / f"{name}.pdf")
            figure.savefig(directory / f"{name}.svg")
            figure.savefig(directory / f"{name}.png", dpi=300)
            pngs[name] = directory / f"{name}.png"
    return pngs
