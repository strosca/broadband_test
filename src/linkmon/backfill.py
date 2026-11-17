"""Einmaliger Nachtrag kurzer Leitungsaussetzer in die Ausfallliste.

Ältere Versionen eröffneten einen Ausfall erst nach drei fehlerhaften Prüfrunden in Folge. Kürzere Aussetzer stehen
deshalb nur in den Prüfrunden, nicht in ausfaelle.csv. Beim ersten Start einer neueren Version trägt der Messdienst
sie einmal nach. Vorher sichert er die Ausfallliste, danach merkt er sich in zustand.csv, dass der Nachtrag erledigt
ist. Die Prüfrunden selbst bleiben unverändert.
"""

from __future__ import annotations

import logging
import shutil
from datetime import datetime
from pathlib import Path

import pandas as pd

from linkmon.linestats import dropouts_from_samples
from linkmon.quality import read_cycle_frame
from linkmon.stats import Outage
from linkmon.store import OUTAGES_FILE, Store
from linkmon.timeutil import iso_utc, parse_iso

log = logging.getLogger(__name__)

BACKFILL_KEY = "nachtrag_kurze_aussetzer"


def _logged(store: Store) -> list[Outage]:
    rows = store.outages()
    return [Outage(row["level"], row["detail"] or "", parse_iso(row["start_utc"]),
                   parse_iso(row["end_utc"] or row["start_utc"]), bool(row["end_confirmed"])) for row in rows]


def _session_at(sessions: list[dict], moment: datetime) -> int | None:
    stamp = iso_utc(moment)
    for row in reversed(sessions):
        if row["started_utc"] <= stamp and (row["ended_utc"] is None or stamp <= row["ended_utc"]):
            return row["id"]
    return None


def find_missing_dropouts(store: Store, gap_threshold_s: float, before: datetime) -> list[Outage]:
    """Kurze Aussetzer vor `before`, die in der Ausfallliste fehlen. Liest eine Monatsdatei nach der anderen.

    An der Monatsgrenze kommt die erste Runde des Folgemonats hinzu, damit ein Aussetzer in der letzten Runde
    eines Monats sein Ende findet.
    """
    logged = _logged(store)
    found: list[Outage] = []
    files = store.cycle_files()
    limit = pd.Timestamp(before).tz_convert("UTC")
    following = read_cycle_frame(files[0]) if files else None
    for index in range(len(files)):
        frame = following
        following = read_cycle_frame(files[index + 1]) if index + 1 < len(files) else None
        if frame is None or frame.empty:
            continue
        if following is not None and not following.empty:
            frame = pd.concat([frame, following.head(1)], ignore_index=True, sort=False)
        frame = frame[frame["ts"] < limit].reset_index(drop=True)
        found += dropouts_from_samples(frame, logged + found, gap_threshold_s)
    return found


def backfill_dropouts(store: Store, gap_threshold_s: float, before: datetime, archive: Path) -> int:
    """Trägt fehlende kurze Aussetzer einmalig ein und gibt die Zahl der Nachträge zurück."""
    if store.get_meta(BACKFILL_KEY):
        return 0
    missing = find_missing_dropouts(store, gap_threshold_s, before)
    # Wurden die Messdaten währenddessen geleert (linkmon clear), gehören die Funde nicht in die neuen Daten.
    first = store.first_cycle_time()
    missing = [item for item in missing if first is not None and iso_utc(item.start) >= first]
    if missing:
        source = store.directory / OUTAGES_FILE
        if source.is_file():
            archive.mkdir(parents=True, exist_ok=True)
            backup = archive / f"ausfaelle_vor_nachtrag_{before.strftime('%Y%m%d_%H%M%S')}.csv"
            shutil.copy2(source, backup)
            log.info("Ausfallliste gesichert: %s", backup)
        sessions = store.sessions()
        store.add_closed_outages([(_session_at(sessions, item.start), item.level, item.detail, item.start, item.end,
                                   item.confirmed) for item in missing])
    store.set_meta(BACKFILL_KEY, f"{iso_utc(before)}, {len(missing)} nachgetragen")
    store.add_event(None, before, "nachtrag", f"{len(missing)} kurze Leitungsaussetzer aus den Prüfrunden nachgetragen")
    log.info("%d kurze Leitungsaussetzer aus den Prüfrunden in die Ausfallliste nachgetragen", len(missing))
    return len(missing)
