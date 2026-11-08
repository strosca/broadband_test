from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import utc

from linkmon import clearing, net
from linkmon.classify import CycleStatus, classify
from linkmon.cli import main
from linkmon.config import Config
from linkmon.dashboard import listen_urls
from linkmon.levels import Level
from linkmon.monitor import META_ISP_HOP
from linkmon.store import Store
from test_monitor import HEALTHY, Clock, cycle, make_monitor

PIHOLE = "192.168.68.168"


def status(**changes: object) -> CycleStatus:
    values: dict[str, object] = {
        "interface_up": True, "lan": {"192.168.68.1": True}, "isp_hop": True,
        "external": {"1.1.1.1": True, "8.8.8.8": True}, "dns": True, "https": True,
        "dns_home": {PIHOLE: True}, "dns_reference": True,
    }
    values.update(changes)
    return CycleStatus(**values)  # type: ignore[arg-type]


# Pi-hole ------------------------------------------------------------------------------------------

def test_all_dns_fine_is_no_fault() -> None:
    assert classify(status()).level is None


def test_pihole_down_is_a_home_network_fault_not_the_provider() -> None:
    result = classify(status(dns=False, https=False, dns_home={PIHOLE: False}))
    assert result.level == Level.DNS_HOME
    assert PIHOLE in result.detail


def test_pihole_down_is_noticed_even_when_the_fallback_server_answers() -> None:
    # Windows fällt auf den zweiten DNS-Server zurück: Das Gerät merkt nichts, andere Geräte schon.
    result = classify(status(dns_home={PIHOLE: False}))
    assert result.level == Level.DNS_HOME
    assert "Ausweichserver" in result.detail


def test_dns_down_everywhere_is_a_real_dns_fault() -> None:
    result = classify(status(dns=False, dns_home={PIHOLE: False}, dns_reference=False))
    assert result.level == Level.DNS


def test_provider_dns_without_pihole() -> None:
    result = classify(status(dns=False, dns_home={}, dns_reference=True))
    assert result.level == Level.DNS
    assert "Anbieter" in result.detail


def test_without_comparison_behaves_like_before() -> None:
    assert classify(status(dns=False, dns_home={}, dns_reference=None)).level == Level.DNS


def test_line_outage_still_wins_over_dns() -> None:
    result = classify(status(external={"1.1.1.1": False, "8.8.8.8": False}, dns=False, dns_home={PIHOLE: False}))
    assert result.level == Level.LINE


@pytest.mark.parametrize(("address", "home"), [
    (PIHOLE, True), ("10.0.0.53", True), ("1.1.1.1", False), ("127.0.0.53", False), ("fe80::1", False),
    ("kein-ip", False),
])
def test_home_dns_server_detection(address: str, home: bool) -> None:
    assert net.is_home_dns_server(address) is home


def test_blocked_answer_counts_as_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    class Answer(list):
        response = SimpleNamespace(time=0.001)

    class FakeResolver:
        def __init__(self, configure: bool = True) -> None:
            self.nameservers: list[str] = []

        def resolve(self, *_args, **_kwargs) -> Answer:
            return Answer([SimpleNamespace(to_text=lambda: "0.0.0.0")])

    monkeypatch.setattr(net.dns.resolver, "Resolver", FakeResolver)
    result = net.check_dns(["ads.example"], 1.0, [PIHOLE])
    assert not result.ok
    assert "gesperrt" in result.detail


# Leeren -------------------------------------------------------------------------------------------

def test_reset_archives_data_and_keeps_route(config: Config, store: Store) -> None:
    clock = Clock()
    monitor = make_monitor(config, store, clock=clock)
    store.set_meta(META_ISP_HOP, "92.79.214.24")
    for _ in range(3):
        cycle(monitor, clock)
    archive = config.data_dir / "archiv" / "test"
    store.reset(archive)
    assert (archive / "pruefungen").is_dir()
    assert store.count_cycles() == 0
    assert store.sessions() == []
    assert store.get_meta(META_ISP_HOP) == "92.79.214.24"
    store.add_cycle(1, utc(2026, 11, 2), None, {"dns": [1, 2.0, ""]})
    assert store.count_cycles() == 1


def test_running_monitor_handles_clear_request(config: Config, store: Store) -> None:
    clock = Clock()
    monitor = make_monitor(config, store, [HEALTHY] * 10, clock=clock)
    for _ in range(3):
        cycle(monitor, clock)
    clearing.write_request(config.data_dir, None)
    monitor._handle_clear_request()
    result = clearing.wait_for_result(config.data_dir, 1)
    assert result is not None and result.ok
    assert store.count_cycles() == 0
    assert not clearing.request_path(config.data_dir).exists()
    assert len(store.sessions()) == 1  # Neue Sitzung, die Messung läuft weiter.
    cycle(monitor, clock)
    assert store.count_cycles() == 1
    assert not list(config.store_dir.parent.glob(".messdaten-geloescht-*"))


def test_unreadable_request_archives_instead_of_deleting(config: Config) -> None:
    config.data_dir.mkdir(parents=True, exist_ok=True)
    clearing.request_path(config.data_dir).write_text("kaputt", encoding="utf-8")
    found, archive = clearing.take_request(config.data_dir)
    assert found and archive is not None


def test_cli_clear_without_running_service(config: Config, store: Store, capsys) -> None:
    store.add_cycle(1, utc(2026, 11, 2), None, {"dns": [1, 2.0, ""]})
    assert main(["--config", str(config.path), "clear", "--yes"]) == 0
    assert not Store.exists(config.store_dir)
    archived = list((config.data_dir / "archiv").iterdir())
    assert len(archived) == 1 and (archived[0] / "pruefungen").is_dir()
    assert "archiviert" in capsys.readouterr().out


def test_cli_clear_asks_first(config: Config, store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    store.add_cycle(1, utc(2026, 11, 2), None, {"dns": [1, 2.0, ""]})
    monkeypatch.setattr("builtins.input", lambda _prompt: "n")
    assert main(["--config", str(config.path), "clear", "--delete"]) == 1
    assert Store.exists(config.store_dir)


# Webserver ----------------------------------------------------------------------------------------

def test_listen_urls_lists_network_addresses() -> None:
    assert listen_urls("127.0.0.1", 8050) == ["http://127.0.0.1:8050"]
    urls = listen_urls("0.0.0.0", 8050)
    assert urls and all(url.endswith(":8050") for url in urls)
    assert not any("127.0.0.1" in url for url in urls)


def test_web_server_listens_on_all_interfaces(tmp_path: Path) -> None:
    import socket
    import urllib.request

    from linkmon.config import load_config
    from linkmon.dashboard import start_in_background

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = int(sock.getsockname()[1])
    path = tmp_path / "config.toml"
    path.write_text(f'[dashboard]\nhost = "0.0.0.0"\nport = {port}\n', encoding="utf-8")
    assert start_in_background(load_config(path)) == f"http://0.0.0.0:{port}"
    with urllib.request.urlopen(f"http://127.0.0.1:{port}", timeout=10) as response:
        assert response.status == 200
        assert "waitress" in response.headers.get("Server", "") or response.headers.get("Server") == "linkmon"


# Netzwechsel: Deco -> direkt an der Vodafone Station ---------------------------------------------

DECO = "192.168.68.1"
STATION = "192.168.0.1"


def _fake_network(monkeypatch: pytest.MonkeyPatch, gateway: str, reachable: set[str]) -> None:
    from linkmon import hostinfo
    from linkmon.net import ProbeResult

    monkeypatch.setattr(hostinfo, "active_interface", lambda: "Ethernet")
    monkeypatch.setattr(hostinfo, "interface_is_up", lambda _name: True)
    monkeypatch.setattr(net, "default_gateway", lambda: gateway)
    monkeypatch.setattr(net, "ping", lambda host, _t: ProbeResult(host in reachable, 2.0 if host in reachable else None))
    monkeypatch.setattr(net, "check_dns", lambda *_a, **_k: ProbeResult(True, 5.0))
    monkeypatch.setattr(net, "check_https", lambda *_a, **_k: ProbeResult(True, 50.0))


def test_old_route_is_dropped_when_plugged_into_the_modem(config: Config, store: Store,
                                                          monkeypatch: pytest.MonkeyPatch) -> None:
    import json

    from linkmon.monitor import META_LAN_HOPS, Prober

    store.set_meta(META_LAN_HOPS, json.dumps([DECO, STATION]))
    _fake_network(monkeypatch, STATION, {STATION, "1.1.1.1", "8.8.8.8", "92.79.214.24"})
    prober = Prober(config, store)
    result = prober.run_cycle()
    assert result.status.lan == {STATION: True}  # Der Deco liegt nicht mehr auf dem Weg.
    assert classify(result.status).level is None
    assert prober.route_stale

    monkeypatch.setattr(net, "traceroute", lambda _target: [STATION, None, "92.79.214.24"])
    prober.refresh_route()
    assert not prober.route_stale
    assert json.loads(store.get_meta(META_LAN_HOPS)) == [STATION]


def test_same_network_keeps_both_routers(config: Config, store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    import json

    from linkmon.monitor import META_LAN_HOPS, Prober

    store.set_meta(META_LAN_HOPS, json.dumps([DECO, STATION]))
    _fake_network(monkeypatch, DECO, {DECO, STATION, "1.1.1.1", "8.8.8.8"})
    prober = Prober(config, store)
    assert prober.run_cycle().status.lan == {DECO: True, STATION: True}
    assert not prober.route_stale


def test_stale_route_is_refreshed_even_during_a_fault(config: Config, store: Store) -> None:
    from linkmon.monitor import Monitor

    calls: list[int] = []
    monitor = Monitor(config, store, probe_runner=lambda: None, route_refresher=lambda: calls.append(1) or True,
                      route_stale=lambda: True, host_info=lambda: None, clock=Clock())
    monitor._maybe_refresh_route(utc(2026, 11, 1, 12), healthy=False)
    monitor._route_thread.join(5)
    assert calls == [1]
