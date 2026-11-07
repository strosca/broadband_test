from __future__ import annotations

from datetime import timedelta

import pytest
from conftest import utc

from linkmon.classify import (
    Classification,
    CycleStatus,
    OutageClosed,
    OutageOpened,
    OutageTracker,
    classify,
)
from linkmon.levels import Level

T0 = utc(2026, 11, 1, 12)


def status(**changes: object) -> CycleStatus:
    values: dict[str, object] = {
        "interface_up": True,
        "lan": {"192.168.0.1": True},
        "isp_hop": True,
        "external": {"1.1.1.1": True, "8.8.8.8": True},
        "dns": True,
        "https": True,
    }
    values.update(changes)
    return CycleStatus(**values)  # type: ignore[arg-type]


DOWN = {"1.1.1.1": False, "8.8.8.8": False}


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({}, None),
        ({"interface_up": False, "lan": {"192.168.0.1": False}, "external": DOWN, "dns": False, "https": False}, Level.LOCAL),
        ({"lan": {"192.168.0.1": False}, "external": DOWN, "dns": False, "https": False}, Level.ROUTER),
        ({"lan": {"192.168.68.1": True, "192.168.0.1": False}, "external": DOWN}, Level.ROUTER),
        ({"external": DOWN, "isp_hop": False, "dns": False, "https": False}, Level.LINE),
        ({"external": DOWN, "isp_hop": True, "dns": False, "https": False}, Level.LINE),
        ({"external": DOWN, "isp_hop": None, "dns": False, "https": False}, Level.LINE),
        ({"dns": False, "https": False}, Level.DNS),
        ({"dns": False}, Level.DNS),
        ({"https": False}, Level.HTTPS),
    ],
)
def test_classify_picks_innermost_level(changes: dict[str, object], expected: Level | None) -> None:
    assert classify(status(**changes)).level == expected


def test_one_external_target_down_is_not_an_outage() -> None:
    assert classify(status(external={"1.1.1.1": False, "8.8.8.8": True})).level is None


def test_isp_hop_silent_alone_is_not_an_outage() -> None:
    assert classify(status(isp_hop=False)).level is None


def test_line_detail_says_whether_hop_answered() -> None:
    assert "erreichbar, externe" in classify(status(external=DOWN, isp_hop=True)).detail
    assert "Hop und externe" in classify(status(external=DOWN, isp_hop=False)).detail


FAULT = Classification(Level.LINE, "Leitung")
OK = Classification(None)


def feed(tracker: OutageTracker, sequence: list[Classification], start: int = 0) -> list[object]:
    events: list[object] = []
    for index, result in enumerate(sequence, start):
        events += tracker.feed(T0 + timedelta(seconds=3 * index), result)
    return events


def test_short_fault_below_threshold_opens_nothing() -> None:
    tracker = OutageTracker(3, 2)
    assert feed(tracker, [FAULT, FAULT, OK, FAULT, FAULT, OK]) == []
    assert tracker.current is None


def test_outage_starts_at_first_failure_not_at_threshold() -> None:
    tracker = OutageTracker(3, 2)
    events = feed(tracker, [OK, FAULT, FAULT, FAULT])
    assert events == [OutageOpened(Level.LINE, "Leitung", T0 + timedelta(seconds=3))]


def test_outage_ends_at_first_ok_after_recovery_threshold() -> None:
    tracker = OutageTracker(3, 2)
    events = feed(tracker, [FAULT, FAULT, FAULT, FAULT, OK, OK])
    assert events[-1] == OutageClosed(Level.LINE, T0 + timedelta(seconds=12), confirmed=True)
    assert tracker.current is None


def test_single_ok_inside_outage_does_not_split_it() -> None:
    tracker = OutageTracker(3, 2)
    events = feed(tracker, [FAULT, FAULT, FAULT, OK, FAULT, FAULT, OK, OK])
    assert len([e for e in events if isinstance(e, OutageOpened)]) == 1
    closed = [e for e in events if isinstance(e, OutageClosed)]
    assert len(closed) == 1
    assert closed[0].end == T0 + timedelta(seconds=18)


def test_threshold_one_opens_immediately() -> None:
    tracker = OutageTracker(1, 1)
    assert isinstance(feed(tracker, [FAULT])[0], OutageOpened)


def test_level_change_needs_stable_cycles_and_splits_outage() -> None:
    tracker = OutageTracker(3, 2)
    router = Classification(Level.ROUTER, "Router")
    events = feed(tracker, [FAULT, FAULT, FAULT, router, router, router])
    assert isinstance(events[0], OutageOpened)
    assert events[1] == OutageClosed(Level.LINE, T0 + timedelta(seconds=9), confirmed=True)
    assert events[2] == OutageOpened(Level.ROUTER, "Router", T0 + timedelta(seconds=9))


def test_single_cycle_of_other_level_keeps_current_outage() -> None:
    tracker = OutageTracker(3, 2)
    router = Classification(Level.ROUTER, "Router")
    events = feed(tracker, [FAULT, FAULT, FAULT, router, FAULT, FAULT])
    assert len(events) == 1
    assert tracker.current == Level.LINE


def test_flapping_levels_still_open_an_outage() -> None:
    tracker = OutageTracker(3, 2)
    dns = Classification(Level.DNS, "DNS")
    events = feed(tracker, [FAULT, dns, FAULT])
    assert events == [OutageOpened(Level.LINE, "Leitung", T0)]


def test_interrupt_closes_as_unconfirmed() -> None:
    tracker = OutageTracker(3, 2)
    feed(tracker, [FAULT, FAULT, FAULT])
    last = T0 + timedelta(seconds=6)
    assert tracker.interrupt(last) == [OutageClosed(Level.LINE, last, confirmed=False)]
    assert tracker.current is None
    assert tracker.interrupt(last) == []


def test_interrupt_discards_pending_faults() -> None:
    tracker = OutageTracker(3, 2)
    feed(tracker, [FAULT, FAULT])
    assert tracker.interrupt(T0) == []
    assert feed(tracker, [FAULT]) == []
