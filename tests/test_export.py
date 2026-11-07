from __future__ import annotations

import csv
import hashlib
import json
from datetime import datetime
from pathlib import Path

from linkmon.export import RAW_FILE, SUMS_FILE, export_range, hash_file
from linkmon.store import CYCLES_DIR, Store


def test_export_writes_checksums_that_verify(seeded: tuple[Store, datetime, datetime], tmp_path: Path) -> None:
    store, start, end = seeded
    result = export_range(store, start, end, tmp_path / "out")
    raw = tmp_path / "out" / RAW_FILE
    assert result.raw_sha256 == hashlib.sha256(raw.read_bytes()).hexdigest()
    for line in (tmp_path / "out" / SUMS_FILE).read_text(encoding="ascii").splitlines():
        digest, name = line.split("  ")
        assert hash_file(tmp_path / "out" / name) == digest
    assert result.outages == 3
    assert result.probe_cycles == 2 * 2880 - 240  # zwei Tage alle 30 s, ohne die Lücke von zwei Stunden
    assert result.minutes == 2 * 1440 - 120


def test_same_data_gives_same_checksum(seeded: tuple[Store, datetime, datetime], tmp_path: Path) -> None:
    store, start, end = seeded
    first = export_range(store, start, end, tmp_path / "a")
    second = export_range(store, start, end, tmp_path / "b")
    assert first.raw_sha256 == second.raw_sha256


def test_changed_data_changes_checksum(seeded: tuple[Store, datetime, datetime], tmp_path: Path) -> None:
    store, start, end = seeded
    first = export_range(store, start, end, tmp_path / "a")
    path = sorted((store.directory / CYCLES_DIR).glob("*.csv"))[0]
    text = path.read_text(encoding="utf-8-sig")
    path.write_text(text.replace("[1,20.0,", "[1,21.0,", 1), encoding="utf-8-sig")  # Jemand ändert einen Wert.
    assert export_range(store, start, end, tmp_path / "b").raw_sha256 != first.raw_sha256


def test_raw_file_contains_all_kinds(seeded: tuple[Store, datetime, datetime], tmp_path: Path) -> None:
    store, start, end = seeded
    export_range(store, start, end, tmp_path)
    with (tmp_path / RAW_FILE).open(encoding="utf-8") as handle:
        kinds = {json.loads(line)["kind"] for line in handle}
    assert kinds == {"session", "outage", "probe_cycle"}


def test_minute_table_has_one_row_per_track_and_minute(seeded: tuple[Store, datetime, datetime], tmp_path: Path) -> None:
    store, start, end = seeded
    export_range(store, start, end, tmp_path)
    with (tmp_path / "qualitaet_minuten.csv").open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    first_minute = [row for row in rows if row["minute_utc"] == rows[0]["minute_utc"]]
    assert {row["strecke"] for row in first_minute} == {
        "Internet (1.1.1.1, 8.8.8.8)", "Provider-Hop 92.79.214.24", "Heimnetz 192.168.0.1", "DNS-Antwort",
        "HTTPS-Abruf"}
    internet = next(row for row in first_minute if row["strecke"].startswith("Internet"))
    assert internet["pings"] == "4" and internet["verloren"] == "1"  # Die erste Runde verliert 1.1.1.1.


def test_excel_export_uses_german_format(seeded: tuple[Store, datetime, datetime], tmp_path: Path) -> None:
    store, start, end = seeded
    export_range(store, start, end, tmp_path, excel_de=True)
    content = (tmp_path / "qualitaet_minuten.csv").read_bytes()
    assert content.startswith(b"\xef\xbb\xbf")
    with (tmp_path / "qualitaet_minuten.csv").open(newline="", encoding="utf-8-sig") as handle:
        row = next(csv.DictReader(handle, delimiter=";"))
    assert "," in row["median_ms"]
