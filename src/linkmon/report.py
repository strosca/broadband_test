"""Erzeugt den PDF-Bericht zur Verfügbarkeit samt Abbildungen und Rohdaten-Prüfsumme."""

from __future__ import annotations

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
from reportlab.platypus import Image, KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from linkmon import __version__
from linkmon.config import Config
from linkmon.export import ExportResult, export_range
from linkmon.levels import LEVEL_LABELS, LEVEL_ORDER
from linkmon.monitor import monitor_gap_threshold
from linkmon.plots import PlotContext, render_all
from linkmon.quality import INTERNET, load_samples
from linkmon.stats import Coverage, Outage, Summary, compute_summary, load_coverage, load_outages
from linkmon.store import Store
from linkmon.timeutil import format_duration, format_local, now_utc

PAGE_WIDTH = A4[0] - 3.6 * cm
INK = colors.HexColor("#0b0b0b")
MUTED = colors.HexColor("#52514e")
RULE = colors.HexColor("#e1e0d9")
HEADER_FILL = colors.HexColor("#f3f2ee")
WARNING_FILL = colors.HexColor("#fdf3dc")
TITLE = "Bericht zur Verfügbarkeit des Internetanschlusses"
CONNECTION_LABELS = {"lan": "LAN", "wlan": "WLAN", "unbekannt": "nicht erkannt"}

FIGURE_CAPTIONS = {
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
        "title": ParagraphStyle("title", parent=body, fontName="Body-Bold", fontSize=18, leading=23, spaceAfter=3),
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
    canvas.drawString(1.8 * cm, 1.0 * cm, f"Verfügbarkeitsbericht, erzeugt mit linkmon {__version__}")
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


def _intro(config: Config, summary: Summary, styles: dict[str, ParagraphStyle], created: datetime) -> list[Any]:
    zone = config.zone
    story: list[Any] = [Paragraph(TITLE, styles["title"])]
    last = summary.end - timedelta(seconds=1)
    rows: list[tuple[str, Any]] = [("Messzeitraum", f"{format_local(summary.start, zone)} bis {format_local(last, zone)} "
                                                    f"({config.general.timezone})")]
    report = config.report
    for label, value in (("Tarif", report.tariff), ("Anschlussinhaber", report.customer_name),
                         ("Kundennummer", report.customer_number), ("Anschlussadresse", report.address)):
        if value:
            rows.append((label, value))
    rows.append(("Bericht erzeugt", format_local(created, zone)))
    story.append(_key_values(rows))
    return story


def _ms(value: float | None) -> str:
    return number(value, 0, " ms") if value is not None else "–"


def _key_figures(summary: Summary, styles: dict[str, ParagraphStyle]) -> list[Any]:
    internet = summary.track(INTERNET)
    rows: list[tuple[str, str]] = [
        ("Verfügbarkeit der Leitung", number(summary.availability_pct, 3, " %") + "  (bezogen auf die überwachte Zeit)"),
        ("Überwachte Zeit", f"{format_duration(summary.monitored_s)}  "
                            f"({number(summary.coverage_pct, 1, ' %')} des Zeitraums)"),
        ("Leitungsausfälle", f"{summary.outage_count.get('line', 0)}, zusammen {format_duration(summary.line_downtime_s)}"),
        ("Ausfälle aller Ebenen", str(sum(summary.outage_count.values()))),
    ]
    if internet:
        rows += [
            ("Ping ins Internet", f"Median {_ms(internet.median_ms)}, P95 {_ms(internet.p95_ms)}, "
                                  f"Maximum {_ms(internet.max_ms)}"),
            ("Paketverlust ins Internet", f"{number(internet.loss_pct, 2, ' %')}  ({internet.lost} von "
                                          f"{internet.sent} Pings unbeantwortet)"),
            ("Jitter ins Internet", number(internet.jitter_ms, 1, " ms")),
        ]
    return [Paragraph("Kennzahlen im Überblick", styles["h1"]), _key_values(rows)]


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


def _methodology(config: Config, sessions: list[dict[str, Any]], styles: dict[str, ParagraphStyle]) -> list[Any]:
    reach = config.reachability
    rows: list[tuple[str, Any]] = [
        ("Messgerät", _distinct(sessions, "hostname")),
        ("Betriebssystem", _distinct(sessions, "os")),
        ("Netzwerkinterface", _distinct(sessions, "interface")),
        ("Verbindungsart", _connection_text(sessions)),
        ("Messsoftware", f"linkmon {_distinct(sessions, 'linkmon_version')}, Python {_distinct(sessions, 'python_version')}"),
        ("Prüfung", f"alle {reach.interval_s:g} s parallel: je ein Ping an Router im Heimnetz, ersten Provider-Hop und "
                    f"{', '.join(reach.external_targets)}, dazu eine DNS-Abfrage und ein HTTPS-Abruf"),
        ("Ausfall-Erkennung", f"{reach.failure_threshold} fehlerhafte Prüfungen in Folge eröffnen, "
                              f"{reach.recovery_threshold} fehlerfreie beenden einen Ausfall. Start ist die erste "
                              "fehlerhafte, Ende die erste fehlerfreie Prüfung."),
    ]
    rows = [(key, Paragraph(escape(value), styles["cell"])) for key, value in rows]
    method_text = (
        "Die Ebene eines Ausfalls ergibt sich aus der innersten gestörten Prüfung. <b>Leitung</b>: Router erreichbar, "
        "aber alle externen Ziele nicht. <b>Router/LAN</b>: ein Router im Heimnetz antwortet nicht. <b>Lokal</b>: das "
        "Messgerät hat keine aktive Verbindung. <b>Nur DNS, nur HTTPS</b>: der jeweilige Dienst scheitert, während die "
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
        Paragraph("Methodik", styles["h1"]),
        _key_values(rows),
        Spacer(1, 6),
        Paragraph(method_text, styles["body"]),
        Paragraph(quality_text, styles["body"]),
        Paragraph("<b>Grenzen der Messung.</b> " + limits_text, styles["body"]),
    ]


def _metrics(summary: Summary, styles: dict[str, ParagraphStyle]) -> list[Any]:
    story: list[Any] = [Paragraph("Kennzahlen", styles["h1"]), Paragraph("Verbindungsqualität je Strecke", styles["h2"])]
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

    story.append(Paragraph("Ausfälle nach Ebene", styles["h2"]))
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
    story: list[Any] = [Paragraph("Alle Ausfälle", styles["h1"])]
    if not outages:
        story.append(Paragraph("Im Zeitraum wurden keine Ausfälle erkannt.", styles["body"]))
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
    story: list[Any] = [Paragraph("Abbildungen", styles["h1"])]
    for name, path in pngs.items():
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
        f"Grundlage dieses Berichts sind {export.probe_cycles} Prüfrunden und {export.outages} Ausfälle in der Datei "
        "<b>rohdaten.jsonl</b>. Die Prüfsumme zeigt, dass diese Datei seit der Erstellung des Berichts unverändert ist. "
        "Sie belegt nicht, wer die Daten erzeugt hat."
    )
    return [
        Paragraph("Anhang: Prüfsumme der Rohdaten", styles["h1"]),
        Paragraph(text, styles["body"]),
        Paragraph("SHA-256 von rohdaten.jsonl:", styles["h2"]),
        Paragraph(export.raw_sha256, styles["mono"]),
        Paragraph("Nachprüfen unter Windows: certutil -hashfile rohdaten.jsonl SHA256<br/>"
                  "Unter Linux: sha256sum rohdaten.jsonl", styles["small"]),
    ]


def build_report(config: Config, store: Store, start: datetime, end: datetime, directory: Path) -> ReportResult:
    _register_fonts()
    styles = _styles()
    zone = config.zone
    directory.mkdir(parents=True, exist_ok=True)
    created = now_utc()

    samples = load_samples(store, start, end)
    outages = load_outages(store, start, end)
    coverage: Coverage = load_coverage(store, start, end, monitor_gap_threshold(config.reachability.interval_s), zone)
    summary = compute_summary(samples, outages, coverage, start, end, zone)
    sessions = store.sessions(start, end)
    export = export_range(store, start, end, directory / "rohdaten")
    pngs = render_all(PlotContext(samples, outages, coverage, start, end, zone), directory / "abbildungen")

    story: list[Any] = []
    story += _intro(config, summary, styles, created)
    story += _key_figures(summary, styles)
    story += _warnings_block(collect_warnings(summary, sessions, outages), styles)
    story += _legal_note(styles)
    story += _methodology(config, sessions, styles)
    story += _metrics(summary, styles)
    story += _outage_table(outages, config, styles)
    story += _figures(pngs, styles)
    story += _appendix(export, styles)

    pdf_path = directory / "bericht.pdf"
    document = SimpleDocTemplate(
        str(pdf_path), pagesize=A4, leftMargin=1.8 * cm, rightMargin=1.8 * cm, topMargin=1.7 * cm,
        bottomMargin=1.7 * cm, title=TITLE, author=config.report.customer_name or "linkmon",
        subject=f"Messzeitraum {start.isoformat()} bis {end.isoformat()}",
    )
    document.build(story, onFirstPage=_footer, onLaterPages=_footer)
    return ReportResult(pdf_path, directory, export, summary)
