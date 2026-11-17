"""Nachtrag kurzer Leitungsaussetzer in die Ausfallliste: `linkmon reanalyse`.

Ältere Versionen eröffneten einen Ausfall erst nach drei fehlerhaften Prüfrunden in Folge. Kürzere Aussetzer stehen
deshalb nur in den Prüfrunden, nicht in ausfaelle.csv. `linkmon reanalyse` sucht sie als eigener Prozess und nur
lesend, Monat für Monat. Das Eintragen ist kurz:

- Läuft kein Messdienst, trägt der Befehl selbst ein.
- Läuft einer, übergibt der Befehl die Funde als Auftrag im Datenordner. Der Dienst trägt sie vor der nächsten
  Prüfrunde ein. Er hält die Ausfallliste im Speicher und schreibt sie bei jeder Änderung neu: Ein zweiter Prozess,
  der selbst in die Datei schreibt, würde sonst beim nächsten Ausfall überschrieben.

Vor dem Eintragen wird die Ausfallliste unter data/archiv gesichert. Die Prüfrunden bleiben unverändert. Mehrfaches
Ausführen ist harmlos: Was schon in der Liste steht, wird nicht noch einmal eingetragen.
"""

from __future__ import annotations

import json
import logging
import shutil
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import pandas as pd

from linkmon.linestats import dropouts_from_samples
from linkmon.quality import read_cycle_frame
from linkmon.stats import Outage
from linkmon.store import OUTAGES_FILE, Store
from linkmon.timeutil import iso_utc, parse_iso

log = logging.getLogger(__name__)

META_KEY = "nachtrag_kurze_aussetzer"
REQUEST_FILE = "nachtrag_auftrag.json"
RESULT_FILE = "nachtrag_ergebnis.json"


def _logged(rows: list[dict]) -> list[Outage]:
    return [Outage(row["level"], row["detail"] or "", parse_iso(row["start_utc"]),
                   parse_iso(row["end_utc"] or row["start_utc"]), bool(row["end_confirmed"])) for row in rows]


def find_missing_dropouts(store: Store, gap_threshold_s: float, start: datetime | None, before: datetime,
                          progress: Callable[[Path, int], None] | None = None) -> list[Outage]:
    """Kurze Aussetzer zwischen `start` und `before`, die in der Ausfallliste fehlen.

    Liest eine Monatsdatei nach der anderen, es liegen höchstens zwei gleichzeitig im Speicher. An der Monatsgrenze
    kommt die erste Runde des Folgemonats dazu, damit ein Aussetzer in der letzten Runde eines Monats sein Ende findet.
    """
    logged = _logged(store.outages())
    found: list[Outage] = []
    files = store.cycle_files(start, before)
    low = pd.Timestamp(start).tz_convert("UTC") if start else None
    high = pd.Timestamp(before).tz_convert("UTC")
    following = read_cycle_frame(files[0]) if files else None
    for index, path in enumerate(files):
        frame = following
        following = read_cycle_frame(files[index + 1]) if index + 1 < len(files) else None
        if frame is None or frame.empty:
            continue
        if following is not None and not following.empty:
            frame = pd.concat([frame, following.head(1)], ignore_index=True, sort=False)
        mask = frame["ts"] < high
        if low is not None:
            mask &= frame["ts"] >= low
        new = dropouts_from_samples(frame[mask].reset_index(drop=True), logged + found, gap_threshold_s)
        found += new
        if progress:
            progress(path, len(new))
    return found


def _session_at(sessions: list[dict], stamp: str) -> int | None:
    for row in reversed(sessions):
        if row["started_utc"] <= stamp and (row["ended_utc"] is None or stamp <= row["ended_utc"]):
            return row["id"]
    return None


def apply_dropouts(store: Store, items: list[Outage], archive: Path, moment: datetime) -> int:
    """Trägt Funde in die Ausfallliste ein. Läuft im Messdienst oder ohne ihn, nie in zwei Prozessen zugleich.

    Prüft gegen den aktuellen Stand noch einmal auf Überschneidungen: Zwischen Suche und Eintrag kann der Dienst einen
    Ausfall geloggt oder jemand die Messdaten geleert haben.
    """
    current = _logged(store.outages())
    first = store.first_cycle_time()
    items = [item for item in items
             if first is not None and iso_utc(item.start) >= first
             and not any(other.start < item.end and item.start <= other.end for other in current)]
    if items:
        source = store.directory / OUTAGES_FILE
        if source.is_file():
            archive.mkdir(parents=True, exist_ok=True)
            backup = archive / f"ausfaelle_vor_nachtrag_{moment.strftime('%Y%m%d_%H%M%S')}.csv"
            shutil.copy2(source, backup)
            log.info("Ausfallliste gesichert: %s", backup)
        sessions = store.sessions()
        store.add_closed_outages([(_session_at(sessions, iso_utc(item.start)), item.level, item.detail, item.start,
                                   item.end, item.confirmed) for item in items])
    store.set_meta(META_KEY, f"{iso_utc(moment)}, {len(items)} nachgetragen")
    store.add_event(None, moment, "nachtrag", f"{len(items)} kurze Leitungsaussetzer aus den Prüfrunden nachgetragen")
    log.info("%d kurze Leitungsaussetzer aus den Prüfrunden in die Ausfallliste nachgetragen", len(items))
    return len(items)


# Übergabe an einen laufenden Messdienst: Auftrag und Ergebnis als kleine JSON-Dateien im Datenordner.

def write_request(data_dir: Path, items: list[Outage]) -> None:
    (data_dir / RESULT_FILE).unlink(missing_ok=True)
    payload = [{"level": item.level, "detail": item.detail, "start": iso_utc(item.start), "end": iso_utc(item.end),
                "confirmed": item.confirmed} for item in items]
    temporary = data_dir / (REQUEST_FILE + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    temporary.replace(data_dir / REQUEST_FILE)


def take_request(data_dir: Path) -> list[Outage] | None:
    """Holt einen wartenden Auftrag ab. None heißt: keiner da. Ein unlesbarer Auftrag trägt nichts ein."""
    path = data_dir / REQUEST_FILE
    try:
        content = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        content = []
    path.unlink(missing_ok=True)
    items: list[Outage] = []
    for entry in content if isinstance(content, list) else []:
        try:
            items.append(Outage(str(entry["level"]), str(entry["detail"]), parse_iso(entry["start"]),
                                parse_iso(entry["end"]), bool(entry["confirmed"])))
        except (KeyError, TypeError, ValueError):
            continue
    return items


def write_result(data_dir: Path, ok: bool, added: int, message: str = "") -> None:
    payload = {"ok": ok, "added": added, "message": message}
    (data_dir / RESULT_FILE).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def wait_for_result(data_dir: Path, timeout_s: float) -> dict | None:
    deadline = time.monotonic() + timeout_s
    path = data_dir / RESULT_FILE
    while time.monotonic() < deadline:
        try:
            content = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            time.sleep(0.3)
            continue
        path.unlink(missing_ok=True)
        return content
    return None
