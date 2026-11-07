from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest
from conftest import EXT_A, results, session, utc

from linkmon.config import Config
from linkmon.quality import (
    INTERNET,
    SampleCache,
    all_track_stats,
    bucket_size,
    bucket_table,
    days_between,
    find_track,
    hour_matrix,
    latest_internet_ms,
    load_samples,
    parse_cycles,
    tracks,
)
from linkmon.store import Store


def _rows(*items: tuple[str, dict]) -> list[dict]:
    return [{"ts_utc": stamp, "results": json.dumps(values)} for stamp, values in items]


def test_tracks_have_fixed_order_and_pool_external_targets() -> None:
    frame = parse_cycles(_rows(("2026-11-01T10:00:00+00:00", results())))
    assert [track.key for track in tracks(frame)] == [INTERNET, "isp:92.79.214.24", "lan:192.168.0.1", "dns", "https"]
    internet = find_track(frame, INTERNET)
    assert internet is not None and internet.label == "Internet (1.1.1.1, 8.8.8.8)"
    assert internet.columns == ("ext:1.1.1.1", "ext:8.8.8.8")
    assert not find_track(frame, "dns").is_ping


def test_loss_counts_every_ping_of_the_pooled_targets() -> None:
    frame = parse_cycles(_rows(
        ("2026-11-01T10:00:00+00:00", results(ext_a=None)),
        ("2026-11-01T10:00:03+00:00", results(ext_a=30.0, ext_b=40.0)),
    ))
    stats = {item.track.key: item for item in all_track_stats(frame)}
    internet = stats[INTERNET]
    assert (internet.sent, internet.lost) == (4, 1)
    assert internet.loss_pct == pytest.approx(25.0)
    assert internet.median_ms == pytest.approx(30.0)  # 22, 30, 40: Verluste zählen nicht als Antwortzeit.
    assert stats["lan:192.168.0.1"].lost == 0


def test_jitter_is_mean_change_between_consecutive_pings_per_target() -> None:
    frame = parse_cycles(_rows(*[(f"2026-11-01T10:00:{s:02d}+00:00", results(ext_a=a, ext_b=20.0))
                                 for s, a in ((0, 10.0), (3, 20.0), (6, 10.0))]))
    internet = next(item for item in all_track_stats(frame) if item.track.key == INTERNET)
    # 1.1.1.1 schwankt um je 10 ms, 8.8.8.8 gar nicht: Mittel aus 10, 10, 0, 0.
    assert internet.jitter_ms == pytest.approx(5.0)


def test_target_that_appears_later_is_not_counted_before() -> None:
    first = {k: v for k, v in results().items() if not k.startswith("isp:")}
    frame = parse_cycles(_rows(("2026-11-01T10:00:00+00:00", first), ("2026-11-01T10:00:03+00:00", results())))
    isp = next(item for item in all_track_stats(frame) if item.track.key.startswith("isp:"))
    assert isp.sent == 1


def test_rounds_during_old_speedtests_count_as_heartbeat_but_not_as_quality() -> None:
    loaded = dict(results(ext_a=900.0), _speedtest=[1, None, ""])
    frame = parse_cycles(_rows(("2026-11-01T10:00:00+00:00", loaded), ("2026-11-01T10:00:03+00:00", results())))
    assert len(frame) == 2
    internet = next(item for item in all_track_stats(frame) if item.track.key == INTERNET)
    assert internet.sent == 2 and internet.max_ms == 22.0


def test_buckets_leave_gaps_empty_so_lines_break() -> None:
    stamps = [utc(2026, 11, 1, 10) + timedelta(seconds=3 * i) for i in range(40)]
    stamps += [utc(2026, 11, 1, 10, 10) + timedelta(seconds=3 * i) for i in range(40)]
    frame = parse_cycles(_rows(*[(stamp.isoformat(), results()) for stamp in stamps]))
    table = bucket_table(frame, find_track(frame, INTERNET), pd.Timedelta(minutes=1))
    assert len(table) == 12
    assert table["median"].isna().sum() == 8  # 10:02 bis 10:09
    assert table["sent"].iloc[0] == 40  # 20 Runden mal zwei Ziele


def test_bucket_size_keeps_point_count_readable() -> None:
    start = utc(2026, 11, 1)
    assert bucket_size(start, start + timedelta(hours=24)) == pd.Timedelta(minutes=1)
    assert bucket_size(start, start + timedelta(days=7)) == pd.Timedelta(minutes=10)
    assert bucket_size(start, start + timedelta(days=30)) == pd.Timedelta(minutes=30)
    assert bucket_size(start, start + timedelta(days=3650)) == pd.Timedelta(days=1)


def test_hour_matrix_shows_evening_pattern(config: Config, seeded: tuple[Store, datetime, datetime]) -> None:
    store, start, end = seeded
    samples = load_samples(store, start, end)
    days = days_between(start, end, config.zone)
    matrix = hour_matrix(samples, find_track(samples, INTERNET), config.zone, "median", days)
    assert matrix.shape == (24, 2)
    assert 60.0 <= matrix.loc[21, "2026-11-02"] <= 62.0
    assert 20.0 <= matrix.loc[10, "2026-11-02"] <= 22.0
    assert pd.isna(matrix.loc[7, "2026-11-03"])  # Messlücke von 06:00 bis 08:00


def test_days_between_includes_both_ends(config: Config) -> None:
    start = datetime(2026, 11, 2, tzinfo=config.zone)
    assert days_between(start, start + timedelta(days=2), config.zone) == ["2026-11-02", "2026-11-03"]


def test_cache_rereads_only_changed_files(config: Config, store: Store) -> None:
    session_id = session(store, utc(2026, 11, 1))
    store.add_cycle(session_id, utc(2026, 11, 1, 10), None, results())
    cache = SampleCache()
    first = load_samples(store, utc(2026, 11, 1), utc(2026, 11, 2), cache)
    assert len(first) == 1
    path = store.cycle_files()[0]
    assert cache.frame(path) is cache.frame(path)
    store.add_cycle(session_id, utc(2026, 11, 1, 10, 0, 3), None, results())
    assert len(load_samples(store, utc(2026, 11, 1), utc(2026, 11, 2), cache)) == 2


def test_samples_are_clipped_to_range(config: Config, store: Store) -> None:
    session_id = session(store, utc(2026, 11, 1))
    for hour in (9, 10, 11):
        store.add_cycle(session_id, utc(2026, 11, 1, hour), None, results())
    assert len(load_samples(store, utc(2026, 11, 1, 10), utc(2026, 11, 1, 11))) == 1


def test_empty_range_gives_empty_frame(tmp_path: Path) -> None:
    empty = Store(tmp_path / "leer")
    samples = load_samples(empty, utc(2026, 11, 1), utc(2026, 11, 2))
    assert samples.empty and tracks(samples) == [] and all_track_stats(samples) == []


def test_latest_internet_ping() -> None:
    assert latest_internet_ms(json.dumps(results(ext_a=20.0, ext_b=30.0))) == 25.0
    assert latest_internet_ms(json.dumps(results(ext_a=None, ext_b=30.0))) == 30.0
    assert latest_internet_ms(json.dumps({EXT_A: [0, None, "x"]})) is None
    assert latest_internet_ms(None) is None
