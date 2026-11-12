"""Kompaktes CSV-Format der Prüfrunden: eine Datei je Monat, eine Spalte je Ziel.

Kopfzeile: sitzung, zeit_utc, ebene, danach je geprüftem Ziel eine Spalte, etwa
`lan:192.168.68.1` oder `ext:1.1.1.1`. Eine Zelle enthält:

- eine Zahl: Prüfung erfolgreich, Antwortzeit in ms
- `ok`: erfolgreich, ohne gemessene Antwortzeit
- `x` oder `x:Grund`: fehlgeschlagen
- nichts: in dieser Runde nicht geprüft

Kommt ein neues Ziel hinzu, etwa nach einem Netzwechsel, wird die Kopfzeile um eine Spalte
verlängert. Ältere Zeilen haben dann einfach weniger Felder, Excel und pandas füllen sie leer auf.
Zeiten stehen in UTC als `2026-11-08 19:00:18`, so erkennt Excel sie direkt als Datum.

Bis Version 2.0 gab es eine Datei je Tag mit dem Ergebnis als JSON in der Spalte `results`.
Solche Dateien werden weiter gelesen und beim Start des Dienstes umgewandelt.
"""

from __future__ import annotations

import json
from typing import Any

BASE_COLUMNS = ("sitzung", "zeit_utc", "ebene")
LEGACY_COLUMNS = ("session_id", "ts_utc", "level", "results")
OK_TEXT = "ok"
FAIL_TEXT = "x"
# Details, die nichts über "fehlgeschlagen" hinaus sagen, kosten nur Platz.
PLAIN_FAILURES = {"", "keine Antwort"}


def is_legacy(header: list[str]) -> bool:
    return "results" in header


def file_time(ts_utc: str) -> str:
    """`2026-11-08T19:00:18+00:00` wird zu `2026-11-08 19:00:18`."""
    return f"{ts_utc[:10]} {ts_utc[11:19]}"


def iso_time(text: str) -> str:
    return f"{text[:10]}T{text[11:19]}+00:00"


def format_ms(value: float) -> str:
    """Ab 10 ms ganze Millisekunden, darunter eine Nachkommastelle. Feiner misst ein Ping nicht."""
    if value >= 10:
        return str(round(value))
    return f"{value:.1f}".rstrip("0").rstrip(".") or "0"


def encode_cell(item: list[Any]) -> str:
    ok, latency, detail = (list(item) + [None, None, ""])[:3]
    if ok:
        return format_ms(float(latency)) if latency is not None else OK_TEXT
    detail = str(detail or "")
    return FAIL_TEXT if detail in PLAIN_FAILURES else f"{FAIL_TEXT}:{detail}"


def decode_cell(text: str) -> list[Any] | None:
    if text == "":
        return None
    if text == OK_TEXT:
        return [1, None, ""]
    if text.startswith(FAIL_TEXT):
        return [0, None, text[2:] if len(text) > 2 else "keine Antwort"]
    try:
        return [1, float(text), ""]
    except ValueError:
        return None


def encode_row(session_id: int | None, ts_utc: str, level: str | None, results: dict[str, list[Any]],
               keys: list[str]) -> list[str]:
    values = ["" if session_id is None else str(session_id), file_time(ts_utc), level or ""]
    cells = [encode_cell(results[key]) if key in results else "" for key in keys]
    while cells and cells[-1] == "":
        cells.pop()  # Leere Felder am Ende weglassen spart bei alten Zielen Platz.
    return values + cells


def decode_row(values: list[str], header: list[str]) -> dict[str, Any] | None:
    """Liefert eine Prüfrunde wie bisher: session_id, ts_utc, level und results als JSON."""
    if is_legacy(header):
        if len(values) != len(header):
            return None
        raw = dict(zip(header, values, strict=True))
        session = raw.get("session_id", "")
        return {"session_id": int(float(session)) if session else None, "ts_utc": raw["ts_utc"],
                "level": raw.get("level") or None, "results": raw["results"]}
    if len(values) < len(BASE_COLUMNS) or len(values) > len(header) or len(values[1]) < 19:
        return None
    results: dict[str, list[Any]] = {}
    for key, text in zip(header[3:], values[3:], strict=False):
        item = decode_cell(text)
        if item is not None:
            results[key] = item
    return {"session_id": int(values[0]) if values[0] else None, "ts_utc": iso_time(values[1]),
            "level": values[2] or None, "results": json.dumps(results, separators=(",", ":"))}


def row_time(values: list[str], header: list[str]) -> str | None:
    """Nur der Zeitstempel, ohne die Ergebnisse zu entpacken."""
    if is_legacy(header):
        return values[1] if len(values) == len(header) else None
    return iso_time(values[1]) if len(values) >= 3 and len(values[1]) >= 19 else None
