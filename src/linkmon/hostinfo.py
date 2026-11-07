"""Metadaten zum Messgerät: Interface, Verbindungsart, Link-Geschwindigkeit, Betriebssystem."""

from __future__ import annotations

import platform
import socket
import subprocess
from dataclasses import dataclass
from pathlib import Path

import psutil

from linkmon.net import IS_WINDOWS, hidden_window_kwargs


@dataclass(frozen=True)
class HostInfo:
    hostname: str
    os_name: str
    interface: str | None
    connection_type: str  # "lan", "wlan" oder "unbekannt"
    link_mbps: float | None
    interface_up: bool


def _route_source_address(probe_target: str = "1.1.1.1") -> str | None:
    """Fragt das System, welche lokale Adresse die Route nach außen nutzt. Sendet keine Daten."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect((probe_target, 53))
            return str(sock.getsockname()[0])
    except OSError:
        return None


def active_interface() -> str | None:
    address = _route_source_address()
    if address is None:
        return None
    for name, entries in psutil.net_if_addrs().items():
        if any(entry.address == address for entry in entries):
            return name
    return None


def interface_is_up(name: str | None) -> bool:
    if name is None:
        return False
    stats = psutil.net_if_stats().get(name)
    return bool(stats and stats.isup)


def media_from_windows_type(physical_media_type: str) -> str:
    text = physical_media_type.lower()
    if "802.11" in text or "wireless" in text:
        return "wlan"
    if "802.3" in text:
        return "lan"
    return "unbekannt"


def _windows_connection_type(interface: str) -> str:
    name = interface.replace("'", "''")
    command = ["powershell", "-NoProfile", "-NonInteractive", "-Command",
               f"(Get-NetAdapter -Name '{name}').PhysicalMediaType"]
    try:
        done = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=20, **hidden_window_kwargs())
    except (OSError, subprocess.SubprocessError):
        return "unbekannt"
    return media_from_windows_type(done.stdout)


def _linux_connection_type(interface: str) -> str:
    device = Path("/sys/class/net") / interface
    if (device / "wireless").exists():
        return "wlan"
    return "lan" if (device / "device").exists() else "unbekannt"


def connection_type(interface: str | None) -> str:
    if interface is None:
        return "unbekannt"
    return _windows_connection_type(interface) if IS_WINDOWS else _linux_connection_type(interface)


def collect_host_info() -> HostInfo:
    interface = active_interface()
    stats = psutil.net_if_stats().get(interface) if interface else None
    link = float(stats.speed) if stats and stats.speed else None
    return HostInfo(
        hostname=socket.gethostname(),
        os_name=platform.platform(),
        interface=interface,
        connection_type=connection_type(interface),
        link_mbps=link,
        interface_up=bool(stats and stats.isup),
    )


def measurement_warnings(info: HostInfo) -> list[str]:
    """Umstände, die eine Messung angreifbar machen."""
    if info.connection_type == "wlan":
        return ["Messung über WLAN: Aussetzer des WLANs erscheinen als Störung im Heimnetz, Funkstörungen erhöhen "
                "Ping und Paketverlust. Für einen Nachweis per Kabel an den Router anschließen."]
    if info.connection_type == "unbekannt":
        return ["Verbindungsart nicht erkannt"]
    return []
