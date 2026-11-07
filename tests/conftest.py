from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from linkmon.config import Config, load_config
from linkmon.store import Store

BASE_TOML = """
[reachability]
interval_s = 3
failure_threshold = 3
recovery_threshold = 2

[report]
tariff = "Test 250"
"""

LAN = "lan:192.168.0.1"
ISP = "isp:92.79.214.24"
EXT_A = "ext:1.1.1.1"
EXT_B = "ext:8.8.8.8"


@pytest.fixture
def config(tmp_path: Path) -> Config:
    path = tmp_path / "config.toml"
    path.write_text(BASE_TOML, encoding="utf-8")
    return load_config(path)


@pytest.fixture
def store(config: Config) -> Iterator[Store]:
    # Ohne fsync, sonst dauern die Tests mit Tausenden Prüfrunden sehr lange.
    item = Store(config.store_dir, durable=False)
    yield item
    item.close()


def results(*, lan: float | None = 3.0, isp: float | None = 12.0, ext_a: float | None = 20.0,
            ext_b: float | None = 22.0, dns: float | None = 15.0, https: float | None = 80.0) -> dict[str, list[Any]]:
    """Ergebnis einer Prüfrunde. None heißt: keine Antwort."""
    def item(value: float | None) -> list[Any]:
        return [1, value, ""] if value is not None else [0, None, "keine Antwort"]

    return {LAN: item(lan), ISP: item(isp), EXT_A: item(ext_a), EXT_B: item(ext_b), "dns": item(dns),
            "https": item(https), "interface": [1, None, "Ethernet"]}


def session(store: Store, moment: datetime, connection: str = "lan") -> int:
    return store.start_session(moment, "testhost", "TestOS", "3.14", "2.0.0", "abc", interface="Ethernet",
                               connection_type=connection, link_mbps=1000.0)


@pytest.fixture
def seeded(config: Config, store: Store) -> tuple[Store, datetime, datetime]:
    """Zwei Tage: Prüfrunden alle 30 s (außer einer Lücke), abends höherer Ping, einzelne Verluste, Ausfälle."""
    start = datetime(2026, 11, 2, 0, 0, tzinfo=config.zone)
    end = datetime(2026, 11, 4, 0, 0, tzinfo=config.zone)
    session_id = session(store, start)

    moment = start
    gap_start = start + timedelta(hours=30)
    gap_end = gap_start + timedelta(hours=2)
    index = 0
    while moment < end:
        if not gap_start <= moment < gap_end:
            evening = moment.astimezone(config.zone).hour >= 20
            ext_a = None if index % 100 == 0 else (60.0 if evening else 20.0)
            store.add_cycle(session_id, moment, None, results(ext_a=ext_a, ext_b=62.0 if evening else 22.0))
        moment += timedelta(seconds=30)
        index += 1

    line_start = start + timedelta(hours=9)
    store.open_outage(session_id, "line", "Erster Provider-Hop und externe Ziele nicht erreichbar", line_start)
    store.close_outage("line", line_start + timedelta(minutes=30), True)
    router_start = start + timedelta(hours=20)
    store.open_outage(session_id, "router", "Nicht erreichbar: 192.168.0.1", router_start)
    store.close_outage("router", router_start + timedelta(minutes=5), True)
    dns_start = start + timedelta(hours=40)
    store.open_outage(session_id, "dns", "DNS-Auflösung fehlgeschlagen, externe Ziele erreichbar", dns_start)
    store.close_outage("dns", dns_start + timedelta(minutes=2), False)
    store.end_session(session_id, end, "regulär beendet")
    return store, start, end


def utc(year: int, month: int, day: int, hour: int = 0, minute: int = 0, second: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, second, tzinfo=UTC)
