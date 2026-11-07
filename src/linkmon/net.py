"""Netzprüfungen: Ping, Route, Traceroute, DNS und HTTPS auf Windows und Linux."""

from __future__ import annotations

import ipaddress
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

import dns.exception
import dns.resolver

from linkmon import __version__

IS_WINDOWS = sys.platform == "win32"
_IPV4 = r"\d{1,3}(?:\.\d{1,3}){3}"


def hidden_window_kwargs() -> dict[str, Any]:
    """Verhindert aufblitzende Konsolenfenster, wenn der Dienst Programme startet."""
    return {"creationflags": subprocess.CREATE_NO_WINDOW} if IS_WINDOWS else {}


@dataclass(frozen=True)
class ProbeResult:
    ok: bool
    latency_ms: float | None = None
    detail: str = ""


def _run(command: list[str], timeout: float) -> str | None:
    try:
        done = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=timeout, **hidden_window_kwargs())
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout


def parse_ping_output(output: str) -> ProbeResult:
    """Wertet die Ping-Ausgabe sprachunabhängig aus.

    Der Exit-Code genügt nicht: Windows liefert 0 auch bei "Zielhost nicht erreichbar".
    Nur eine echte Antwort enthält "TTL=".
    """
    if "ttl=" not in output.lower():
        return ProbeResult(False, detail="keine Antwort")
    match = re.search(r"[=<]\s*([\d.,]+)\s*ms", output)
    latency = float(match.group(1).replace(",", ".")) if match else None
    return ProbeResult(True, latency)


def ping(host: str, timeout_s: float) -> ProbeResult:
    if IS_WINDOWS:
        command = ["ping", "-n", "1", "-w", str(int(timeout_s * 1000)), host]
    else:
        command = ["ping", "-n", "-c", "1", "-W", str(max(1, round(timeout_s))), host]
    output = _run(command, timeout_s + 2)
    if output is None:
        return ProbeResult(False, detail="ping nicht ausführbar")
    return parse_ping_output(output)


def parse_windows_route(output: str) -> str | None:
    """Sucht in `route print` das Standardgateway mit der kleinsten Metrik."""
    best: tuple[int, str] | None = None
    for match in re.finditer(rf"^\s*0\.0\.0\.0\s+0\.0\.0\.0\s+({_IPV4})\s+{_IPV4}\s+(\d+)\s*$", output, re.MULTILINE):
        candidate = (int(match.group(2)), match.group(1))
        if best is None or candidate < best:
            best = candidate
    return best[1] if best else None


def parse_linux_route(output: str) -> str | None:
    match = re.search(rf"^default via ({_IPV4})", output, re.MULTILINE)
    return match.group(1) if match else None


def default_gateway() -> str | None:
    if IS_WINDOWS:
        output = _run(["route", "print", "-4", "0.0.0.0"], 5)
        return parse_windows_route(output) if output else None
    output = _run(["ip", "-4", "route", "show", "default"], 5)
    return parse_linux_route(output) if output else None


def parse_traceroute(output: str) -> list[str | None]:
    """Liefert je Hop die IP oder None bei einem stummen Hop. Sprachunabhängig."""
    hops: list[str | None] = []
    for line in output.splitlines():
        if not re.match(r"^\s*\d+\s", line):
            continue
        found = re.findall(_IPV4, line)
        hops.append(found[0] if found else None)
    return hops


def traceroute(target: str, max_hops: int = 8) -> list[str | None]:
    if IS_WINDOWS:
        command = ["tracert", "-d", "-h", str(max_hops), "-w", "800", target]
    else:
        command = ["traceroute", "-n", "-m", str(max_hops), "-w", "1", "-q", "1", target]
    output = _run(command, max_hops * 3 * 1.5 + 10)
    return parse_traceroute(output) if output else []


def is_lan_address(address: str) -> bool:
    return ipaddress.ip_address(address).is_private


def is_public_address(address: str) -> bool:
    parsed = ipaddress.ip_address(address)
    # 100.64.0.0/10 (Carrier-NAT) gehört schon dem Provider, ist aber nicht öffentlich erreichbar.
    return parsed.is_global


def split_hops(hops: list[str | None]) -> tuple[list[str], list[str]]:
    """Teilt eine Route in LAN-Router vor der Leitung und öffentliche Hops dahinter."""
    lan: list[str] = []
    public: list[str] = []
    for hop in hops:
        if hop is None:
            continue
        if is_public_address(hop):
            public.append(hop)
        elif is_lan_address(hop) and not public:
            lan.append(hop)
    return lan, public


# So beantworten Pi-hole und andere DNS-Filter gesperrte Namen.
BLOCKED_ANSWERS = {"0.0.0.0", "::"}


def system_nameservers() -> list[str]:
    """Die im System eingetragenen DNS-Server in ihrer Reihenfolge."""
    try:
        return list(dns.resolver.Resolver().nameservers)
    except dns.exception.DNSException:
        return []


def is_home_dns_server(address: str) -> bool:
    """Ein DNS-Server im Heimnetz, etwa ein Pi-hole. Nicht der lokale Stub von systemd-resolved."""
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return False
    return parsed.version == 4 and parsed.is_private and not parsed.is_loopback


def check_dns(names: list[str], timeout_s: float, nameservers: list[str] | None = None) -> ProbeResult:
    """Löst einen Namen auf, ohne den Cache des Betriebssystems.

    Ohne nameservers gelten die im System eingetragenen Server, samt Ausweichserver. Mit
    nameservers werden genau diese gefragt, etwa nur der Pi-hole oder nur ein öffentlicher Server.
    Eine Sperrantwort (0.0.0.0) zählt als Fehler: Dann ist der Name nicht nutzbar.
    """
    try:
        if nameservers is None:
            resolver = dns.resolver.Resolver()
        else:
            resolver = dns.resolver.Resolver(configure=False)
            resolver.nameservers = list(nameservers)
    except (dns.exception.DNSException, ValueError) as exc:
        return ProbeResult(False, detail=f"resolver: {type(exc).__name__}")
    resolver.cache = None
    last_error = "keine Namen"
    per_name_timeout = timeout_s / len(names)
    for name in names:
        try:
            answer = resolver.resolve(name, "A", lifetime=per_name_timeout, search=False)
        except dns.exception.DNSException as exc:
            last_error = f"{name}: {type(exc).__name__}"
            continue
        if all(item.to_text() in BLOCKED_ANSWERS for item in answer):
            last_error = f"{name}: gesperrt (DNS-Filter)"
            continue
        latency = answer.response.time * 1000 if answer.response.time else None
        return ProbeResult(True, round(latency, 1) if latency is not None else None)
    return ProbeResult(False, detail=last_error)


def _elapsed_ms(started: float) -> float:
    """Dauer von DNS, Verbindungsaufbau, TLS und Antwort zusammen."""
    return round((time.perf_counter() - started) * 1000, 1)


def check_https(urls: list[str], timeout_s: float) -> ProbeResult:
    """Prüft TLS und HTTP. Jede HTTP-Antwort zählt, auch 4xx: Die Verbindung selbst funktioniert."""
    last_error = "keine URL"
    for url in urls:
        request = urllib.request.Request(url, method="HEAD", headers={"User-Agent": f"linkmon/{__version__}"})
        started = time.perf_counter()
        try:
            with urllib.request.urlopen(request, timeout=timeout_s / len(urls)):
                return ProbeResult(True, _elapsed_ms(started))
        except urllib.error.HTTPError:
            return ProbeResult(True, _elapsed_ms(started))
        except (OSError, ValueError) as exc:
            last_error = f"{url}: {type(exc).__name__}"
    return ProbeResult(False, detail=last_error)
