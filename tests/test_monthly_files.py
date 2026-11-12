from __future__ import annotations

import json
from datetime import timedelta

import pandas as pd
from conftest import results, utc

from linkmon import cyclefile
from linkmon.config import Config
from linkmon.quality import SampleCache, load_samples, parse_cycles, read_cycle_frame
from linkmon.store import CYCLES_DIR, Store, read_cycle_rows


def _month_file(config: Config, month: str = "2026-11"):
    return config.store_dir / CYCLES_DIR / f"pruefungen_{month}.csv"


def test_cells_round_trip() -> None:
    assert cyclefile.encode_cell([1, 23.4, ""]) == "23"
    assert cyclefile.encode_cell([1, 3.0, ""]) == "3"
    assert cyclefile.encode_cell([1, 0.45, ""]) == "0.5"
    assert cyclefile.encode_cell([1, None, "WLAN"]) == "ok"
    assert cyclefile.encode_cell([0, None, "keine Antwort"]) == "x"
    assert cyclefile.encode_cell([0, None, "dns.example: Timeout"]) == "x:dns.example: Timeout"
    assert cyclefile.decode_cell("23") == [1, 23.0, ""]
    assert cyclefile.decode_cell("ok") == [1, None, ""]
    assert cyclefile.decode_cell("x") == [0, None, "keine Antwort"]
    assert cyclefile.decode_cell("x:Timeout") == [0, None, "Timeout"]
    assert cyclefile.decode_cell("") is None


def test_row_is_compact_and_readable(config: Config, store: Store) -> None:
    store.add_cycle(1, utc(2026, 11, 8, 19, 0, 18), None, results())
    lines = _month_file(config).read_text(encoding="utf-8-sig").splitlines()
    assert lines[0] == "sitzung,zeit_utc,ebene,lan:192.168.0.1,isp:92.79.214.24,ext:1.1.1.1,ext:8.8.8.8,dns,https,interface"
    assert lines[1] == "1,2026-11-08 19:00:18,,3,12,20,22,15,80,ok"


def test_new_target_extends_the_header_and_keeps_old_rows(config: Config, store: Store) -> None:
    store.add_cycle(1, utc(2026, 11, 8), None, {"ext:1.1.1.1": [1, 20.0, ""]})
    store.add_cycle(1, utc(2026, 11, 8, 0, 0, 3), "router", {"ext:1.1.1.1": [1, 21.0, ""],
                                                             "lan:192.168.0.1": [0, None, "keine Antwort"]})
    lines = _month_file(config).read_text(encoding="utf-8-sig").splitlines()
    assert lines == ["sitzung,zeit_utc,ebene,ext:1.1.1.1,lan:192.168.0.1",
                     "1,2026-11-08 00:00:00,,20", "1,2026-11-08 00:00:03,router,21,x"]
    rows = Store(config.store_dir, readonly=True).cycles()
    assert [json.loads(row["results"]) for row in rows] == [
        {"ext:1.1.1.1": [1, 20.0, ""]},
        {"ext:1.1.1.1": [1, 21.0, ""], "lan:192.168.0.1": [0, None, "keine Antwort"]},
    ]
    frame = read_cycle_frame(_month_file(config))
    assert frame["lan:192.168.0.1|ok"].tolist() == [-1, 0]


def test_restart_continues_the_same_month_file(config: Config, store: Store) -> None:
    store.add_cycle(1, utc(2026, 11, 8), None, results())
    store.close()
    again = Store(config.store_dir, durable=False)
    again.add_cycle(2, utc(2026, 11, 8, 0, 0, 3), None, results())
    assert len(list((config.store_dir / CYCLES_DIR).glob("*.csv"))) == 1
    assert again.count_cycles() == 2


def test_fast_reader_matches_the_row_by_row_reader(config: Config, store: Store) -> None:
    moment = utc(2026, 11, 8)
    for second in range(0, 300, 3):
        down = second % 30 == 0
        store.add_cycle(1, moment + timedelta(seconds=second), None,
                        results(ext_a=None if down else 20.0 + second % 7, dns=None if down else 4.5))
    store.add_cycle(1, moment + timedelta(seconds=300), None, {**results(), "https": [0, None, "URLError"]})
    path = _month_file(config)
    fast = read_cycle_frame(path)
    slow = parse_cycles(read_cycle_rows(path))
    assert list(fast.columns) == list(slow.columns)
    pd.testing.assert_frame_equal(fast.drop(columns="ts"), slow.drop(columns="ts"), check_dtype=False)
    assert fast["ts"].tolist() == slow["ts"].tolist()


def test_cache_reads_only_new_rows(config: Config, store: Store, monkeypatch) -> None:
    import linkmon.quality as quality

    store.add_cycle(1, utc(2026, 11, 8), None, results())
    cache = SampleCache()
    path = _month_file(config)
    assert len(cache.frame(path)) == 1
    calls: list[int] = []
    original = quality.frame_from_text
    monkeypatch.setattr(quality, "frame_from_text", lambda text, header: calls.append(len(text)) or original(text, header))
    store.add_cycle(1, utc(2026, 11, 8, 0, 0, 3), None, results())
    assert len(cache.frame(path)) == 2
    assert len(calls) == 1 and calls[0] < 80  # Nur die neue Zeile, nicht die ganze Datei.
    # Neues Ziel: Die Kopfzeile ändert sich, die Datei wird ganz neu gelesen.
    store.add_cycle(1, utc(2026, 11, 8, 0, 0, 6), None, {**results(), "lan:192.168.68.1": [1, 2.0, ""]})
    frame = cache.frame(path)
    assert len(frame) == 3 and frame["lan:192.168.68.1|ok"].tolist() == [-1, -1, 1]


def test_cache_handles_new_rows_that_are_all_shorter_than_the_header(config: Config, store: Store) -> None:
    # Fällt ein Router weg, enden die neuen Zeilen früher: Leere Felder am Ende werden nicht geschrieben.
    store.add_cycle(1, utc(2026, 11, 8), None, results())
    cache = SampleCache()
    path = _month_file(config)
    cache.frame(path)
    store.add_cycle(1, utc(2026, 11, 8, 0, 0, 3), None, {"lan:192.168.0.1": [1, 2.0, ""]})
    frame = cache.frame(path)
    assert frame["lan:192.168.0.1|ok"].tolist() == [1, 1]
    assert frame["ext:1.1.1.1|ok"].tolist() == [1, -1]


def _write_legacy_day(config: Config, day: str, stamps: list[str]) -> None:
    folder = config.store_dir / CYCLES_DIR
    folder.mkdir(parents=True, exist_ok=True)
    lines = ["﻿session_id,ts_utc,level,results"]
    for stamp in stamps:
        payload = json.dumps({"ext:1.1.1.1": [1, 20.0, ""], "dns": [0, None, "keine Antwort"]},
                             separators=(",", ":")).replace('"', '""')
        lines.append(f'1,{stamp},dns,"{payload}"')
    (folder / f"pruefungen_{day}.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_old_daily_files_are_readable_before_conversion(config: Config) -> None:
    _write_legacy_day(config, "2026-11-08", ["2026-11-08T10:00:00+00:00"])
    reader = Store(config.store_dir, readonly=True)
    assert reader.cycle_times() == ["2026-11-08T10:00:00+00:00"]
    assert len(load_samples(reader, utc(2026, 11, 8), utc(2026, 11, 9))) == 1


def test_old_daily_files_are_converted_on_start(config: Config) -> None:
    _write_legacy_day(config, "2026-11-08", ["2026-11-08T10:00:00+00:00", "2026-11-08T10:00:03+00:00"])
    _write_legacy_day(config, "2026-11-09", ["2026-11-09T10:00:00+00:00"])
    _write_legacy_day(config, "2026-10-31", ["2026-10-31T23:59:57+00:00"])
    store = Store(config.store_dir, durable=False)
    names = sorted(path.name for path in (config.store_dir / CYCLES_DIR).glob("*.csv"))
    assert names == ["pruefungen_2026-10.csv", "pruefungen_2026-11.csv"]
    archive = config.data_dir / "archiv" / "tagesdateien_vor_umstellung"
    assert len(list(archive.glob("*.csv"))) == 3
    assert store.count_cycles() == 4
    assert store.last_cycle_time() == "2026-11-09T10:00:00+00:00"
    row = next(iter(store.cycles(utc(2026, 11, 9), utc(2026, 11, 10))))
    assert row["level"] == "dns"
    assert json.loads(row["results"]) == {"ext:1.1.1.1": [1, 20.0, ""], "dns": [0, None, "keine Antwort"]}
    store.add_cycle(1, utc(2026, 11, 9, 10, 0, 3), None, {"ext:1.1.1.1": [1, 19.0, ""]})
    assert store.count_cycles() == 5


def test_interrupted_conversion_does_not_duplicate_rows(config: Config) -> None:
    _write_legacy_day(config, "2026-11-08", ["2026-11-08T10:00:00+00:00"])
    Store(config.store_dir, durable=False)
    # Als wäre der Dienst abgestürzt, bevor die Tagesdatei ins Archiv kam.
    _write_legacy_day(config, "2026-11-08", ["2026-11-08T10:00:00+00:00"])
    assert Store(config.store_dir, durable=False).count_cycles() == 1
