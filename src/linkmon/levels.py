"""Ebenen, denen ein Ausfall zugeordnet wird."""

from __future__ import annotations

from enum import StrEnum


class Level(StrEnum):
    LOCAL = "local"
    ROUTER = "router"
    LINE = "line"
    DNS_HOME = "dns_home"
    DNS = "dns"
    HTTPS = "https"


LEVEL_LABELS: dict[str, str] = {
    Level.LOCAL: "Lokal (dieses Gerät)",
    Level.ROUTER: "Router/LAN",
    Level.LINE: "Leitung (Vodafone)",
    Level.DNS_HOME: "DNS Heimnetz (Pi-hole)",
    Level.DNS: "Nur DNS",
    Level.HTTPS: "Nur HTTPS",
}

# Feste Reihenfolge in Plots und Tabellen.
LEVEL_ORDER: list[str] = [Level.LINE, Level.ROUTER, Level.LOCAL, Level.DNS_HOME, Level.DNS, Level.HTTPS]

LEVEL_COLORS: dict[str, str] = {
    Level.LINE: "#e34948",
    Level.ROUTER: "#eda100",
    Level.LOCAL: "#6250d6",
    Level.DNS_HOME: "#2a8fbd",
    Level.DNS: "#e87ba4",
    Level.HTTPS: "#008300",
}
