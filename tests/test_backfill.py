from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta

from conftest import results, session

from linkmon import backfill
from linkmon.cli import main
from linkmon.config import Config
from linkmon.linestats import DROPOUT_DETAIL
from linkmon.monitor import Monitor
from linkmon.store import Store

DOWN = dict(isp=None, ext_a=None, ext_b=None)
START = datetime(2026, 11, 10, 20, tzinfo=UTC)


def _rounds(store: Store, start: datetime, pattern: str) -> int:
    """Eine Runde je 3 s. „x“: Leitung weg, „.“: alles gut."""
    session_id = session(store, start)
    for index, mark in enumerate(pattern):
        store.add_cycle(session_id, start + timedelta(seconds=3 * index), "line" if mark == "x" else None,
                        results(**DOWN) if mark == "x" else results())
    return session_id


def _seed_with_logged_outage(store: Store) -> int:
    session_id = _rounds(store, START, "..x...xx...xxxx....x..")
    # Der lange Ausfall wurde schon damals geloggt und darf nicht doppelt erscheinen.
    store.open_outage(session_id, "line", "Erster Provider-Hop und externe Ziele nicht erreichbar",
                      START + timedelta(seconds=33))
    store.close_outage("line", START + timedelta(seconds=45), True)
    return session_id


def test_finds_only_dropouts_missing_from_the_list(config: Config, store: Store) -> None:
    _seed_with_logged_outage(store)
    found = backfill.find_missing_dropouts(store, 9, None, START + timedelta(minutes=5))
    assert [(item.start - START).total_seconds() for item in found] == [6, 18, 57]
    assert [item.duration_s for item in found][:2] == [3, 6]
    later = backfill.find_missing_dropouts(store, 9, START + timedelta(seconds=10), START + timedelta(minutes=5))
    assert len(later) == 2  # --from


def test_apply_writes_backup_and_is_harmless_twice(config: Config, store: Store) -> None:
    session_id = _seed_with_logged_outage(store)
    archive = config.data_dir / "archiv"
    found = backfill.find_missing_dropouts(store, 9, None, START + timedelta(minutes=5))
    assert backfill.apply_dropouts(store, found, archive, START + timedelta(minutes=5)) == 3
    added = [row for row in store.outages() if row["detail"] == DROPOUT_DETAIL]
    assert len(added) == 3 and all(row["session_id"] == session_id and row["level"] == "line" for row in added)
    backups = list(archive.glob("ausfaelle_vor_nachtrag_*.csv"))
    assert len(backups) == 1 and "Erster Provider-Hop" in backups[0].read_text(encoding="utf-8-sig")
    assert store.get_meta(backfill.META_KEY)
    assert backfill.apply_dropouts(store, found, archive, START + timedelta(minutes=6)) == 0
    assert backfill.find_missing_dropouts(store, 9, None, START + timedelta(minutes=5)) == []
    assert len(store.outages()) == 4


def test_dropout_at_the_end_of_a_month_gets_its_end(config: Config, store: Store) -> None:
    start = datetime(2026, 10, 31, 23, 59, 54, tzinfo=UTC)
    _rounds(store, start, ".x.")  # Aussetzer in der letzten Runde des Oktobers, gut ab 1. November
    assert len(store.cycle_files()) == 2
    found = backfill.find_missing_dropouts(store, 9, None, start + timedelta(minutes=1))
    assert len(found) == 1 and found[0].confirmed and found[0].end == datetime(2026, 11, 1, tzinfo=UTC)


def test_cli_without_running_service_writes_itself(config: Config, store: Store, capsys) -> None:
    _seed_with_logged_outage(store)
    store.close()
    arguments = ["--config", str(config.path), "reanalyse"]
    assert main(arguments + ["--dry-run"]) == 0
    assert "Gefunden: 3" in capsys.readouterr().out
    assert len(Store(config.store_dir, readonly=True).outages()) == 1
    assert main(arguments) == 0
    assert "3 Aussetzer in die Ausfallliste eingetragen" in capsys.readouterr().out
    assert len(Store(config.store_dir, readonly=True).outages()) == 4
    assert main(arguments) == 0
    assert "Nichts nachzutragen" in capsys.readouterr().out


def test_running_service_takes_over_the_writing(config: Config, store: Store) -> None:
    """Der Dienst hält die Ausfallliste im Speicher. Er muss die Funde selbst eintragen, sonst gingen sie verloren."""
    _seed_with_logged_outage(store)
    found = backfill.find_missing_dropouts(store, 9, None, START + timedelta(minutes=5))
    backfill.write_request(config.data_dir, found)
    monitor = Monitor(config, store, probe_runner=lambda: None, clock=lambda: START + timedelta(minutes=5))
    monitor._handle_backfill_request()
    result = backfill.wait_for_result(config.data_dir, 1)
    assert result == {"ok": True, "added": 3, "message": ""}
    # Ein späterer Ausfall des Dienstes schreibt die Liste neu: Die Nachträge müssen dabei erhalten bleiben.
    store.open_outage(None, "router", "Nicht erreichbar: 192.168.0.1", START + timedelta(minutes=6))
    assert sum(row["detail"] == DROPOUT_DETAIL for row in Store(config.store_dir, readonly=True).outages()) == 3


def test_cli_hands_over_to_a_running_service(config: Config, store: Store, capsys) -> None:
    from linkmon.cli import _lock

    _seed_with_logged_outage(store)
    lock = _lock(config)
    assert lock.acquire()  # Tut so, als liefe der Messdienst
    monitor = Monitor(config, store, probe_runner=lambda: None, clock=lambda: START + timedelta(minutes=5))
    stop = threading.Event()

    def service() -> None:
        while not stop.is_set():
            monitor._handle_backfill_request()
            stop.wait(0.1)

    worker = threading.Thread(target=service)
    worker.start()
    try:
        assert main(["--config", str(config.path), "reanalyse"]) == 0
    finally:
        stop.set()
        worker.join()
        lock.release()
    assert "Die Messung läuft weiter" in capsys.readouterr().out
    assert sum(row["detail"] == DROPOUT_DETAIL for row in store.outages()) == 3
