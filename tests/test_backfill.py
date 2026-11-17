from __future__ import annotations

from datetime import UTC, datetime, timedelta

from conftest import results, session

from linkmon.backfill import BACKFILL_KEY, backfill_dropouts, find_missing_dropouts
from linkmon.config import Config
from linkmon.linestats import DROPOUT_DETAIL
from linkmon.monitor import Monitor
from linkmon.store import Store

DOWN = dict(isp=None, ext_a=None, ext_b=None)


def _rounds(store: Store, start: datetime, pattern: str) -> int:
    """Eine Runde je 3 s. „x“: Leitung weg, „.“: alles gut."""
    session_id = session(store, start)
    for index, mark in enumerate(pattern):
        store.add_cycle(session_id, start + timedelta(seconds=3 * index), "line" if mark == "x" else None,
                        results(**DOWN) if mark == "x" else results())
    return session_id


def test_backfill_adds_short_dropouts_once_and_keeps_a_backup(config: Config, store: Store) -> None:
    start = datetime(2026, 11, 10, 20, tzinfo=UTC)
    session_id = _rounds(store, start, "..x...xx...xxxx....x..")
    # Der lange Ausfall wurde schon damals geloggt und darf nicht doppelt erscheinen.
    store.open_outage(session_id, "line", "Erster Provider-Hop und externe Ziele nicht erreichbar",
                      start + timedelta(seconds=33))
    store.close_outage("line", start + timedelta(seconds=45), True)
    before = start + timedelta(minutes=5)
    archive = config.data_dir / "archiv"

    assert backfill_dropouts(store, 9, before, archive) == 3
    rows = store.outages()
    added = [row for row in rows if row["detail"] == DROPOUT_DETAIL]
    assert [row["start_utc"] for row in added] == ["2026-11-10T20:00:06+00:00", "2026-11-10T20:00:18+00:00",
                                                    "2026-11-10T20:00:57+00:00"]
    assert [row["end_utc"] for row in added][:2] == ["2026-11-10T20:00:09+00:00", "2026-11-10T20:00:24+00:00"]
    assert all(row["session_id"] == session_id and row["level"] == "line" for row in added)
    assert len(rows) == 4
    backups = list(archive.glob("ausfaelle_vor_nachtrag_*.csv"))
    assert len(backups) == 1 and "Erster Provider-Hop" in backups[0].read_text(encoding="utf-8-sig")
    assert store.get_meta(BACKFILL_KEY)

    assert backfill_dropouts(store, 9, before + timedelta(hours=1), archive) == 0  # Nur einmal
    assert len(store.outages()) == 4


def test_backfill_survives_a_reload_and_old_readers(config: Config, store: Store) -> None:
    start = datetime(2026, 11, 10, 20, tzinfo=UTC)
    _rounds(store, start, "..x..")
    backfill_dropouts(store, 9, start + timedelta(minutes=1), config.data_dir / "archiv")
    store.close()
    reopened = Store(config.store_dir, readonly=True)
    assert [row["detail"] for row in reopened.outages()] == [DROPOUT_DETAIL]
    assert reopened.get_meta(BACKFILL_KEY)


def test_backfill_ignores_rounds_after_the_new_version_started(config: Config, store: Store) -> None:
    start = datetime(2026, 11, 10, 20, tzinfo=UTC)
    _rounds(store, start, "..x...x..")
    found = find_missing_dropouts(store, 9, start + timedelta(seconds=12))
    assert [(item.start - start).total_seconds() for item in found] == [6]


def test_backfill_closes_a_dropout_at_the_end_of_a_month(config: Config, store: Store) -> None:
    start = datetime(2026, 10, 31, 23, 59, 54, tzinfo=UTC)
    _rounds(store, start, ".x.")  # Aussetzer in der letzten Runde des Oktobers, gut ab 1. November
    assert len(store.cycle_files()) == 2
    found = find_missing_dropouts(store, 9, start + timedelta(minutes=1))
    assert len(found) == 1 and found[0].confirmed
    assert found[0].end == datetime(2026, 11, 1, tzinfo=UTC)


def test_monitor_backfills_only_in_real_operation(config: Config, store: Store) -> None:
    assert Monitor(config, store, probe_runner=lambda: None)._backfill is False
    assert Monitor(config, store, probe_runner=lambda: None, backfill=True)._backfill is True
