from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

from conftest import utc

from linkmon.classify import CycleStatus
from linkmon.config import Config
from linkmon.hostinfo import HostInfo
from linkmon.monitor import META_CONNECTION, CycleResult, Monitor
from linkmon.store import Store

HEALTHY = CycleStatus(True, {"192.168.0.1": True}, True, {"1.1.1.1": True, "8.8.8.8": True}, True, True)
LINE_DOWN = CycleStatus(True, {"192.168.0.1": True}, False, {"1.1.1.1": False, "8.8.8.8": False}, False, False)


def host(connection: str = "lan", interface: str = "Ethernet") -> HostInfo:
    return HostInfo("testhost", "TestOS", interface, connection, 1000.0, True)


class Clock:
    def __init__(self) -> None:
        self.now = utc(2026, 11, 1, 12)

    def __call__(self):
        return self.now


def make_monitor(config: Config, store: Store, statuses: list[CycleStatus] | None = None,
                 clock: Clock | None = None, interfaces: list[str] | None = None,
                 host_info=None) -> Monitor:
    queue = list(statuses or [])
    names = list(interfaces or [])

    def probe() -> CycleResult:
        values: dict[str, list[Any]] = {"dns": [1, 3.0, ""]}
        if names:
            values["interface"] = [1, None, names.pop(0)]
        return CycleResult(queue.pop(0) if queue else HEALTHY, values)

    monitor = Monitor(config, store, probe_runner=probe, host_info=host_info or (lambda: host()),
                      clock=clock or Clock())
    monitor._session_id = store.start_session(utc(2026, 11, 1, 12), "h", "o", "p", "v", "c")
    return monitor


def cycle(monitor: Monitor, clock: Clock, seconds: int = 3) -> None:
    monitor._check_gap(clock.now)
    monitor._run_one_cycle(clock.now)
    clock.now += timedelta(seconds=seconds)


def test_line_outage_is_stored_with_exact_times(config: Config, store: Store) -> None:
    clock = Clock()
    monitor = make_monitor(config, store, [HEALTHY, LINE_DOWN, LINE_DOWN, LINE_DOWN, LINE_DOWN, HEALTHY, HEALTHY],
                           clock=clock)
    for _ in range(7):
        cycle(monitor, clock)
    row = store.outages()[0]
    assert row["level"] == "line"
    assert row["start_utc"] == "2026-11-01T12:00:03+00:00"
    assert row["end_utc"] == "2026-11-01T12:00:15+00:00"
    assert row["end_confirmed"] == 1
    assert len(list(store.cycles())) == 7


def test_monitor_gap_closes_outage_as_unconfirmed_and_is_logged(config: Config, store: Store) -> None:
    clock = Clock()
    monitor = make_monitor(config, store, [LINE_DOWN, LINE_DOWN, LINE_DOWN, HEALTHY], clock=clock)
    for _ in range(3):
        cycle(monitor, clock)
    last_beat = clock.now - timedelta(seconds=3)
    clock.now += timedelta(hours=1)  # Rechner schläft oder Tool hängt.
    cycle(monitor, clock)
    row = store.outages()[0]
    assert row["end_confirmed"] == 0
    assert row["end_utc"] == last_beat.isoformat(timespec="seconds")
    assert [e for e in store.events() if e["kind"] == "messluecke"]


def test_gap_while_healthy_creates_no_outage(config: Config, store: Store) -> None:
    clock = Clock()
    monitor = make_monitor(config, store, clock=clock)
    cycle(monitor, clock)
    clock.now += timedelta(hours=1)
    cycle(monitor, clock)
    assert store.outages() == []


def test_outage_stays_open_while_line_is_down(config: Config, store: Store) -> None:
    clock = Clock()
    monitor = make_monitor(config, store, [LINE_DOWN, LINE_DOWN, LINE_DOWN], clock=clock)
    for _ in range(3):
        cycle(monitor, clock)
    assert store.outages()[0]["end_utc"] is None


def test_restart_after_crash_recovers_open_outage(config: Config, store: Store) -> None:
    session = store.start_session(utc(2026, 11, 1, 10), "h", "o", "p", "v", "c")
    store.open_outage(session, "line", "x", utc(2026, 11, 1, 10, 0, 5))
    store.add_cycle(session, utc(2026, 11, 1, 10, 5), "line", {})
    assert store.recover_after_crash(utc(2026, 11, 1, 12)) == 1
    outage = store.outages()[0]
    assert outage["end_utc"] == "2026-11-01T10:05:00+00:00"
    assert outage["end_confirmed"] == 0
    closed = next(s for s in store.sessions() if s["id"] == session)
    assert closed["end_reason"] == "unsauber beendet"
    assert closed["ended_utc"] == "2026-11-01T10:05:00+00:00"


def test_connection_change_is_noticed_without_restart(config: Config, store: Store) -> None:
    clock = Clock()
    infos = iter([host("lan", "Ethernet")])
    monitor = make_monitor(config, store, clock=clock, interfaces=["WLAN", "Ethernet", "Ethernet"],
                           host_info=lambda: next(infos))
    monitor._interface = "WLAN"
    for _ in range(3):
        cycle(monitor, clock)
        if monitor._connection_thread:
            monitor._connection_thread.join(timeout=5)
    events = [e for e in store.events() if e["kind"] == "verbindung"]
    assert len(events) == 1 and "WLAN auf Ethernet (LAN)" in events[0]["message"]
    assert json.loads(store.get_meta(META_CONNECTION))["connection_type"] == "lan"


def test_full_run_records_connection_ends_cleanly_and_survives_a_probe_error(config: Config, store: Store) -> None:
    calls = {"n": 0}
    holder: dict[str, Monitor] = {}

    def probe() -> CycleResult:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("kaputt")
        holder["monitor"].stop()
        return CycleResult(HEALTHY, {})

    monitor = Monitor(config, store, probe_runner=probe, host_info=lambda: host("wlan", "WLAN"))
    holder["monitor"] = monitor
    monitor.run()
    assert calls["n"] == 2
    session = store.sessions()[0]
    assert session["end_reason"] == "regulär beendet"
    assert (session["connection_type"], session["interface"], session["link_mbps"]) == ("wlan", "WLAN", 1000.0)
    assert [e for e in store.events() if e["kind"] == "warnung" and "WLAN" in e["message"]]
    assert json.loads(store.get_meta(META_CONNECTION))["interface"] == "WLAN"
