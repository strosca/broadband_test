from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from conftest import session, utc

from linkmon.config import Config
from linkmon.store import CYCLES_DIR, EVENTS_FILE, OUTAGES_FILE, SESSIONS_FILE, Store


def test_values_keep_their_types_after_a_restart(config: Config, store: Store) -> None:
    session_id = session(store, utc(2026, 11, 1, 10), connection="wlan")
    store.open_outage(session_id, "line", 'Befund mit Komma, und "Anführungszeichen"', utc(2026, 11, 1, 10, 5))
    store.close_outage("line", utc(2026, 11, 1, 10, 6), True)
    again = Store(config.store_dir)
    row = again.sessions()[0]
    assert row["id"] == 1 and row["link_mbps"] == 1000.0 and row["connection_type"] == "wlan"
    assert row["ended_utc"] is None
    outage = again.outages()[0]
    assert outage["session_id"] == 1 and outage["end_confirmed"] == 1
    assert outage["detail"] == 'Befund mit Komma, und "Anführungszeichen"'


def test_ids_continue_after_restart(config: Config, store: Store) -> None:
    session(store, utc(2026, 11, 1, 10))
    store.add_event(1, utc(2026, 11, 1, 10), "warnung", "eins")
    store.close()
    restarted = Store(config.store_dir)
    assert session(restarted, utc(2026, 11, 1, 11)) == 2
    restarted.add_event(2, utc(2026, 11, 1, 11), "warnung", "zwei")
    assert [row["id"] for row in restarted.events()] == [1, 2]


def test_old_session_file_without_connection_columns_still_reads(config: Config) -> None:
    config.store_dir.mkdir(parents=True)
    (config.store_dir / SESSIONS_FILE).write_text(
        "\ufeffid,started_utc,ended_utc,end_reason,hostname,os,python_version,linkmon_version,ookla_version,"
        "config_sha256\n1,2026-11-07T19:05:00+00:00,,,host,Windows,3.14,1.0.0,Ookla,abc\n", encoding="utf-8")
    store = Store(config.store_dir)
    assert store.sessions()[0]["connection_type"] is None
    assert session(store, utc(2026, 11, 8)) == 2


def test_half_written_last_line_is_ignored(config: Config, store: Store) -> None:
    session = store.start_session(utc(2026, 11, 1), "h", "o", "p", "v", "c")
    store.add_cycle(session, utc(2026, 11, 1, 10), None, {"dns": [1, 3.0, ""]})
    path = next((config.store_dir / CYCLES_DIR).glob("*.csv"))
    with path.open("a", encoding="utf-8") as handle:
        handle.write('1,2026-11-01T10:00:03+00:00,,"{""dns""')  # Abgebrochen mitten im Schreiben.
    reader = Store(config.store_dir, readonly=True)
    assert reader.cycle_times() == ["2026-11-01T10:00:00+00:00"]
    assert reader.last_cycle_time() == "2026-11-01T10:00:00+00:00"


def test_cycles_are_split_by_day_and_read_by_range(config: Config, store: Store) -> None:
    session = store.start_session(utc(2026, 11, 1), "h", "o", "p", "v", "c")
    for hour in (22, 23, 24, 25):
        store.add_cycle(session, utc(2026, 11, 1) + timedelta(hours=hour), None, {})
    assert len(list((config.store_dir / CYCLES_DIR).glob("*.csv"))) == 2
    reader = Store(config.store_dir, readonly=True)
    assert reader.cycle_times(utc(2026, 11, 1, 23), utc(2026, 11, 2, 1)) == [
        "2026-11-01T23:00:00+00:00", "2026-11-02T00:00:00+00:00"]
    assert reader.first_cycle_time() == "2026-11-01T22:00:00+00:00"
    assert reader.last_cycle_time() == "2026-11-02T01:00:00+00:00"
    assert reader.count_cycles() == 4


def test_long_day_file_reads_only_its_end(config: Config, store: Store) -> None:
    session = store.start_session(utc(2026, 11, 1), "h", "o", "p", "v", "c")
    for second in range(0, 3000 * 3, 3):
        store.add_cycle(session, utc(2026, 11, 1) + timedelta(seconds=second), None, {"x": [1, 1.0, "y" * 20]})
    latest = Store(config.store_dir, readonly=True).latest_cycle()
    assert latest is not None and latest["ts_utc"] == "2026-11-01T02:29:57+00:00"


def test_locked_file_keeps_rows_until_it_is_free(config: Config, store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    """Simuliert eine in Excel geöffnete Datei: Schreiben scheitert, die Messung läuft weiter."""
    original = Path.open
    locked = {"on": True}

    def fake_open(self: Path, mode: str = "r", *args, **kwargs):
        if locked["on"] and self.name in (EVENTS_FILE, OUTAGES_FILE + ".tmp") and mode != "r":
            raise PermissionError("gesperrt")
        return original(self, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fake_open)
    store.add_event(None, utc(2026, 11, 1, 10), "warnung", "erste")
    store.open_outage(None, "line", "x", utc(2026, 11, 1, 10))
    assert store.events() == [] and not (config.store_dir / OUTAGES_FILE).exists()
    assert store.outages()[0]["level"] == "line"  # Im Speicher schon da.
    locked["on"] = False
    session(store, utc(2026, 11, 1, 10, 1))  # Irgendein Schreibvorgang trägt nach.
    reader = Store(config.store_dir, readonly=True)
    assert [row["message"] for row in reader.events()] == ["erste"]
    assert reader.outages()[0]["level"] == "line"


def test_reader_sees_open_outage_and_meta_of_the_running_service(config: Config, store: Store) -> None:
    store.open_outage(1, "line", "x", utc(2026, 11, 1, 10))
    store.set_meta("schluessel", "wert")
    reader = Store(config.store_dir, readonly=True)
    assert reader.open_outages()[0]["start_utc"] == "2026-11-01T10:00:00+00:00"
    assert reader.get_meta("schluessel") == "wert"
    store.close_outage("line", utc(2026, 11, 1, 10, 5), True)
    assert reader.open_outages() == []
    assert reader.outages()[0]["end_confirmed"] == 1


def test_reading_missing_data_fails_clearly(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="Keine Messdaten"):
        Store(tmp_path / "leer", readonly=True)


def test_files_open_in_excel_with_umlauts(config: Config, store: Store) -> None:
    store.add_event(None, utc(2026, 11, 1), "warnung", "Zeitüberschreitung")
    assert (config.store_dir / EVENTS_FILE).read_bytes().startswith(b"\xef\xbb\xbf")
