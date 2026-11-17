"""Erzeugt den PDF-Bericht zur Verfügbarkeit samt Abbildungen und Rohdaten-Prüfsumme."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

import matplotlib
import pandas as pd
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    Image,
    KeepTogether,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from linkmon import __version__
from linkmon.classify import STABLE_ROUNDS
from linkmon.config import Config
from linkmon.export import ExportResult, export_range
from linkmon.levels import LEVEL_LABELS, LEVEL_ORDER, Level
from linkmon.linestats import (
    ClassRow,
    LineStatistics,
    PeriodStats,
    dropouts_from_samples,
    measured_interval,
    plain_duration,
)
from linkmon.monitor import monitor_gap_threshold
from linkmon.plots import MAIN_FIGURE, PlotContext, render_all
from linkmon.quality import load_samples
from linkmon.stats import Coverage, Outage, Summary, compute_coverage, compute_summary, load_outages
from linkmon.store import Store
from linkmon.timeutil import format_duration, format_local, now_utc, parse_iso

PAGE_WIDTH = A4[0] - 3.6 * cm
INK = colors.HexColor("#0b0b0b")
MUTED = colors.HexColor("#52514e")
RULE = colors.HexColor("#e1e0d9")
HEADER_FILL = colors.HexColor("#f3f2ee")
WARNING_FILL = colors.HexColor("#fdf3dc")
LINE_FILL = colors.HexColor("#fbe9e8")
LINE_INK = colors.HexColor("#b3302f")
TITLE = "Störungsprotokoll Internetanschluss"
CONNECTION_LABELS = {"lan": "LAN", "wlan": "WLAN", "unbekannt": "nicht erkannt"}
# Bauen mehrere Geräte gleichzeitig einen Bericht über das Webinterface, wechseln sich die Aufrufe ab:
# matplotlib setzt seine Stileinstellungen global.
_BUILD_LOCK = threading.Lock()

FIGURE_CAPTIONS = {
    MAIN_FIGURE: "Abbildung 1: Ausfallzeit der Internetleitung je Tag",
    "a_ausfall_zeitleiste": "Abbildung a: Ausfälle nach Ebene",
    "b_ping_verlauf": "Abbildung b: Ping im Zeitverlauf",
    "c_paketverlust": "Abbildung c: Paketverlust im Zeitverlauf",
    "d_heatmap_verlust": "Abbildung d: Paketverlust nach Tageszeit",
    "e_heatmap_ping": "Abbildung e: Ping-Spitzen nach Tageszeit",
}


def number(value: float | None, digits: int = 1, suffix: str = "") -> str:
    if value is None or pd.isna(value):
        return "–"
    return f"{value:.{digits}f}".replace(".", ",") + suffix


def _register_fonts() -> None:
    fonts = Path(matplotlib.get_data_path()) / "fonts" / "ttf"
    for name, file in (("Body", "DejaVuSans.ttf"), ("Body-Bold", "DejaVuSans-Bold.ttf"), ("Mono", "DejaVuSansMono.ttf")):
        if name not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(TTFont(name, str(fonts / file)))
    pdfmetrics.registerFontFamily("Body", normal="Body", bold="Body-Bold", italic="Body", boldItalic="Body-Bold")


def _styles() -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()["Normal"]
    body = ParagraphStyle("body", parent=base, fontName="Body", fontSize=9, leading=13, textColor=INK, spaceAfter=5)
    return {
        "body": body,
        "small": ParagraphStyle("small", parent=body, fontSize=7, leading=9.5, textColor=MUTED, spaceAfter=2),
        "cell": ParagraphStyle("cell", parent=body, fontSize=7, leading=8.8, spaceAfter=0),
        "title": ParagraphStyle("title", parent=body, fontName="Body-Bold", fontSize=18, leading=23, spaceAfter=1),
        "subtitle": ParagraphStyle("subtitle", parent=body, fontSize=10, leading=13, textColor=MUTED, spaceAfter=8),
        "figure": ParagraphStyle("figure", parent=body, fontName="Body-Bold", fontSize=13, leading=16, spaceAfter=0),
        "figure_label": ParagraphStyle("figure_label", parent=body, fontSize=7.5, leading=9.5, textColor=MUTED,
                                       spaceAfter=0),
        "appendix": ParagraphStyle("appendix", parent=body, fontName="Body-Bold", fontSize=15, leading=19,
                                   spaceAfter=4),
        "h1": ParagraphStyle("h1", parent=body, fontName="Body-Bold", fontSize=12, leading=15, spaceBefore=11,
                             spaceAfter=5, keepWithNext=1),
        "h2": ParagraphStyle("h2", parent=body, fontName="Body-Bold", fontSize=9.5, leading=12, spaceBefore=7,
                             spaceAfter=3, keepWithNext=1),
        "mono": ParagraphStyle("mono", parent=body, fontName="Mono", fontSize=7.5, leading=10),
    }


def _table(rows: list[list[Any]], widths: list[float], *, font_size: float = 7.5, align_right_from: int = 1) -> Table:
    table = Table(rows, colWidths=widths, repeatRows=1)
    table.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, 0), "Body-Bold"),
        ("FONTNAME", (0, 1), (-1, -1), "Body"),
        ("FONTSIZE", (0, 0), (-1, -1), font_size),
        ("LEADING", (0, 0), (-1, -1), font_size + 2),
        ("TEXTCOLOR", (0, 0), (-1, -1), INK),
        ("BACKGROUND", (0, 0), (-1, 0), HEADER_FILL),
        ("LINEBELOW", (0, 0), (-1, -1), 0.4, RULE),
        ("ALIGN", (align_right_from, 0), (-1, -1), "RIGHT"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 2.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5),
        ("LEFTPADDING", (0, 0), (-1, -1), 3),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3),
    ]))
    return table


def _key_values(rows: list[tuple[str, Any]], key_width: float = 5.5 * cm) -> Table:
    table = Table([[key, value] for key, value in rows], colWidths=[key_width, PAGE_WIDTH - key_width])
    table.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (0, -1), "Body-Bold"),
        ("FONTNAME", (1, 0), (1, -1), "Body"),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("LEADING", (0, 0), (-1, -1), 10.5),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LINEBELOW", (0, 0), (-1, -1), 0.4, RULE),
        ("TOPPADDING", (0, 0), (-1, -1), 2.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5),
        ("LEFTPADDING", (0, 0), (-1, -1), 3),
    ]))
    return table


def _connection_text(sessions: list[dict[str, Any]]) -> str:
    kinds = sorted({CONNECTION_LABELS.get(row.get("connection_type") or "unbekannt", "nicht erkannt")
                    for row in sessions})
    return ", ".join(kinds) if kinds else "–"


def collect_warnings(summary: Summary, sessions: list[dict[str, Any]], outages: list[Outage]) -> list[str]:
    """Benennt offen, was die Aussagekraft dieses Berichts einschränkt."""
    warnings: list[str] = []
    wlan = [row for row in sessions if row.get("connection_type") == "wlan"]
    if wlan:
        warnings.append(f"{len(wlan)} von {len(sessions)} Messsitzungen liefen über WLAN. Aussetzer des WLANs erscheinen "
                        "als Störung im Heimnetz, Funkstörungen erhöhen Ping und Paketverlust aller Strecken.")
    if any(row.get("connection_type") is None for row in sessions):
        warnings.append("Für einen Teil der Messsitzungen ist die Verbindungsart nicht erfasst (ältere Version des Tools).")
    unconfirmed = sum(1 for outage in outages if not outage.confirmed)
    if unconfirmed:
        warnings.append(f"Ausfälle mit unbestätigtem Ende oder noch laufend: {unconfirmed}. Sie enden beim letzten "
                        "Lebenszeichen des Tools, die tatsächliche Dauer kann länger sein.")
    if summary.coverage_pct is None:
        warnings.append("Für diesen Zeitraum liegen keine Prüfungen vor, die Verfügbarkeit ist daher nicht bestimmbar.")
    elif summary.coverage_pct < 95:
        warnings.append(f"Das Tool hat nur {number(summary.coverage_pct)} % des Zeitraums überwacht. Über Messlücken "
                        "trifft dieser Bericht keine Aussage.")
    return warnings


def _footer(canvas: Any, document: Any) -> None:
    canvas.saveState()
    canvas.setFont("Body", 7)
    canvas.setFillColor(MUTED)
    canvas.drawString(1.8 * cm, 1.0 * cm, f"{TITLE}, automatisch erzeugt mit linkmon {__version__}")
    canvas.drawRightString(A4[0] - 1.8 * cm, 1.0 * cm, f"Seite {document.page}")
    canvas.restoreState()


@dataclass(frozen=True)
class ReportResult:
    pdf: Path
    directory: Path
    export: ExportResult
    summary: Summary


def _distinct(rows: list[dict[str, Any]], key: str) -> str:
    values = sorted({str(row[key]) for row in rows if row.get(key)})
    return ", ".join(values) if values else "–"


def _provider(config: Config) -> str:
    return config.report.provider or "des Anbieters"


def _intro(config: Config, summary: Summary, styles: dict[str, ParagraphStyle], created: datetime,
           effective_end: datetime) -> list[Any]:
    zone = config.zone
    story: list[Any] = [
        Paragraph(TITLE, styles["title"]),
        Paragraph(f"Ausfälle der Internetverbindung über die Leitung von {escape(_provider(config))}",
                  styles["subtitle"]),
    ]
    last = effective_end - timedelta(seconds=1)
    rows: list[tuple[str, Any]] = [("Messzeitraum", f"{format_local(summary.start, zone)} bis {format_local(last, zone)} "
                                                    f"({config.general.timezone})")]
    report = config.report
    for label, value in (("Anbieter", report.provider), ("Tarif", report.tariff),
                         ("Anschlussinhaber", report.customer_name), ("Kundennummer", report.customer_number),
                         ("Anschlussadresse", report.address)):
        if value:
            rows.append((label, value))
    rows.append(("Bericht erzeugt", format_local(created, zone)))
    story.append(_key_values(rows))
    return story


def _when(moment: datetime, config: Config) -> str:
    return moment.astimezone(config.zone).strftime("%d.%m.%Y um %H:%M Uhr")


def summary_sentences(total: PeriodStats, config: Config, interval_s: float | None = None) -> list[str]:
    """Die Kernaussage in ganzen Sätzen, verständlich ohne Fachwissen.

    interval_s ist der tatsächliche Abstand der Prüfungen in den Daten, ohne Daten der aus der Konfiguration.
    """
    reach = config.reachability
    provider = escape(_provider(config))
    interval = interval_s or reach.interval_s
    minimum = reach.failure_threshold * interval
    if total.coverage_pct is None:
        return ["Für diesen Zeitraum liegen keine Messungen vor."]
    sentences: list[str] = []
    if total.count:
        times = "einmal" if total.count == 1 else f"<b>{total.count}-mal</b>"
        # Die Dauer steht nie am Satzende: „5 Min..“ sähe nach einem Fehler aus.
        sentences.append(f"Im Messzeitraum war die Internetverbindung {times} unterbrochen. Insgesamt war der "
                         f"Anschluss <b>{plain_duration(total.downtime_s)}</b> ohne Internet.")
        if total.longest is not None and total.count > 1:
            sentences.append(f"Der längste Ausfall dauerte <b>{plain_duration(total.longest.duration_s)}</b> und "
                             f"begann am {_when(total.longest.start, config)}.")
    else:
        sentences.append("Im Messzeitraum wurde <b>kein Ausfall</b> der Internetverbindung festgestellt.")
    sentences.append(f"Gemessen wurde während {number(total.coverage_pct, 1)} % des Zeitraums, alle "
                     f"{interval:g} Sekunden.")
    sentences.append(f"Gezählt sind nur Ausfälle, bei denen das eigene Heimnetz funktionierte und der Router "
                     f"antwortete, das Internet aber über die Leitung von {provider} nicht erreichbar war. "
                     f"Erkannt werden Unterbrechungen ab etwa {minimum:g} Sekunden.")
    return sentences


def _headline(total: PeriodStats, styles: dict[str, ParagraphStyle]) -> list[Any]:
    """Vier große Zahlen: wie oft, wie lange, längster Ausfall, Verfügbarkeit."""
    figures = [
        (str(total.count), "Ausfälle"),
        (plain_duration(total.downtime_s) if total.count else "0", "ohne Internet, zusammen"),
        (plain_duration(total.longest_s) if total.longest else "–", "längster Ausfall"),
        (number(total.availability_pct, 2, " %"), "Verfügbarkeit (gemessene Zeit)"),
    ]
    cells = [[Paragraph(escape(value), styles["figure"]) for value, _ in figures],
             [Paragraph(escape(label), styles["figure_label"]) for _, label in figures]]
    table = Table(cells, colWidths=[PAGE_WIDTH / 4] * 4)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), LINE_FILL if total.count else HEADER_FILL),
        ("LINEAFTER", (0, 0), (-2, -1), 2, colors.white),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, 0), 8),
        ("BOTTOMPADDING", (0, -1), (-1, -1), 8),
        ("LEFTPADDING", (0, 0), (-1, -1), 7),
    ]))
    return [Spacer(1, 4), table, Spacer(1, 4)]


def _short_stamp(moment: datetime, config: Config) -> str:
    return moment.astimezone(config.zone).strftime("%d.%m.%y %H:%M")


def _period_rows(rows: list[PeriodStats], config: Config, styles: dict[str, ParagraphStyle], *,
                 with_span: bool) -> list[list[Any]]:
    head = ["Zeitraum", "von – bis", "gemessen", "Ausfälle", "ohne Internet", "längster", "verfügbar"]
    table: list[list[Any]] = [head if with_span else [head[0], *head[2:]]]
    for row in rows:
        span = Paragraph(f"{_short_stamp(row.start, config)} – {_short_stamp(row.end - timedelta(seconds=1), config)}",
                         styles["cell"])
        cells: list[Any] = [Paragraph(f"<b>{escape(row.label)}</b>", styles["cell"])]
        if with_span:
            cells.append(span)
        cells += [
            number(row.coverage_pct, 1, " %"), str(row.count),
            plain_duration(row.downtime_s) if row.count else "–",
            plain_duration(row.longest_s) if row.longest else "–",
            number(row.availability_pct, 2, " %"),
        ]
        table.append(cells)
    return table


def _period_section(stats: LineStatistics, start: datetime, end: datetime, config: Config,
                    styles: dict[str, ParagraphStyle]) -> list[Any]:
    story: list[Any] = [Paragraph("Ausfälle nach Zeitraum", styles["h1"])]
    rolling = stats.rolling(start, end)
    story.append(_table(_period_rows(rolling, config, styles, with_span=True),
                        [w * cm for w in (3.0, 4.6, 1.7, 1.5, 2.3, 2.2, 2.1)]))
    story.append(Paragraph("„gemessen“: Anteil des Zeitraums, in dem linkmon lief. Die Verfügbarkeit bezieht sich "
                           "nur auf diese Zeit. Ein Ausfall, der über eine Zeitraumgrenze reicht, zählt in beiden "
                           "Zeiträumen.", styles["small"]))
    weeks = stats.weeks(start, end)
    if len(weeks) > 1:
        story.append(Paragraph("Nach Kalenderwochen", styles["h2"]))
        story.append(_table(_period_rows(weeks, config, styles, with_span=True),
                            [w * cm for w in (3.0, 4.6, 1.7, 1.5, 2.3, 2.2, 2.1)]))
    return story


def _class_table(title: str, rows: list[ClassRow], styles: dict[str, ParagraphStyle], width: float) -> Table:
    data: list[list[Any]] = [[title, "Anzahl", "zusammen"]]
    for row in rows:
        data.append([Paragraph(escape(row.label), styles["cell"]), str(row.count),
                     plain_duration(row.downtime_s) if row.count else "–"])
    return _table(data, [width - 3.9 * cm, 1.4 * cm, 2.5 * cm])


def _pattern_section(stats: LineStatistics, styles: dict[str, ParagraphStyle]) -> list[Any]:
    if not stats.outages:
        return []
    half = (PAGE_WIDTH - 0.6 * cm) / 2
    pair = Table([[_class_table("Dauer des Ausfalls", stats.by_duration(), styles, half),
                   _class_table("Beginn nach Tageszeit", stats.by_daytime(), styles, half)]],
                 colWidths=[half + 0.3 * cm, half + 0.3 * cm])
    pair.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                              ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))
    return [Paragraph("Wie lange und wann?", styles["h1"]), pair]


def _days_section(stats: LineStatistics, config: Config, styles: dict[str, ParagraphStyle]) -> list[Any]:
    days = stats.days_with_outages()
    if not days:
        return []
    rows: list[list[Any]] = [["Tag", "gemessen", "Ausfälle", "ohne Internet", "längster", "verfügbar"]]
    for row in days:
        rows.append([row.label, number(row.coverage_pct, 1, " %"), str(row.count), plain_duration(row.downtime_s),
                     plain_duration(row.longest_s), number(row.availability_pct, 2, " %")])
    return [
        Paragraph("Tage mit Ausfällen", styles["h1"]),
        _table(rows, [w * cm for w in (3.0, 2.2, 2.0, 3.6, 3.6, 3.0)]),
        Paragraph("Tage ohne Ausfall der Leitung sind nicht aufgeführt. Ein Ausfall über Mitternacht zählt an "
                  "beiden Tagen.", styles["small"]),
    ]


def hop_state(detail: str) -> str:
    """Ob der erste Knoten im Netz des Anbieters noch antwortete. Grenzt die Fehlerstelle ein."""
    if detail.startswith("Erster Provider-Hop erreichbar"):
        return "erreichbar"
    if detail.startswith("Erster Provider-Hop und"):
        return "nicht erreichbar"
    return "nicht geprüft"


def _line_list(stats: LineStatistics, config: Config, styles: dict[str, ParagraphStyle]) -> list[Any]:
    provider = escape(_provider(config))
    story: list[Any] = [Paragraph(f"Liste aller Ausfälle der Leitung ({len(stats.outages)})", styles["h1"])]
    if not stats.outages:
        story.append(Paragraph("Im Zeitraum wurde kein Ausfall der Leitung erkannt.", styles["body"]))
        return story
    rows: list[list[Any]] = [["Nr.", "Beginn", "Ende", "Dauer", f"Erster Knoten von {provider}"]]
    uncertain = False
    for position, outage in enumerate(stats.outages, start=1):
        end = format_local(outage.end, config.zone)
        if outage.ongoing:
            end += " *"
            uncertain = True
        elif not outage.confirmed:
            end += " *"
            uncertain = True
        rows.append([str(position), format_local(outage.start, config.zone), end, plain_duration(outage.duration_s),
                     hop_state(outage.detail)])
    story.append(_table(rows, [w * cm for w in (1.0, 3.6, 3.9, 3.4, 5.5)], font_size=7, align_right_from=99))
    notes = [f"Zeiten in lokaler Zeit ({config.general.timezone}), sekundengenau. „Erster Knoten“: der erste Router "
             f"im Netz von {provider} hinter dem eigenen Anschluss. Antwortet er nicht, liegt die Störung zwischen "
             "Hausanschluss und diesem Knoten."]
    if uncertain:
        notes.append("* Ende nicht sicher bestimmt (linkmon lief zwischendurch nicht oder der Ausfall dauert noch "
                     "an). Die tatsächliche Dauer kann länger sein.")
    story += [Paragraph(escape(note), styles["small"]) for note in notes]
    return story


def _main_figure(pngs: dict[str, Path], styles: dict[str, ParagraphStyle]) -> list[Any]:
    path = pngs.get(MAIN_FIGURE)
    if path is None:
        return []
    width, height = ImageReader(str(path)).getSize()
    scale = PAGE_WIDTH / width
    return [Spacer(1, 6), KeepTogether([Image(str(path), width=PAGE_WIDTH, height=height * scale),
                                        Paragraph(FIGURE_CAPTIONS[MAIN_FIGURE], styles["small"])])]


def _ms(value: float | None) -> str:
    return number(value, 0, " ms") if value is not None else "–"


def _warnings_block(warnings: list[str], styles: dict[str, ParagraphStyle]) -> list[Any]:
    if not warnings:
        return []
    items = [[Paragraph("<b>Einschränkungen dieses Berichts</b>", styles["cell"])]]
    items += [[Paragraph(escape(text), styles["cell"])] for text in warnings]
    table = Table(items, colWidths=[PAGE_WIDTH])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), WARNING_FILL),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    return [Spacer(1, 8), table]


def _rounds(count: int, adjective: str) -> str:
    return f"eine {adjective} Prüfung" if count == 1 else f"{count} {adjective} Prüfungen in Folge"


def _detection_text(failure: int, recovery: int) -> str:
    return (f"Verbindungsausfall (Leitung, Router, Lokal): ab {_rounds(failure, 'fehlerhafte')}. Störung einzelner "
            f"Dienste (DNS, HTTPS): ab {_rounds(max(failure, STABLE_ROUNDS), 'fehlerhafte')}. Beendet durch "
            f"{_rounds(recovery, 'fehlerfreie')}. Start ist die erste fehlerhafte, Ende die erste fehlerfreie "
            "Prüfung. Kurze Aussetzer aus Messungen mit älteren Einstellungen sind aus den gespeicherten "
            "Prüfrunden nachgetragen.")


def _methodology(config: Config, sessions: list[dict[str, Any]], styles: dict[str, ParagraphStyle],
                 interval_s: float | None = None) -> list[Any]:
    reach = config.reachability
    interval = interval_s or reach.interval_s
    rows: list[tuple[str, Any]] = [
        ("Messgerät", _distinct(sessions, "hostname")),
        ("Betriebssystem", _distinct(sessions, "os")),
        ("Netzwerkinterface", _distinct(sessions, "interface")),
        ("Verbindungsart", _connection_text(sessions)),
        ("Messsoftware", f"linkmon {_distinct(sessions, 'linkmon_version')}, Python {_distinct(sessions, 'python_version')}"),
        ("Prüfung", f"alle {interval:g} s parallel: je ein Ping an Router im Heimnetz, ersten Provider-Hop und "
                    f"{', '.join(reach.external_targets)}, dazu eine DNS-Abfrage (System, eigener DNS-Server im "
                    f"Heimnetz und zum Vergleich {', '.join(reach.dns_reference_servers) or 'kein öffentlicher Server'})"
                    " und ein HTTPS-Abruf"),
        ("Ausfall-Erkennung", _detection_text(reach.failure_threshold, reach.recovery_threshold)),
    ]
    rows = [(key, Paragraph(escape(value), styles["cell"])) for key, value in rows]
    method_text = (
        "Die Ebene eines Ausfalls ergibt sich aus der innersten gestörten Prüfung. <b>Leitung</b>: Router erreichbar, "
        "aber alle externen Ziele nicht. <b>Router/LAN</b>: ein Router im Heimnetz antwortet nicht. <b>Lokal</b>: das "
        "Messgerät hat keine aktive Verbindung. <b>DNS Heimnetz</b>: der eigene DNS-Server im Heimnetz (Pi-hole) "
        "antwortet nicht, ein öffentlicher DNS-Server direkt gefragt aber schon. Das ist kein Fehler des Anbieters. "
        "<b>Nur DNS, nur HTTPS</b>: der jeweilige Dienst scheitert, während die "
        "Grundverbindung steht. Die <b>Verfügbarkeit</b> berücksichtigt nur Leitungsausfälle und bezieht sie auf die "
        "Zeit, in der das Tool tatsächlich geprüft hat. Messlücken zählen weder als verfügbar noch als Ausfall."
    )
    quality_text = (
        "<b>Ping</b> ist die Antwortzeit eines einzelnen ICMP-Pings je Prüfrunde. Für die Strecke Internet werden die "
        "externen Ziele zusammengefasst. <b>P95</b> ist der Wert, den 95 % der Antworten nicht überschreiten, er zeigt "
        "Spitzen. <b>Paketverlust</b> ist der Anteil unbeantworteter Pings. <b>Jitter</b> ist die mittlere Abweichung "
        "aufeinanderfolgender Antwortzeiten. Steigen Ping und Verlust schon am Provider-Hop, liegt die Ursache "
        "hinter dem eigenen Router."
    )
    limits_text = (
        "Ein Ausfall der Ebene Leitung unterscheidet nicht zwischen der WAN-Seite des Kabelrouters und dem Netz des "
        "Anbieters. Der Ausfall eines vom Anbieter gestellten Routers erscheint unter Router/LAN. Router beantworten "
        "Pings an sich selbst oft nachrangig: Einzelne Spitzen am Provider-Hop ohne Spitzen ins Internet sind deshalb "
        "kein Beleg für eine Störung. Die Geschwindigkeit des Anschlusses misst dieses Tool nicht."
    )
    return [
        Paragraph("A1 Messmethode", styles["h1"]),
        _key_values(rows),
        Spacer(1, 6),
        Paragraph(method_text, styles["body"]),
        Paragraph(quality_text, styles["body"]),
        Paragraph("<b>Grenzen der Messung.</b> " + limits_text, styles["body"]),
    ]


def _metrics(summary: Summary, styles: dict[str, ParagraphStyle]) -> list[Any]:
    story: list[Any] = [Paragraph("A2 Kennzahlen aller Prüfungen", styles["h1"]),
                        Paragraph("Verbindungsqualität je Strecke", styles["h2"])]
    if summary.tracks:
        rows: list[list[Any]] = [["Strecke", "Prüfungen", "Verlust", "Median", "P95", "Maximum", "Jitter"]]
        for item in summary.tracks:
            rows.append([Paragraph(escape(item.track.label), styles["cell"]), str(item.sent),
                         number(item.loss_pct, 2, " %"), _ms(item.median_ms), _ms(item.p95_ms), _ms(item.max_ms),
                         number(item.jitter_ms, 1, " ms") if item.track.is_ping else "–"])
        story.append(_table(rows, [5.6 * cm, 1.9 * cm, 1.8 * cm, 1.8 * cm, 1.8 * cm, 2.0 * cm, 1.8 * cm]))
        story.append(Paragraph("DNS-Antwort und HTTPS-Abruf messen die Dauer der ganzen Anfrage, nicht einen Ping. "
                               "Ihr Verlust ist der Anteil gescheiterter Anfragen.", styles["small"]))
    else:
        story.append(Paragraph("Keine Prüfungen im Zeitraum.", styles["body"]))

    story.append(Paragraph("Ereignisse nach Ebene", styles["h2"]))
    outage_rows = [["Ebene", "Anzahl", "Gesamtdauer"]]
    for level in LEVEL_ORDER:
        outage_rows.append([LEVEL_LABELS[level], str(summary.outage_count.get(level, 0)),
                            format_duration(summary.outage_duration_s.get(level, 0.0))])
    story.append(_table(outage_rows, [8 * cm, 3 * cm, 6 * cm]))

    story.append(Paragraph("Aufschlüsselung nach Tagen", styles["h2"]))
    if summary.days:
        day_rows = [["Tag", "überwacht", "Ausfälle", "Leitung", "Dauer Leitung", "Verfügb.", "Ping P50", "Ping P95",
                     "Verlust"]]
        for day in summary.days:
            day_rows.append([
                f"{day.day[8:10]}.{day.day[5:7]}.{day.day[:4]}", format_duration(day.monitored_s), str(day.outages),
                str(day.line_outages), format_duration(day.line_downtime_s), number(day.availability_pct, 2, " %"),
                _ms(day.ping_p50), _ms(day.ping_p95), number(day.loss_pct, 2, " %"),
            ])
        widths = [2.0, 1.9, 1.5, 1.4, 2.1, 1.9, 1.7, 1.7, 1.6]
        story.append(_table(day_rows, [w * cm for w in widths], font_size=6.5))
        story.append(Paragraph("Leitung, Dauer Leitung und Verfügb. beziehen sich auf Leitungsausfälle. Ping und "
                               "Verlust gelten für die Strecke Internet.", styles["small"]))
    else:
        story.append(Paragraph("Keine Daten im Zeitraum.", styles["body"]))
    return story


def _outage_table(outages: list[Outage], config: Config, styles: dict[str, ParagraphStyle]) -> list[Any]:
    """Alle Ereignisse außer Leitungsausfällen: Heimnetz, DNS, HTTPS. Sie stehen bewusst nur im Anhang."""
    outages = [outage for outage in outages if outage.level != Level.LINE]
    story: list[Any] = [
        Paragraph("A3 Weitere Ereignisse (nicht in den Zahlen auf den ersten Seiten)", styles["h1"]),
        Paragraph("Störungen im eigenen Heimnetz (Router/LAN, Lokal, DNS Heimnetz) liegen nicht im "
                  "Verantwortungsbereich des Anbieters. Nur DNS und Nur HTTPS sind Störungen einzelner Dienste bei "
                  "stehender Verbindung. Sie sind hier der Vollständigkeit halber aufgeführt.", styles["body"]),
    ]
    if not outages:
        story.append(Paragraph("Im Zeitraum gab es keine weiteren Ereignisse.", styles["body"]))
        return story
    zone = config.zone
    rows: list[list[Any]] = [["Start", "Ende", "Dauer", "Ebene", "Befund"]]
    for outage in outages:
        marker = ""
        if outage.ongoing:
            marker = " (läuft noch)"
        elif not outage.confirmed:
            marker = " (unbestätigt)"
        rows.append([
            format_local(outage.start, zone), Paragraph(format_local(outage.end, zone) + marker, styles["cell"]),
            format_duration(outage.duration_s), LEVEL_LABELS.get(outage.level, outage.level),
            Paragraph(escape(outage.detail), styles["cell"]),
        ])
    story.append(_table(rows, [2.9 * cm, 3.5 * cm, 1.6 * cm, 3.0 * cm, 6.4 * cm], font_size=6.5, align_right_from=99))
    return story


def _figures(pngs: dict[str, Path], styles: dict[str, ParagraphStyle]) -> list[Any]:
    story: list[Any] = [Paragraph("A4 Abbildungen", styles["h1"])]
    for name, path in pngs.items():
        if name == MAIN_FIGURE:
            continue
        width, height = ImageReader(str(path)).getSize()
        scale = PAGE_WIDTH / width
        story.append(KeepTogether([
            Image(str(path), width=PAGE_WIDTH, height=height * scale),
            Paragraph(FIGURE_CAPTIONS[name], styles["small"]),
            Spacer(1, 8),
        ]))
    return story


def _legal_note(styles: dict[str, ParagraphStyle]) -> list[Any]:
    text = (
        "Dieser Bericht ist eine eigene, automatische Aufzeichnung und kein amtliches Messprotokoll. Er dokumentiert "
        "Zeitpunkt, Dauer, Häufigkeit und Einordnung von Störungen über einen längeren Zeitraum. Ansprüche bei "
        "andauernden Störungen (§ 58 TKG) setzen eine Störungsmeldung beim Anbieter voraus. Für eine Minderung wegen "
        "zu geringer Geschwindigkeit (§ 57 TKG) ist die Messung mit der Desktop-App der Bundesnetzagentur "
        "(breitbandmessung.de) maßgeblich."
    )
    return [Paragraph("Rechtlicher Hinweis", styles["h1"]), Paragraph(text, styles["body"])]


def _appendix(export: ExportResult, styles: dict[str, ParagraphStyle]) -> list[Any]:
    text = (
        f"Grundlage dieses Berichts sind {export.probe_cycles} Prüfrunden und {export.outages} Ereignisse in der Datei "
        "<b>rohdaten.jsonl</b>. Sie liegt auf dem Messgerät im Ordner des Berichts und kann auf Wunsch nachgereicht "
        "werden. Die Prüfsumme zeigt, dass diese Datei seit der Erstellung des Berichts unverändert ist. Sie belegt "
        "nicht, wer die Daten erzeugt hat."
    )
    return [
        Paragraph("A5 Prüfsumme der Rohdaten", styles["h1"]),
        Paragraph(text, styles["body"]),
        Paragraph("SHA-256 von rohdaten.jsonl:", styles["h2"]),
        Paragraph(export.raw_sha256, styles["mono"]),
        Paragraph("Nachprüfen unter Windows: certutil -hashfile rohdaten.jsonl SHA256<br/>"
                  "Unter Linux: sha256sum rohdaten.jsonl", styles["small"]),
    ]


def _appendix_intro(styles: dict[str, ParagraphStyle]) -> list[Any]:
    text = ("Dieser Teil richtet sich an Netzwerktechniker. Er beschreibt die Messmethode, die Qualität jeder "
            "geprüften Strecke und alle übrigen Ereignisse. Für die Aussage über die Ausfälle der Leitung reichen "
            "die vorderen Seiten.")
    return [PageBreak(), Paragraph("Technischer Anhang", styles["appendix"]), Paragraph(text, styles["body"])]


def build_report(config: Config, store: Store, start: datetime, end: datetime, directory: Path) -> ReportResult:
    with _BUILD_LOCK:
        return _build(config, store, start, end, directory)


def _build(config: Config, store: Store, start: datetime, end: datetime, directory: Path) -> ReportResult:
    _register_fonts()
    styles = _styles()
    zone = config.zone
    directory.mkdir(parents=True, exist_ok=True)
    created = now_utc()
    # Liegt das Ende in der Zukunft (z. B. „bis heute“), enden die Zeiträume jetzt: Sonst zählten die
    # noch nicht vergangenen Stunden als Messlücke und „letzte 24 Stunden“ wären die falschen.
    effective_end = max(min(end, created), start + timedelta(seconds=1))

    threshold = monitor_gap_threshold(config.reachability.interval_s)
    samples = load_samples(store, start, effective_end)
    outages = load_outages(store, start, effective_end)
    outages = sorted(outages + dropouts_from_samples(samples, outages, threshold), key=lambda item: item.start)
    stamps = [parse_iso(stamp) for stamp in store.cycle_times(start, effective_end)]
    coverage: Coverage = compute_coverage(stamps, start, effective_end, threshold, zone)
    summary = compute_summary(samples, outages, coverage, start, effective_end, zone)
    sessions = store.sessions(start, effective_end)
    export = export_range(store, start, effective_end, directory / "rohdaten")
    pngs = render_all(PlotContext(samples, outages, coverage, start, effective_end, zone), directory / "abbildungen")
    line = LineStatistics(outages, stamps, threshold, zone)
    total = line.period("Gesamter Zeitraum", start, effective_end)

    story: list[Any] = []
    story += _intro(config, summary, styles, created, effective_end)
    interval = measured_interval(stamps)
    story += [Spacer(1, 8), Paragraph(" ".join(summary_sentences(total, config, interval)), styles["body"])]
    story += _headline(total, styles)
    story += _warnings_block(collect_warnings(summary, sessions, outages), styles)
    story += _period_section(line, start, effective_end, config, styles)
    story += _main_figure(pngs, styles)
    story += _pattern_section(line, styles)
    story += _days_section(line, config, styles)
    story += _line_list(line, config, styles)
    story += _legal_note(styles)
    story += _appendix_intro(styles)
    story += _methodology(config, sessions, styles, interval)
    story += _metrics(summary, styles)
    story += _outage_table(outages, config, styles)
    story += _figures(pngs, styles)
    story += _appendix(export, styles)

    pdf_path = directory / "bericht.pdf"
    document = SimpleDocTemplate(
        str(pdf_path), pagesize=A4, leftMargin=1.8 * cm, rightMargin=1.8 * cm, topMargin=1.7 * cm,
        bottomMargin=1.7 * cm, title=TITLE, author=config.report.customer_name or "linkmon",
        subject=f"Messzeitraum {start.isoformat()} bis {effective_end.isoformat()}",
    )
    document.build(story, onFirstPage=_footer, onLaterPages=_footer)
    return ReportResult(pdf_path, directory, export, summary)
