"""Speicher in CSV-Dateien. Jede Messung wird sofort angehängt und auf die Platte geschrieben.

Aufbau im Ordner data/messdaten:

- pruefungen/pruefungen_JJJJ-MM.csv: eine Zeile je Prüfrunde, eine Spalte je Ziel mit der Antwortzeit,
  eine Datei je Monat (UTC). Das Format beschreibt linkmon.cyclefile.
- ausfaelle.csv, sitzungen.csv: werden bei jeder Änderung vollständig und atomar neu geschrieben
- ereignisse.csv: Warnungen, Messlücken, Routen- und Verbindungswechsel
- zustand.csv: interne Merkwerte wie die zuletzt ermittelte Route und die aktuelle Verbindung

Ist eine Datei gesperrt, etwa weil sie in Excel offen ist, bleiben die Zeilen im Speicher und
werden beim nächsten Schreibvorgang nachgetragen. Die Messung läuft dabei weiter.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import os
import shutil
import threading
import time
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from linkmon import cyclefile
from linkmon.timeutil import iso_utc

log = logging.getLogger("linkmon.store")

SESSION_COLUMNS = ("id", "started_utc", "ended_utc", "end_reason", "hostname", "os", "interface",
                   "connection_type", "link_mbps", "python_version", "linkmon_version", "config_sha256")
OUTAGE_COLUMNS = ("id", "session_id", "level", "detail", "start_utc", "end_utc", "end_confirmed")
EVENT_COLUMNS = ("id", "session_id", "ts_utc", "kind", "message")
META_COLUMNS = ("key", "value")

INT_COLUMNS = {"id", "session_id", "end_confirmed"}
FLOAT_COLUMNS = {"link_mbps"}

OUTAGES_FILE = "ausfaelle.csv"
SESSIONS_FILE = "sitzungen.csv"
EVENTS_FILE = "ereignisse.csv"
META_FILE = "zustand.csv"
CYCLES_DIR = "pruefungen"
CYCLE_PREFIX = "pruefungen_"
TAIL_BYTES = 64 * 1024
# Hierhin wandern die alten Tagesdateien, nachdem sie in Monatsdateien umgewandelt wurden.
LEGACY_ARCHIVE = "tagesdateien_vor_umstellung"


def _encode(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return str(int(value))
    if isinstance(value, float):
        return repr(value)
    return str(value)


def _decode(column: str, text: str) -> Any:
    if text == "":
        return None
    if column in INT_COLUMNS:
        try:
            return int(text)
        except ValueError:
            return int(float(text))
    if column in FLOAT_COLUMNS:
        return float(text)
    return text


def _line(values: Iterable[Any]) -> str:
    buffer = io.StringIO()
    csv.writer(buffer, lineterminator="\n").writerow([_encode(value) for value in values])
    return buffer.getvalue()


def _complete_lines(text: str) -> str:
    """Schneidet eine gerade erst halb geschriebene letzte Zeile ab."""
    if text.endswith("\n"):
        return text
    return text[: text.rfind("\n") + 1]


def _parse(text: str, columns: tuple[str, ...], header: list[str] | None = None) -> list[dict[str, Any]]:
    reader = csv.reader(io.StringIO(_complete_lines(text)))
    if header is None:
        header = next(reader, None)
        if header is None:
            return []
    rows: list[dict[str, Any]] = []
    for values in reader:
        if len(values) != len(header):
            continue
        raw = dict(zip(header, values, strict=True))
        try:
            rows.append({column: _decode(column, raw.get(column, "")) for column in columns})
        except ValueError:
            continue
    return rows


def read_rows(path: Path, columns: tuple[str, ...]) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    return _parse(path.read_text(encoding="utf-8-sig"), columns)


def _read_header(path: Path) -> list[str] | None:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return next(csv.reader([handle.readline()]), None)


# Prüfrunden --------------------------------------------------------------------------------------

def cycle_period(path: Path) -> str:
    """`2026-11` bei einer Monatsdatei, `2026-11-08` bei einer alten Tagesdatei."""
    return path.stem[len(CYCLE_PREFIX):]


def _cycle_sort_key(path: Path) -> tuple[str, int, str]:
    # Alte Tagesdateien eines Monats liegen zeitlich vor seiner Monatsdatei.
    period = cycle_period(path)
    return period[:7], 0 if len(period) > 7 else 1, period


def read_cycle_header(path: Path) -> list[str] | None:
    try:
        return _read_header(path)
    except FileNotFoundError:
        return None


def parse_cycle_text(text: str, header: list[str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for values in csv.reader(io.StringIO(_complete_lines(text))):
        row = cyclefile.decode_row(values, header)
        if row is not None:
            rows.append(row)
    return rows


def _body_text(path: Path) -> tuple[list[str] | None, str]:
    text = path.read_text(encoding="utf-8-sig")
    newline = text.find("\n")
    if newline < 0:
        return None, ""
    header = next(csv.reader([text[:newline]]), None)
    return header, text[newline + 1:]


def read_cycle_rows(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    header, body = _body_text(path)
    return parse_cycle_text(body, header) if header else []


def read_last_cycle(path: Path) -> dict[str, Any] | None:
    """Liest nur das Ende der Datei. Eine Monatsdatei wird mehrere Dutzend MB groß."""
    header = read_cycle_header(path)
    if not header:
        return None
    size = path.stat().st_size
    with path.open("rb") as handle:
        handle.seek(max(0, size - TAIL_BYTES))
        chunk = handle.read().decode("utf-8", errors="replace")
    chunk = chunk.lstrip("﻿")
    chunk = chunk[chunk.find("\n") + 1:]  # Erste, angeschnittene Zeile oder die Kopfzeile.
    rows = parse_cycle_text(chunk, header)
    return rows[-1] if rows else None


def read_first_cycle(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        header = next(csv.reader([handle.readline()]), None)
        text = handle.readline()
    rows = parse_cycle_text(text, header) if header else []
    return rows[0] if rows else None


def read_cycle_times(path: Path) -> list[str]:
    """Nur die Zeitstempel. Schneller als ganze Prüfrunden zu entpacken."""
    if not path.is_file():
        return []
    header, body = _body_text(path)
    if not header:
        return []
    if cyclefile.is_legacy(header):
        return [row["ts_utc"] for row in parse_cycle_text(body, header)]
    times: list[str] = []
    for line in _complete_lines(body).splitlines():
        # Sitzung und Zeit enthalten nie Kommas oder Anführungszeichen.
        parts = line.split(",", 2)
        if len(parts) == 3 and len(parts[1]) >= 19:
            times.append(cyclefile.iso_time(parts[1]))
    return times


def _overlaps(row: dict[str, Any], begin_key: str, end_key: str, start: str | None, end: str | None) -> bool:
    if end is not None and row[begin_key] >= end:
        return False
    return start is None or row[end_key] is None or row[end_key] > start


def _bounds(start: datetime | None, end: datetime | None) -> tuple[str | None, str | None]:
    return (iso_utc(start) if start else None, iso_utc(end) if end else None)


def _rename_with_retry(source: Path, target: Path, attempts: int = 10) -> None:
    """Unter Windows blockiert eine gerade lesende Webseite das Umbenennen kurz: dann erneut versuchen."""
    for attempt in range(attempts):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.5)


def remove_store_dir(directory: Path, archive_to: Path | None) -> None:
    """Verschiebt den Ordner der Messdaten ins Archiv oder löscht ihn, wenn archive_to None ist.

    Gelöscht wird erst nach dem Umbenennen: Scheitert es, bleibt der Ordner vollständig erhalten
    und nicht halb gelöscht. Gelingt nur das Löschen danach nicht, liegt ein Rest neben dem Ordner.
    """
    if not directory.exists():
        return
    if archive_to is not None:
        archive_to.parent.mkdir(parents=True, exist_ok=True)
        _rename_with_retry(directory, archive_to)
        return
    trash = directory.with_name(f".{directory.name}-geloescht-{time.time_ns()}")
    _rename_with_retry(directory, trash)
    shutil.rmtree(trash, ignore_errors=True)
    if trash.exists():
        log.warning("Gelöschte Messdaten konnten nicht vollständig entfernt werden: %s", trash)


class _Appender:
    """Hängt Zeilen an eine Datei an. Ist sie gesperrt, bleiben sie bis zum nächsten Versuch im Speicher."""

    def __init__(self, path: Path, columns: tuple[str, ...] | None, durable: bool) -> None:
        self.path = path
        self.columns = columns
        self.durable = durable
        self.pending: list[str] = []
        self._warned = False

    def add(self, line: str) -> None:
        self.pending.append(line)
        self.flush()

    def flush(self) -> bool:
        if not self.pending:
            return True
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            new = not self.path.is_file() or self.path.stat().st_size == 0
            with self.path.open("a", encoding="utf-8", newline="") as handle:
                if new and self.columns is not None:
                    # Die BOM lässt Excel die Umlaute richtig lesen.
                    handle.write("﻿" + _line(self.columns))
                handle.write("".join(self.pending))
                handle.flush()
                if self.durable:
                    os.fsync(handle.fileno())
        except PermissionError:
            if not self._warned:
                log.warning("%s ist gesperrt (in Excel offen?). %d Zeilen warten und werden nachgetragen.",
                            self.path.name, len(self.pending))
                self._warned = True
            return False
        if self._warned:
            log.info("%s wieder beschreibbar, wartende Zeilen nachgetragen", self.path.name)
            self._warned = False
        self.pending.clear()
        return True


class Store:
    def __init__(self, directory: Path, *, readonly: bool = False, durable: bool = True) -> None:
        self.directory = directory
        self.readonly = readonly
        self._durable = durable
        self._lock = threading.RLock()
        if readonly:
            if not self.exists(directory):
                raise FileNotFoundError(f"Keine Messdaten gefunden: {directory}")
            return
        self._load()

    def _load(self) -> None:
        directory = self.directory
        directory.mkdir(parents=True, exist_ok=True)
        self._sessions = read_rows(directory / SESSIONS_FILE, SESSION_COLUMNS)
        self._outages = read_rows(directory / OUTAGES_FILE, OUTAGE_COLUMNS)
        self._meta = {row["key"]: row["value"] or "" for row in read_rows(directory / META_FILE, META_COLUMNS)}
        events = read_rows(directory / EVENTS_FILE, EVENT_COLUMNS)
        self._next_event = max((row["id"] for row in events), default=0) + 1
        self._appenders: dict[Path, _Appender] = {}
        self._dirty: set[str] = set()
        self._cycle_keys: dict[Path, list[str]] = {}
        self._migrate_daily_files()
        newest = self.cycle_files()[-1:]
        last = read_last_cycle(newest[0]) if newest else None
        self._last_cycle: str | None = last["ts_utc"] if last else None

    @staticmethod
    def exists(directory: Path) -> bool:
        names = (SESSIONS_FILE, OUTAGES_FILE, EVENTS_FILE, META_FILE, CYCLES_DIR)
        return any((directory / name).exists() for name in names)

    def close(self) -> None:
        if self.readonly:
            return
        with self._lock:
            self._flush_pending()
            waiting = sum(len(item.pending) for item in self._appenders.values())
            if waiting or self._dirty:
                log.error("Beim Beenden nicht geschrieben: %d Zeilen, Dateien %s. Ist eine Datei in Excel offen?",
                          waiting, sorted(self._dirty))

    # Schreiben ---------------------------------------------------------------------------------

    def _require_writable(self) -> None:
        if self.readonly:
            raise RuntimeError("Speicher ist nur zum Lesen geöffnet")

    def _append(self, path: Path, columns: tuple[str, ...] | None, line: str) -> None:
        appender = self._appenders.get(path)
        if appender is None:
            appender = self._appenders[path] = _Appender(path, columns, self._durable)
        self._flush_pending()
        appender.add(line)

    def _rewrite(self, name: str) -> None:
        tables = {SESSIONS_FILE: (SESSION_COLUMNS, self._sessions), OUTAGES_FILE: (OUTAGE_COLUMNS, self._outages),
                  META_FILE: (META_COLUMNS, [{"key": k, "value": v} for k, v in sorted(self._meta.items())])}
        columns, rows = tables[name]
        path = self.directory / name
        temporary = path.with_name(path.name + ".tmp")
        try:
            with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
                handle.write(_line(columns))
                handle.writelines(_line(row.get(column) for column in columns) for row in rows)
                handle.flush()
                if self._durable:
                    os.fsync(handle.fileno())
            os.replace(temporary, path)
        except PermissionError:
            if name not in self._dirty:
                log.warning("%s ist gesperrt (in Excel offen?). Wird beim nächsten Schreiben nachgetragen.", name)
            self._dirty.add(name)
            return
        self._dirty.discard(name)

    def _save(self, name: str) -> None:
        """Schreibt eine Tabelle neu und trägt vorher nach, was wegen einer Sperre noch wartet."""
        self._flush_pending()
        self._rewrite(name)

    def _flush_pending(self) -> None:
        for name in list(self._dirty):
            self._rewrite(name)
        for appender in self._appenders.values():
            appender.flush()

    def start_session(self, moment: datetime, hostname: str, os_name: str, python_version: str,
                      linkmon_version: str, config_sha256: str, *, interface: str | None = None,
                      connection_type: str | None = None, link_mbps: float | None = None) -> int:
        self._require_writable()
        with self._lock:
            session_id = max((row["id"] for row in self._sessions), default=0) + 1
            self._sessions.append({
                "id": session_id, "started_utc": iso_utc(moment), "ended_utc": None, "end_reason": None,
                "hostname": hostname, "os": os_name, "interface": interface, "connection_type": connection_type,
                "link_mbps": link_mbps, "python_version": python_version, "linkmon_version": linkmon_version,
                "config_sha256": config_sha256,
            })
            self._save(SESSIONS_FILE)
            return session_id

    def end_session(self, session_id: int, moment: datetime, reason: str) -> None:
        self._require_writable()
        with self._lock:
            for row in self._sessions:
                if row["id"] == session_id:
                    row.update(ended_utc=iso_utc(moment), end_reason=reason)
            self._save(SESSIONS_FILE)

    def _cycle_path(self, ts_utc: str) -> Path:
        return self.directory / CYCLES_DIR / f"{CYCLE_PREFIX}{ts_utc[:7]}.csv"

    def _keys_for(self, path: Path) -> list[str]:
        keys = self._cycle_keys.get(path)
        if keys is None:
            header = read_cycle_header(path) if path.is_file() and path.stat().st_size else None
            keys = self._cycle_keys[path] = list(header[len(cyclefile.BASE_COLUMNS):]) if header else []
        return keys

    def _extend_header(self, path: Path, keys: list[str]) -> bool:
        """Verlängert die Kopfzeile um neue Ziele. Die Zeilen bleiben unverändert, sie werden nur kopiert.

        Das geschieht selten, etwa nach einem Netzwechsel. Ist die Datei gesperrt (Excel), bleibt
        die alte Kopfzeile und die neuen Ziele werden beim nächsten Mal nachgetragen.
        """
        header = [*cyclefile.BASE_COLUMNS, *keys]
        appender = self._appenders.get(path)
        if appender is not None:
            appender.columns = tuple(header)
            if not appender.flush():
                return False
        if not path.is_file() or path.stat().st_size == 0:
            self._cycle_keys[path] = list(keys)
            return True
        temporary = path.with_name(path.name + ".tmp")
        try:
            with path.open("rb") as source, temporary.open("wb") as target:
                source.readline()
                target.write(("﻿" + _line(header)).encode("utf-8"))
                shutil.copyfileobj(source, target, 1 << 20)
                target.flush()
                if self._durable:
                    os.fsync(target.fileno())
            _rename_with_retry(temporary, path, attempts=3)
        except OSError as exc:
            log.warning("Kopfzeile von %s nicht erweiterbar (%s), neue Ziele folgen später", path.name, exc)
            temporary.unlink(missing_ok=True)
            return False
        added = [key for key in keys if key not in self._keys_for(path)]
        self._cycle_keys[path] = list(keys)
        log.info("Neue Ziele in %s: %s", path.name, ", ".join(added))
        return True

    def add_cycle(self, session_id: int, moment: datetime, level: str | None, results: dict[str, Any]) -> None:
        self._require_writable()
        stamp = iso_utc(moment)
        path = self._cycle_path(stamp)
        with self._lock:
            keys = self._keys_for(path)
            new = [key for key in results if key not in keys]
            if new and self._extend_header(path, keys + new):
                keys = self._keys_for(path)
            line = _line(cyclefile.encode_row(session_id, stamp, level, results, keys))
            self._append(path, (*cyclefile.BASE_COLUMNS, *keys), line)
            self._last_cycle = stamp

    def _migrate_daily_files(self) -> None:
        """Wandelt Tagesdateien aus Version 2.0 einmalig in Monatsdateien um.

        Die Originale wandern danach nach data/archiv. Bricht das ab, wiederholt der nächste Start
        es: Zeilen, die schon in der Monatsdatei stehen, werden nicht doppelt übernommen.
        """
        daily = [path for path in self.cycle_files() if len(cycle_period(path)) > 7]
        if not daily:
            return
        log.info("Wandle %d Tagesdateien in Monatsdateien um ...", len(daily))
        by_month: dict[str, list[Path]] = {}
        for path in daily:
            by_month.setdefault(cycle_period(path)[:7], []).append(path)
        archive = self.directory.parent / "archiv" / LEGACY_ARCHIVE
        for month, paths in sorted(by_month.items()):
            target = self.directory / CYCLES_DIR / f"{CYCLE_PREFIX}{month}.csv"
            rows = [row for path in paths for row in read_cycle_rows(path)]
            seen = {row["ts_utc"] for row in rows}
            existing = [row for row in read_cycle_rows(target) if row["ts_utc"] not in seen]
            keys: list[str] = []
            decoded = [(row, json.loads(row["results"]) if row["results"] else {}) for row in rows + existing]
            for _row, results in decoded:
                keys.extend(key for key in results if key not in keys)
            temporary = target.with_name(target.name + ".tmp")
            with temporary.open("w", encoding="utf-8", newline="") as handle:
                handle.write("﻿" + _line((*cyclefile.BASE_COLUMNS, *keys)))
                for row, results in decoded:
                    handle.write(_line(cyclefile.encode_row(row["session_id"], row["ts_utc"], row["level"],
                                                            results, keys)))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
            archive.mkdir(parents=True, exist_ok=True)
            for path in paths:
                destination = archive / path.name
                if destination.exists():
                    destination = archive / f"{path.stem}_{time.time_ns()}{path.suffix}"
                os.replace(path, destination)
            log.info("%s: %d Prüfrunden umgewandelt, Tagesdateien liegen in %s", target.name, len(rows), archive)

    def open_outage(self, session_id: int | None, level: str, detail: str, start: datetime) -> int:
        self._require_writable()
        with self._lock:
            outage_id = max((row["id"] for row in self._outages), default=0) + 1
            self._outages.append({"id": outage_id, "session_id": session_id, "level": level, "detail": detail,
                                  "start_utc": iso_utc(start), "end_utc": None, "end_confirmed": 0})
            self._save(OUTAGES_FILE)
            return outage_id

    def close_outage(self, level: str, end: datetime, confirmed: bool) -> None:
        self._require_writable()
        with self._lock:
            changed = False
            for row in self._outages:
                if row["level"] == level and row["end_utc"] is None:
                    row.update(end_utc=iso_utc(end), end_confirmed=int(confirmed))
                    changed = True
            if changed:
                self._save(OUTAGES_FILE)

    def add_event(self, session_id: int | None, moment: datetime, kind: str, message: str) -> None:
        self._require_writable()
        with self._lock:
            event_id = self._next_event
            self._next_event += 1
            self._append(self.directory / EVENTS_FILE, EVENT_COLUMNS,
                         _line((event_id, session_id, iso_utc(moment), kind, message)))

    def set_meta(self, key: str, value: str) -> None:
        self._require_writable()
        with self._lock:
            if self._meta.get(key) == value:
                return
            self._meta[key] = value
            self._save(META_FILE)

    def recover_after_crash(self, fallback: datetime) -> int:
        """Räumt nach einem unsauberen Ende auf und gibt die Zahl geschlossener Ausfälle zurück.

        Offene Ausfälle enden beim letzten Herzschlag und bleiben als unbestätigt erkennbar,
        denn was danach geschah, hat das Tool nicht gesehen.
        """
        self._require_writable()
        last = self.last_cycle_time() or iso_utc(fallback)
        with self._lock:
            sessions = [row for row in self._sessions if row["ended_utc"] is None]
            for row in sessions:
                row.update(ended_utc=last, end_reason="unsauber beendet")
            outages = [row for row in self._outages if row["end_utc"] is None]
            for row in outages:
                row.update(end_utc=max(row["start_utc"], last), end_confirmed=0)
            if sessions:
                self._save(SESSIONS_FILE)
            if outages:
                self._save(OUTAGES_FILE)
            return len(outages)

    def reset(self, archive_to: Path | None) -> None:
        """Leert die Messdaten im laufenden Betrieb: archiviert oder löscht den Ordner und beginnt neu.

        Die internen Merkwerte (Route, Verbindung) bleiben erhalten, sie sind keine Messdaten.
        Scheitert das Verschieben, bleibt alles wie es war.
        """
        self._require_writable()
        with self._lock:
            self._flush_pending()
            meta = dict(self._meta)
            remove_store_dir(self.directory, archive_to)
            self._load()
            self._meta = meta
            if meta:
                self._rewrite(META_FILE)

    # Lesen -------------------------------------------------------------------------------------

    def _table(self, name: str, columns: tuple[str, ...]) -> list[dict[str, Any]]:
        if not self.readonly:
            with self._lock:
                source = self._sessions if name == SESSIONS_FILE else self._outages
                return [dict(row) for row in source]
        return read_rows(self.directory / name, columns)

    def get_meta(self, key: str) -> str | None:
        if not self.readonly:
            with self._lock:
                return self._meta.get(key)
        for row in read_rows(self.directory / META_FILE, META_COLUMNS):
            if row["key"] == key:
                return row["value"] or ""
        return None

    def sessions(self, start: datetime | None = None, end: datetime | None = None) -> list[dict[str, Any]]:
        low, high = _bounds(start, end)
        rows = [row for row in self._table(SESSIONS_FILE, SESSION_COLUMNS)
                if (high is None or row["started_utc"] < high) and (low is None or row["ended_utc"] is None
                                                                     or row["ended_utc"] >= low)]
        return sorted(rows, key=lambda row: row["id"])

    def outages(self, start: datetime | None = None, end: datetime | None = None) -> list[dict[str, Any]]:
        low, high = _bounds(start, end)
        rows = [row for row in self._table(OUTAGES_FILE, OUTAGE_COLUMNS)
                if _overlaps(row, "start_utc", "end_utc", low, high)]
        return sorted(rows, key=lambda row: (row["start_utc"], row["id"]))

    def open_outages(self) -> list[dict[str, Any]]:
        return [row for row in self.outages() if row["end_utc"] is None]

    def events(self) -> list[dict[str, Any]]:
        return read_rows(self.directory / EVENTS_FILE, EVENT_COLUMNS)

    def cycle_files(self, start: datetime | None = None, end: datetime | None = None) -> list[Path]:
        folder = self.directory / CYCLES_DIR
        if not folder.is_dir():
            return []
        files = sorted(folder.glob(f"{CYCLE_PREFIX}*.csv"), key=_cycle_sort_key)
        low = start.astimezone(UTC).strftime("%Y-%m-%d") if start else None
        high = end.astimezone(UTC).strftime("%Y-%m-%d") if end else None
        # Ein Monat `2026-11` umfasst alle Tage von `2026-11-01` bis `2026-11-31`.
        return [path for path in files
                if (low is None or cycle_period(path) >= low[:len(cycle_period(path))])
                and (high is None or cycle_period(path) <= high[:len(cycle_period(path))])]

    def cycles(self, start: datetime | None = None, end: datetime | None = None) -> Iterator[dict[str, Any]]:
        low, high = _bounds(start, end)
        for path in self.cycle_files(start, end):
            for row in read_cycle_rows(path):
                if (low is None or row["ts_utc"] >= low) and (high is None or row["ts_utc"] < high):
                    yield row

    def cycle_times(self, start: datetime | None = None, end: datetime | None = None) -> list[str]:
        low, high = _bounds(start, end)
        return [stamp for path in self.cycle_files(start, end) for stamp in read_cycle_times(path)
                if (low is None or stamp >= low) and (high is None or stamp < high)]

    def count_cycles(self) -> int:
        total = 0
        for path in self.cycle_files():
            with path.open("rb") as handle:
                total += max(0, sum(chunk.count(b"\n") for chunk in iter(lambda: handle.read(1 << 20), b"")) - 1)
        return total

    def first_cycle_time(self) -> str | None:
        files = self.cycle_files()
        row = read_first_cycle(files[0]) if files else None
        return row["ts_utc"] if row else None

    def latest_cycle(self) -> dict[str, Any] | None:
        files = self.cycle_files()
        return read_last_cycle(files[-1]) if files else None

    def last_cycle_time(self) -> str | None:
        if not self.readonly:
            with self._lock:
                return self._last_cycle
        row = self.latest_cycle()
        return row["ts_utc"] if row else None
