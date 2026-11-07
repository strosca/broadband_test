from __future__ import annotations

import json
import socket
import urllib.request
from datetime import timedelta
from pathlib import Path

from conftest import results, session, utc

from linkmon.config import load_config
from linkmon.dashboard import (
    age_text,
    build_overview,
    condition_tone,
    connection_text,
    current_condition,
    load_view,
    start_in_background,
)
from linkmon.monitor import META_CONNECTION
from linkmon.store import Store


def test_condition_is_never_color_alone() -> None:
    assert condition_tone("Alles in Ordnung") == ("tone-good", "✓")
    assert condition_tone("Ausfall: Leitung (Vodafone)") == ("tone-critical", "✕")
    assert condition_tone("Prüfung gestört") == ("tone-warning", "!")
    assert condition_tone("Unbekannt") == ("tone-unknown", "?")


def test_age_text_uses_readable_units() -> None:
    assert age_text(3) == "vor 3 s"
    assert age_text(600) == "vor 10 min"
    assert age_text(7200) == "vor 2 h"
    assert age_text(172800) == "vor 2 d"
    assert age_text(-5) == "vor 0 s"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _config(tmp_path: Path, port: int):
    path = tmp_path / "config.toml"
    path.write_text(f"[dashboard]\nport = {port}\n", encoding="utf-8")
    return load_config(path)


def test_web_interface_serves_page_in_background(tmp_path: Path) -> None:
    port = _free_port()
    url = start_in_background(_config(tmp_path, port))
    assert url == f"http://127.0.0.1:{port}"
    with urllib.request.urlopen(url, timeout=10) as response:
        assert response.status == 200


def test_busy_port_does_not_stop_the_process(tmp_path: Path) -> None:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen()
        assert start_in_background(_config(tmp_path, sock.getsockname()[1])) is None


def test_condition_all_good() -> None:
    values = json.dumps({"lan:192.168.0.1": [1, 3.0, ""], "dns": [1, 5.0, ""]})
    assert current_condition(values, []) == ("Alles in Ordnung", "2 von 2 Prüfungen in Ordnung")


def test_condition_names_open_outage_and_failed_checks() -> None:
    values = json.dumps({"lan:192.168.0.1": [1, 3.0, ""], "ext:1.1.1.1": [0, None, "x"], "ext:8.8.8.8": [0, None, "x"]})
    value, note = current_condition(values, ["line"])
    assert value == "Ausfall: Leitung (Vodafone)"
    assert note == "1 von 3 Prüfungen in Ordnung. Gestört: Internet"


def test_single_failed_check_is_not_called_an_outage() -> None:
    value, _ = current_condition(json.dumps({"ext:1.1.1.1": [0, None, "x"], "ext:8.8.8.8": [1, 9.0, ""]}), [])
    assert value == "Prüfung gestört"


def test_old_speedtest_marker_is_ignored() -> None:
    values = json.dumps({"ext:1.1.1.1": [1, 9.0, ""], "_speedtest": [1, None, ""]})
    assert current_condition(values, []) == ("Alles in Ordnung", "1 von 1 Prüfungen in Ordnung")


def test_condition_without_data() -> None:
    assert current_condition(None, [])[0] == "Unbekannt"


def test_connection_text() -> None:
    assert connection_text(None) == ("–", False)
    info = json.dumps({"connection_type": "wlan", "link_mbps": 585.0})
    assert connection_text(info) == ("WLAN, 585 Mbit/s", True)
    assert connection_text(json.dumps({"connection_type": "lan", "link_mbps": None})) == ("LAN", False)


def test_time_before_the_first_check_is_not_a_gap(config, seeded) -> None:
    store, start, end = seeded
    earlier = start - timedelta(days=3)
    view = load_view(config, store, earlier, end, end, cache=None)
    assert view["gaps"]  # Die eingebaute Lücke von zwei Stunden bleibt sichtbar.
    assert all(begin >= start for begin, _ in view["gaps"])


def test_overview_shows_live_state_ping_and_wlan_hint(config, store: Store) -> None:
    now = utc(2026, 11, 1, 12)
    session_id = session(store, now - timedelta(minutes=5))
    for second in range(0, 60, 3):
        store.add_cycle(session_id, now - timedelta(seconds=60 - second), None, results(ext_a=30.0, ext_b=40.0))
    store.set_meta(META_CONNECTION, json.dumps({"connection_type": "wlan", "link_mbps": 585.0}))
    reader = Store(config.store_dir, readonly=True)
    view = load_view(config, reader, now - timedelta(hours=1), now, now, cache=None)
    overview = build_overview(reader, view["samples"], view["stats"], view["outages"], view["coverage"], config, now)
    text = str(overview)
    assert "Alles in Ordnung" in text
    assert "35 ms" in text  # Mittel der beiden externen Ziele in der letzten Runde
    assert "WLAN, 585 Mbit/s" in text and "per Kabel" in text
    assert "Noch keiner" in text
