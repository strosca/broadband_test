"""Einordnung von Ausfällen. Reine Logik ohne Netz- oder Dateizugriff."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime

from linkmon.levels import Level


@dataclass(frozen=True)
class CycleStatus:
    """Ergebnis einer Prüfrunde. isp_hop ist None, wenn kein Hop bekannt ist.

    dns ist die Auflösung wie sie das Gerät erlebt, samt Ausweichserver. dns_home fragt jeden
    DNS-Server im Heimnetz (Pi-hole) einzeln, dns_reference einen öffentlichen Server direkt.
    dns_reference ist None, wenn der Vergleich abgeschaltet ist.
    """

    interface_up: bool
    lan: dict[str, bool]
    isp_hop: bool | None
    external: dict[str, bool]
    dns: bool
    https: bool
    dns_home: dict[str, bool] = field(default_factory=dict)
    dns_reference: bool | None = None


@dataclass(frozen=True)
class Classification:
    level: Level | None
    detail: str = ""


def classify(status: CycleStatus) -> Classification:
    """Ordnet eine Prüfrunde der innersten gestörten Ebene zu.

    Ein einzelnes unerreichbares externes Ziel ist kein Leitungsausfall: Erst wenn
    alle ausfallen, liegt das Problem nicht mehr beim Ziel.
    """
    if not status.interface_up:
        return Classification(Level.LOCAL, "Keine aktive Netzwerkverbindung oder Standardroute")

    failed_lan = [host for host, ok in status.lan.items() if not ok]
    if failed_lan:
        return Classification(Level.ROUTER, "Nicht erreichbar: " + ", ".join(failed_lan))

    if status.external and not any(status.external.values()):
        if status.isp_hop is True:
            return Classification(Level.LINE, "Erster Provider-Hop erreichbar, externe Ziele nicht")
        if status.isp_hop is False:
            return Classification(Level.LINE, "Erster Provider-Hop und externe Ziele nicht erreichbar")
        return Classification(Level.LINE, "Externe Ziele nicht erreichbar, Router erreichbar")

    dns_result = classify_dns(status)
    if dns_result is not None:
        return dns_result
    if not status.https:
        return Classification(Level.HTTPS, "HTTPS fehlgeschlagen, DNS und externe Ziele in Ordnung")
    return Classification(None)


def classify_dns(status: CycleStatus) -> Classification | None:
    """Trennt einen gestörten Pi-hole von einer DNS-Störung beim Anbieter oder im Internet.

    Antwortet der öffentliche Vergleichsserver, funktioniert DNS über die Leitung: Die Ursache liegt
    im Heimnetz. Ein ausgefallener Pi-hole fällt auch auf, wenn ein Ausweichserver einspringt,
    denn andere Geräte im Haushalt haben oft nur den Pi-hole eingetragen.
    """
    failed_home = [host for host, ok in status.dns_home.items() if not ok]
    reference = status.dns_reference
    if not status.dns:
        if reference is False or (reference is None and not status.dns_home):
            suffix = ", auch der öffentliche DNS-Server antwortet nicht" if reference is False else ""
            return Classification(Level.DNS, f"DNS-Auflösung fehlgeschlagen, externe Ziele erreichbar{suffix}")
        if failed_home:
            return Classification(Level.DNS_HOME, f"DNS-Server im Heimnetz antwortet nicht: {', '.join(failed_home)}"
                                  + (", öffentlicher DNS-Server antwortet" if reference else ""))
        if status.dns_home:
            return Classification(Level.DNS_HOME, "Namensauflösung auf diesem Gerät gestört, "
                                                  "die DNS-Server antworten direkt gefragt")
        return Classification(Level.DNS, "DNS-Server des Anbieters gestört, öffentlicher DNS-Server antwortet")
    if failed_home:
        if reference is False:
            return Classification(Level.DNS, f"DNS-Server im Heimnetz ({', '.join(failed_home)}) und öffentlicher "
                                             "DNS-Server antworten nicht")
        return Classification(Level.DNS_HOME, f"DNS-Server im Heimnetz antwortet nicht: {', '.join(failed_home)}. "
                                              "Dieses Gerät nutzt einen Ausweichserver, andere Geräte evtl. nicht")
    return None


@dataclass(frozen=True)
class OutageOpened:
    level: Level
    detail: str
    start: datetime


@dataclass(frozen=True)
class OutageClosed:
    level: Level
    end: datetime
    confirmed: bool


OutageEvent = OutageOpened | OutageClosed


class OutageTracker:
    """Macht aus einzelnen Prüfrunden Ausfälle mit Start, Ende und Ebene.

    Start ist die erste fehlerhafte Runde, Ende die erste fehlerfreie: Die Schwellwerte
    verhindern Fehlalarme, verfälschen aber die Dauer nicht.
    """

    def __init__(self, failure_threshold: int, recovery_threshold: int) -> None:
        self._failure_threshold = failure_threshold
        self._recovery_threshold = recovery_threshold
        self._current: Level | None = None
        self._fault_run: list[tuple[datetime, Level, str]] = []
        self._ok_run_start: datetime | None = None
        self._ok_run_count = 0

    @property
    def current(self) -> Level | None:
        return self._current

    def feed(self, moment: datetime, result: Classification) -> list[OutageEvent]:
        if result.level is None:
            return self._feed_ok(moment)
        return self._feed_fault(moment, result.level, result.detail)

    def _feed_ok(self, moment: datetime) -> list[OutageEvent]:
        self._fault_run.clear()
        if self._current is None:
            return []
        if self._ok_run_start is None:
            self._ok_run_start = moment
        self._ok_run_count += 1
        if self._ok_run_count < self._recovery_threshold:
            return []
        event = OutageClosed(self._current, self._ok_run_start, confirmed=True)
        self._current = None
        self._reset_ok_run()
        return [event]

    def _feed_fault(self, moment: datetime, level: Level, detail: str) -> list[OutageEvent]:
        self._reset_ok_run()
        if level == self._current:
            self._fault_run.clear()
            return []

        self._fault_run.append((moment, level, detail))
        if self._current is None:
            if len(self._fault_run) < self._failure_threshold:
                return []
            return [self._open_from_run(self._dominant_level())]

        # Ebenenwechsel während eines Ausfalls erst nach stabilen Runden derselben Ebene.
        recent = self._fault_run[-self._failure_threshold :]
        if len(recent) < self._failure_threshold or any(item[1] != level for item in recent):
            return []
        self._fault_run = recent
        closed = OutageClosed(self._current, recent[0][0], confirmed=True)
        return [closed, self._open_from_run(level)]

    def _dominant_level(self) -> Level:
        counts = Counter(item[1] for item in self._fault_run)
        highest = max(counts.values())
        return next(item[1] for item in reversed(self._fault_run) if counts[item[1]] == highest)

    def _open_from_run(self, level: Level) -> OutageOpened:
        start = self._fault_run[0][0]
        detail = next(item[2] for item in reversed(self._fault_run) if item[1] == level)
        self._current = level
        self._fault_run = []
        return OutageOpened(level, detail, start)

    def _reset_ok_run(self) -> None:
        self._ok_run_start = None
        self._ok_run_count = 0

    def interrupt(self, last_seen: datetime) -> list[OutageEvent]:
        """Die Überwachung war unterbrochen: Das tatsächliche Ende ist unbekannt.

        Der Ausfall endet deshalb beim letzten Herzschlag und wird als unbestätigt markiert.
        """
        self._fault_run.clear()
        self._reset_ok_run()
        if self._current is None:
            return []
        event = OutageClosed(self._current, last_seen, confirmed=False)
        self._current = None
        return [event]

