from __future__ import annotations

import pytest

from linkmon.config import ConfigError, load_config
from linkmon.hostinfo import HostInfo, measurement_warnings, media_from_windows_type
from linkmon.net import parse_linux_route, parse_ping_output, parse_traceroute, parse_windows_route, split_hops
from linkmon.timeutil import parse_user_time

GERMAN_OK = """
Ping wird ausgeführt für 1.1.1.1 mit 32 Bytes Daten:
Antwort von 1.1.1.1: Bytes=32 Zeit=14ms TTL=58

Ping-Statistik für 1.1.1.1:
    Pakete: Gesendet = 1, Empfangen = 1, Verloren = 0
    Minimum = 14ms, Maximum = 14ms, Mittelwert = 14ms
"""
GERMAN_FAST = "Antwort von 192.168.0.1: Bytes=32 Zeit<1ms TTL=64\n"
GERMAN_UNREACHABLE = "Antwort von 192.168.68.138: Zielhost nicht erreichbar.\n"
GERMAN_TIMEOUT = "Zeitüberschreitung der Anforderung.\n"
LINUX_OK = "64 bytes from 1.1.1.1: icmp_seq=1 ttl=58 time=13.4 ms\n"
TRACERT = """
Routenverfolgung zu 1.1.1.1 über maximal 5 Hops

  1     6 ms     4 ms     2 ms  192.168.68.1
  2     3 ms     7 ms     5 ms  192.168.0.1
  3     *        *        *     Zeitüberschreitung der Anforderung.
  4    53 ms    37 ms    44 ms  92.79.214.24
  5    15 ms    15 ms    29 ms  145.254.2.179

Ablaufverfolgung beendet.
"""
TRACEROUTE_LINUX = """traceroute to 1.1.1.1 (1.1.1.1), 8 hops max, 60 byte packets
 1  192.168.0.1  0.4 ms
 2  *
 3  92.79.214.24  12.2 ms
"""
ROUTE_PRINT = """
IPv4-Routentabelle
===========================================================================
Aktive Routen:
     Netzwerkziel    Netzwerkmaske          Gateway    Schnittstelle Metrik
          0.0.0.0          0.0.0.0     192.168.68.1   192.168.68.138     35
          0.0.0.0          0.0.0.0      192.168.0.1     192.168.0.20     25
===========================================================================
Ständige Routen:
  Keine
"""


@pytest.mark.parametrize(
    ("output", "ok", "latency"),
    [(GERMAN_OK, True, 14.0), (GERMAN_FAST, True, 1.0), (LINUX_OK, True, 13.4),
     (GERMAN_UNREACHABLE, False, None), (GERMAN_TIMEOUT, False, None), ("", False, None)],
)
def test_ping_output(output: str, ok: bool, latency: float | None) -> None:
    result = parse_ping_output(output)
    assert (result.ok, result.latency_ms) == (ok, latency)


def test_traceroute_keeps_silent_hops() -> None:
    assert parse_traceroute(TRACERT) == ["192.168.68.1", "192.168.0.1", None, "92.79.214.24", "145.254.2.179"]
    assert parse_traceroute(TRACEROUTE_LINUX) == ["192.168.0.1", None, "92.79.214.24"]


def test_split_hops_finds_two_routers_and_first_public_hop() -> None:
    lan, public = split_hops(parse_traceroute(TRACERT))
    assert lan == ["192.168.68.1", "192.168.0.1"]
    assert public[0] == "92.79.214.24"


def test_private_hop_behind_public_hop_is_not_lan() -> None:
    lan, _ = split_hops(["192.168.0.1", "92.79.214.24", "10.0.0.1"])
    assert lan == ["192.168.0.1"]


def test_carrier_nat_hop_is_neither_lan_nor_public() -> None:
    lan, public = split_hops(["192.168.0.1", "100.64.0.1", "92.79.214.24"])
    assert lan == ["192.168.0.1"]
    assert public == ["92.79.214.24"]


def test_route_parsers() -> None:
    assert parse_windows_route(ROUTE_PRINT) == "192.168.0.1"
    assert parse_windows_route("Keine\n") is None
    assert parse_linux_route("default via 192.168.0.1 dev eth0 proto dhcp\n") == "192.168.0.1"
    assert parse_linux_route("") is None


def test_media_types() -> None:
    assert media_from_windows_type("Native 802.11\r\n") == "wlan"
    assert media_from_windows_type("802.3") == "lan"
    assert media_from_windows_type("") == "unbekannt"


def host(connection: str, link: float | None) -> HostInfo:
    return HostInfo("h", "o", "i", connection, link, True)


def test_wlan_warning() -> None:
    assert "WLAN" in measurement_warnings(host("wlan", 585))[0]
    assert "nicht erkannt" in measurement_warnings(host("unbekannt", None))[0]
    assert measurement_warnings(host("lan", 100)) == []


def test_https_check_measures_time(monkeypatch: pytest.MonkeyPatch) -> None:
    from linkmon import net

    class Answer:
        def __enter__(self) -> Answer:
            return self

        def __exit__(self, *_: object) -> bool:
            return False

    monkeypatch.setattr(net.urllib.request, "urlopen", lambda *_args, **_kwargs: Answer())
    result = net.check_https(["https://example.invalid/"], 3.0)
    assert result.ok and result.latency_ms is not None and result.latency_ms >= 0


def test_user_time_end_of_day(config) -> None:
    zone = config.zone
    assert parse_user_time("2026-11-30", zone, end_of_day=True).isoformat() == "2026-12-01T00:00:00+01:00"
    assert parse_user_time("2026-11-30", zone).isoformat() == "2026-11-30T00:00:00+01:00"
    assert parse_user_time("2026-11-30T08:30", zone, end_of_day=True).hour == 8


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("[reachability]\nintervall = 3\n", "Unbekannte Einstellung"),
        ("[quatsch]\nx = 1\n", "Unbekannter Abschnitt"),
        ("[reachability]\ninterval_s = 0\n", "interval_s"),
        ('[reachability]\nexternal_targets = ["1.1.1.1"]\n', "mindestens zwei"),
        ("[speedtest]\nprimary_server_id = 5\n", "gibt es nicht mehr"),
        ("[contract]\ndownload_max = 250.0\n", "gibt es nicht mehr"),
        ('[general]\ntimezone = "Mars/Olympus"\n', "Zeitzone"),
    ],
)
def test_invalid_config_is_rejected(tmp_path, content: str, message: str) -> None:
    path = tmp_path / "config.toml"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ConfigError, match=message):
        load_config(path)


def test_example_config_is_valid() -> None:
    from pathlib import Path

    config = load_config(Path(__file__).parent.parent / "config.example.toml")
    assert config.reachability.interval_s == 3
    assert config.report.tariff == ""
