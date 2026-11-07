"""Der Dienst: prüft im Sekundentakt die Erreichbarkeit und macht daraus Ausfälle."""

from __future__ import annotations

import json
import logging
import platform
import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor, wait
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

from linkmon import __version__, clearing, hostinfo, net
from linkmon.classify import (
    Classification,
    CycleStatus,
    OutageClosed,
    OutageEvent,
    OutageOpened,
    OutageTracker,
    classify,
)
from linkmon.config import Config
from linkmon.hostinfo import HostInfo
from linkmon.levels import LEVEL_LABELS
from linkmon.net import ProbeResult
from linkmon.store import Store
from linkmon.timeutil import iso_utc, now_utc

log = logging.getLogger("linkmon.monitor")

ROUTE_REFRESH_S = 3600
META_LAN_HOPS = "route.lan_hops"
META_ISP_HOP = "route.isp_hop"
META_CONNECTION = "verbindung"
CONNECTION_LABELS = {"lan": "LAN", "wlan": "WLAN", "unbekannt": "unbekannt"}


@dataclass(frozen=True)
class CycleResult:
    status: CycleStatus
    results: dict[str, list[Any]]


class Prober:
    """Führt eine Prüfrunde parallel aus und kennt die aktuellen Ziele."""

    def __init__(self, config: Config, store: Store) -> None:
        self._reach = config.reachability
        self._store = store
        self._pool = ThreadPoolExecutor(max_workers=12, thread_name_prefix="probe")
        self._lock = threading.Lock()
        cached_lan = store.get_meta(META_LAN_HOPS)
        self._route_lan: list[str] = json.loads(cached_lan) if cached_lan else []
        self._isp_hop: str | None = None if self._reach.isp_hop == "auto" else self._reach.isp_hop
        if self._isp_hop is None:
            self._isp_hop = store.get_meta(META_ISP_HOP)

    def targets_text(self) -> str:
        with self._lock:
            return f"LAN (Route) {self._route_lan or '-'}, Provider-Hop {self._isp_hop or '-'}"

    def refresh_route(self) -> bool:
        """Ermittelt Router und ersten Provider-Hop neu. Gibt True zurück, wenn sich etwas änderte."""
        hops = net.traceroute(self._reach.external_targets[0])
        lan, public = net.split_hops(hops)
        if not lan and not public:
            log.warning("Traceroute lieferte keine verwertbaren Hops, behalte bisherige Ziele")
            return False
        isp_hop = self._reach.isp_hop if self._reach.isp_hop != "auto" else None
        if isp_hop is None:
            # Der erste antwortende öffentliche Hop: Manche Hops antworten nur auf Traceroute.
            for candidate in public:
                if any(net.ping(candidate, self._reach.ping_timeout_s).ok for _ in range(3)):
                    isp_hop = candidate
                    break
        lan = [host for host in lan if any(net.ping(host, self._reach.ping_timeout_s).ok for _ in range(3))]
        with self._lock:
            changed = lan != self._route_lan or isp_hop != self._isp_hop
            self._route_lan = lan
            self._isp_hop = isp_hop
        if changed:
            self._store.set_meta(META_LAN_HOPS, json.dumps(lan))
            if isp_hop:
                self._store.set_meta(META_ISP_HOP, isp_hop)
        return changed

    def _lan_targets(self, gateway: str | None) -> list[str]:
        if self._reach.lan_targets != "auto":
            return list(self._reach.lan_targets)
        with self._lock:
            hosts = list(self._route_lan)
        if gateway and gateway not in hosts:
            hosts.insert(0, gateway)
        return hosts

    def _dns_home_servers(self) -> list[str]:
        if self._reach.dns_home_servers != "auto":
            return list(self._reach.dns_home_servers)
        return [host for host in net.system_nameservers() if net.is_home_dns_server(host)]

    def run_cycle(self) -> CycleResult:
        reach = self._reach
        interface = hostinfo.active_interface()
        gateway = net.default_gateway()
        interface_up = hostinfo.interface_is_up(interface) and gateway is not None

        lan_hosts = self._lan_targets(gateway)
        with self._lock:
            isp_hop = self._isp_hop

        futures: dict[str, Future[ProbeResult]] = {}
        for host in lan_hosts:
            futures[f"lan:{host}"] = self._pool.submit(net.ping, host, reach.ping_timeout_s)
        if isp_hop:
            futures[f"isp:{isp_hop}"] = self._pool.submit(net.ping, isp_hop, reach.ping_timeout_s)
        for host in reach.external_targets:
            futures[f"ext:{host}"] = self._pool.submit(net.ping, host, reach.ping_timeout_s)
        futures["dns"] = self._pool.submit(net.check_dns, reach.dns_names, reach.dns_timeout_s)
        dns_home = self._dns_home_servers()
        for host in dns_home:
            futures[f"dnshome:{host}"] = self._pool.submit(net.check_dns, reach.dns_names, reach.dns_timeout_s, [host])
        if reach.dns_reference_servers:
            futures["dnsref"] = self._pool.submit(net.check_dns, reach.dns_names, reach.dns_timeout_s,
                                                  reach.dns_reference_servers)
        futures["https"] = self._pool.submit(net.check_https, reach.https_urls, reach.https_timeout_s)

        budget = max(reach.ping_timeout_s, reach.dns_timeout_s, reach.https_timeout_s) + 1.5
        wait(futures.values(), timeout=budget)
        probes: dict[str, ProbeResult] = {}
        for name, future in futures.items():
            if not future.done():
                probes[name] = ProbeResult(False, detail="zeitüberschreitung")
                continue
            try:
                probes[name] = future.result()
            except Exception as exc:  # Eine kaputte Prüfung darf den Dienst nicht beenden.
                log.exception("Prüfung %s fehlgeschlagen", name)
                probes[name] = ProbeResult(False, detail=type(exc).__name__)

        status = CycleStatus(
            interface_up=interface_up,
            lan={host: probes[f"lan:{host}"].ok for host in lan_hosts},
            isp_hop=probes[f"isp:{isp_hop}"].ok if isp_hop else None,
            external={host: probes[f"ext:{host}"].ok for host in reach.external_targets},
            dns=probes["dns"].ok,
            https=probes["https"].ok,
            dns_home={host: probes[f"dnshome:{host}"].ok for host in dns_home},
            dns_reference=probes["dnsref"].ok if "dnsref" in probes else None,
        )
        results = {name: [int(p.ok), p.latency_ms, p.detail] for name, p in probes.items()}
        results["interface"] = [int(interface_up), None, interface or ""]
        return CycleResult(status, results)


ProbeRunner = Callable[[], CycleResult]
HostInfoCollector = Callable[[], HostInfo]


class Monitor:
    def __init__(self, config: Config, store: Store, *,
                 probe_runner: ProbeRunner | None = None,
                 route_refresher: Callable[[], bool] | None = None,
                 host_info: HostInfoCollector = hostinfo.collect_host_info,
                 clock: Callable[[], datetime] = now_utc) -> None:
        self._config = config
        self._db = store
        self._stop = threading.Event()
        self._clock = clock
        self._host_info = host_info
        if probe_runner is None:
            prober = Prober(config, store)
            probe_runner = prober.run_cycle
            route_refresher = prober.refresh_route
        self._probe_runner = probe_runner
        self._route_refresher = route_refresher
        reach = config.reachability
        self._tracker = OutageTracker(reach.failure_threshold, reach.recovery_threshold)
        self._session_id = 0
        self._last_cycle: datetime | None = None
        self._last_route_refresh: datetime | None = None
        self._route_thread: threading.Thread | None = None
        self._interface: str | None = None
        self._info: HostInfo | None = None
        self._connection_thread: threading.Thread | None = None

    def stop(self) -> None:
        self._stop.set()

    @property
    def gap_threshold_s(self) -> float:
        return monitor_gap_threshold(self._config.reachability.interval_s)

    def run(self) -> None:
        now = self._clock()
        recovered = self._db.recover_after_crash(now)
        if recovered:
            log.warning("%d offene Ausfälle nach unsauberem Ende als unbestätigt geschlossen", recovered)
        self._start_session(now, self._host_info())

        reason = "regulär beendet"
        try:
            self._reachability_loop()
        except Exception:
            reason = "Fehler, siehe Log"
            log.exception("Überwachung abgebrochen")
            raise
        finally:
            self._stop.set()
            end = self._last_cycle or self._clock()
            self._apply(self._tracker.interrupt(end))
            self._db.end_session(self._session_id, end, reason)
            log.info("Sitzung %d beendet: %s", self._session_id, reason)

    def _start_session(self, now: datetime, info: HostInfo) -> None:
        self._session_id = self._db.start_session(
            now, info.hostname, info.os_name, platform.python_version(), __version__, self._config.sha256,
            interface=info.interface, connection_type=info.connection_type, link_mbps=info.link_mbps,
        )
        self._info = info
        self._interface = info.interface
        self._remember_connection(info)
        log.info("Sitzung %d gestartet auf %s, Interface %s (%s, %s Mbit/s)", self._session_id, info.hostname,
                 info.interface, info.connection_type, info.link_mbps)
        for warning in hostinfo.measurement_warnings(info):
            log.warning(warning)
            self._db.add_event(self._session_id, now, "warnung", warning)

    def _handle_clear_request(self) -> None:
        """Leert die Messdaten, wenn `linkmon clear` darum gebeten hat. Die Messung läuft danach weiter."""
        data_dir = self._config.data_dir
        found, archive = clearing.take_request(data_dir)
        if not found:
            return
        now = self._clock()
        end = self._last_cycle or now
        self._apply(self._tracker.interrupt(end))
        self._db.end_session(self._session_id, end, "Messdaten geleert")
        try:
            self._db.reset(archive)
        except OSError as exc:
            log.error("Messdaten konnten nicht geleert werden: %s", exc)
            result = clearing.ClearResult(False, message=f"{type(exc).__name__}: {exc}. Ist eine Datei in Excel offen?")
        else:
            self._last_cycle = None
            where = f"archiviert nach {archive}" if archive else "gelöscht"
            log.warning("Messdaten geleert und %s", where)
            result = clearing.ClearResult(True, archive, f"Messdaten {where}")
        self._start_session(now, self._info if self._info is not None else self._host_info())
        clearing.write_result(data_dir, result)

    def _remember_connection(self, info: HostInfo) -> None:
        self._db.set_meta(META_CONNECTION, json.dumps(asdict(info), ensure_ascii=False))

    def _maybe_refresh_route(self, now: datetime, healthy: bool) -> None:
        if self._route_refresher is None or not healthy:
            return
        running = self._route_thread is not None and self._route_thread.is_alive()
        due = (self._last_route_refresh is None
               or (now - self._last_route_refresh).total_seconds() >= ROUTE_REFRESH_S)
        if running or not due:
            return
        self._last_route_refresh = now
        self._route_thread = threading.Thread(target=self._refresh_route, name="route", daemon=True)
        self._route_thread.start()

    def _refresh_route(self) -> None:
        try:
            if self._route_refresher and self._route_refresher():
                log.info("Route neu ermittelt")
                self._db.add_event(self._session_id, self._clock(), "route", "Router oder Provider-Hop geändert")
        except Exception:
            log.exception("Route konnte nicht ermittelt werden")

    def _check_connection(self, results: dict[str, list[Any]]) -> None:
        """Bemerkt einen Wechsel der Netzwerkkarte, etwa von WLAN auf Kabel, ohne Neustart."""
        entry = results.get("interface")
        name = entry[2] if entry and entry[2] else None
        if name is None or name == self._interface:
            return
        if self._connection_thread is not None and self._connection_thread.is_alive():
            return
        previous = self._interface
        self._interface = name
        # Die Verbindungsart zu ermitteln dauert unter Windows einige Sekunden: nicht im Prüftakt.
        self._connection_thread = threading.Thread(target=self._update_connection, args=(previous,),
                                                   name="verbindung", daemon=True)
        self._connection_thread.start()

    def _update_connection(self, previous: str | None) -> None:
        try:
            info = self._host_info()
            self._info = info
            self._remember_connection(info)
            kind = CONNECTION_LABELS.get(info.connection_type, info.connection_type)
            message = f"Verbindung gewechselt: {previous or '-'} auf {info.interface} ({kind})"
            log.info(message)
            self._db.add_event(self._session_id, self._clock(), "verbindung", message)
        except Exception:
            log.exception("Verbindung konnte nicht ermittelt werden")

    def _reachability_loop(self) -> None:
        interval = self._config.reachability.interval_s
        while not self._stop.is_set():
            try:
                self._handle_clear_request()
            except Exception:
                log.exception("Auftrag zum Leeren der Messdaten fehlgeschlagen")
            started = self._clock()
            self._check_gap(started)
            try:
                self._run_one_cycle(started)
            except Exception:
                # Ein einzelner Fehler, etwa eine gesperrte Datei, beendet die Messung nicht.
                # Die Lücke bleibt als fehlende Herzschläge sichtbar.
                log.exception("Prüfrunde fehlgeschlagen")
            elapsed = (self._clock() - started).total_seconds()
            self._stop.wait(max(0.0, interval - elapsed))

    def _check_gap(self, now: datetime) -> None:
        """Ruhezustand oder hängender Prozess: Hier hat das Tool nichts gesehen."""
        if self._last_cycle is None:
            return
        gap = (now - self._last_cycle).total_seconds()
        if gap <= self.gap_threshold_s:
            return
        message = f"Messlücke von {gap:.0f} s (Tool lief nicht oder Rechner schlief)"
        log.warning(message)
        self._db.add_event(self._session_id, now, "messluecke", message)
        self._apply(self._tracker.interrupt(self._last_cycle))

    def _run_one_cycle(self, moment: datetime) -> None:
        cycle = self._probe_runner()
        result: Classification = classify(cycle.status)
        self._db.add_cycle(self._session_id, moment, result.level, cycle.results)
        self._last_cycle = moment
        self._apply(self._tracker.feed(moment, result))
        self._check_connection(cycle.results)
        self._maybe_refresh_route(moment, result.level is None)

    def _apply(self, events: list[OutageEvent]) -> None:
        for event in events:
            if isinstance(event, OutageOpened):
                log.warning("Ausfall begonnen: %s seit %s (%s)", LEVEL_LABELS[event.level],
                            iso_utc(event.start), event.detail)
                self._db.open_outage(self._session_id, event.level, event.detail, event.start)
            elif isinstance(event, OutageClosed):
                log.warning("Ausfall beendet: %s um %s%s", LEVEL_LABELS[event.level], iso_utc(event.end),
                            "" if event.confirmed else " (Ende unbestätigt)")
                self._db.close_outage(event.level, event.end, event.confirmed)


def monitor_gap_threshold(interval_s: float) -> float:
    """Ab diesem Abstand zwischen zwei Herzschlägen gilt die Zeit als nicht überwacht."""
    return max(interval_s * 3, 30.0)
