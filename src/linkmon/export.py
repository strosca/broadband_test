"""Export als CSV und JSON plus Rohdaten mit reproduzierbarer SHA-256-Prüfsumme."""

from __future__ import annotations

import csv
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from linkmon.quality import bucket_table, load_samples, tracks
from linkmon.store import Store
from linkmon.timeutil import parse_iso

RAW_FILE = "rohdaten.jsonl"
SUMS_FILE = "SHA256SUMS.txt"
OUTAGE_COLUMNS = ("level", "detail", "start_utc", "end_utc", "end_confirmed", "duration_s")
MINUTE_COLUMNS = ("minute_utc", "strecke", "pings", "verloren", "verlust_pct", "median_ms", "p95_ms", "max_ms")
DECIMAL_COLUMNS = {"duration_s", "verlust_pct", "median_ms", "p95_ms", "max_ms"}


@dataclass(frozen=True)
class ExportResult:
    directory: Path
    raw_sha256: str
    outages: int
    probe_cycles: int
    minutes: int


def select_outages(store: Store, start: datetime, end: datetime) -> list[dict[str, Any]]:
    result = []
    for item in store.outages(start, end):
        item["duration_s"] = None
        if item["end_utc"]:
            delta = parse_iso(item["end_utc"]) - parse_iso(item["start_utc"])
            item["duration_s"] = delta.total_seconds()
        result.append(item)
    return result


def minute_rows(store: Store, start: datetime, end: datetime) -> list[dict[str, Any]]:
    """Ping und Paketverlust je Minute und Strecke. Minuten ohne Prüfung fehlen."""
    samples = load_samples(store, start, end)
    frames = []
    for track in tracks(samples):
        table = bucket_table(samples, track, pd.Timedelta(minutes=1)).dropna(subset=["sent"])
        frames.append(pd.DataFrame({
            "minute_utc": table.index.strftime("%Y-%m-%dT%H:%M:%S+00:00"), "strecke": track.label,
            "pings": table["sent"].astype(int).to_numpy(), "verloren": table["lost"].astype(int).to_numpy(),
            "verlust_pct": table["loss_pct"].round(2).to_numpy(), "median_ms": table["median"].round(1).to_numpy(),
            "p95_ms": table["p95"].round(1).to_numpy(), "max_ms": table["max"].round(1).to_numpy(),
        }))
    if not frames:
        return []
    combined = pd.concat(frames, ignore_index=True).sort_values(["minute_utc", "strecke"], kind="stable")
    combined = combined.astype(object).where(combined.notna(), None)
    return combined.to_dict("records")


def _canonical(kind: str, row: dict[str, Any]) -> bytes:
    return (json.dumps({"kind": kind, **row}, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()


def write_raw_data(store: Store, path: Path, start: datetime, end: datetime) -> tuple[str, int]:
    """Schreibt alle Rohdaten im Zeitraum in fester Form. Gleiche Daten ergeben dieselbe Prüfsumme."""
    digest = hashlib.sha256()
    cycles = 0
    with path.open("wb") as handle:
        def emit(kind: str, row: dict[str, Any]) -> None:
            line = _canonical(kind, row)
            handle.write(line)
            digest.update(line)

        for row in store.sessions(start, end):
            emit("session", row)
        for item in select_outages(store, start, end):
            emit("outage", item)
        for row in store.events():
            if start <= parse_iso(row["ts_utc"]) < end:
                emit("event", row)
        for row in store.cycles(start, end):
            emit("probe_cycle", row)
            cycles += 1
    return digest.hexdigest(), cycles


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _csv_value(value: Any, column: str, excel_de: bool) -> Any:
    if value is None:
        return ""
    if excel_de and column in DECIMAL_COLUMNS:
        return str(value).replace(".", ",")
    return value


def _write_csv(path: Path, columns: tuple[str, ...], rows: list[dict[str, Any]], excel_de: bool) -> None:
    # Deutsches Excel erwartet Semikolon, Dezimalkomma und eine BOM für die Umlaute.
    encoding = "utf-8-sig" if excel_de else "utf-8"
    with path.open("w", newline="", encoding=encoding) as handle:
        writer = csv.writer(handle, delimiter=";" if excel_de else ",")
        writer.writerow(columns)
        for row in rows:
            writer.writerow(_csv_value(row.get(column), column, excel_de) for column in columns)


def _write_json(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text(json.dumps(rows, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")


def export_range(store: Store, start: datetime, end: datetime, directory: Path, *,
                 excel_de: bool = False) -> ExportResult:
    directory.mkdir(parents=True, exist_ok=True)
    outages = select_outages(store, start, end)
    minutes = minute_rows(store, start, end)

    raw_hash, cycles = write_raw_data(store, directory / RAW_FILE, start, end)
    _write_csv(directory / "ausfaelle.csv", OUTAGE_COLUMNS, outages, excel_de)
    _write_csv(directory / "qualitaet_minuten.csv", MINUTE_COLUMNS, minutes, excel_de)
    _write_json(directory / "ausfaelle.json", outages)

    names = [RAW_FILE, "ausfaelle.csv", "qualitaet_minuten.csv", "ausfaelle.json"]
    sums = "".join(f"{hash_file(directory / name)}  {name}\n" for name in names)
    (directory / SUMS_FILE).write_text(sums, encoding="ascii")
    return ExportResult(directory, raw_hash, len(outages), cycles, len({row["minute_utc"] for row in minutes}))
