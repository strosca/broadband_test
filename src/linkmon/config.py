"""Lädt und prüft die TOML-Konfiguration."""

from __future__ import annotations

import hashlib
import os
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class ConfigError(Exception):
    """Die Konfiguration fehlt oder ist ungültig."""


# Gab es bis zur Umstellung auf reine Verfügbarkeitsmessung.
REMOVED_SECTIONS = {"contract", "speedtest"}


@dataclass(frozen=True)
class General:
    data_dir: str = "data"
    timezone: str = "Europe/Berlin"


@dataclass(frozen=True)
class Reachability:
    interval_s: float = 3.0
    failure_threshold: int = 3
    recovery_threshold: int = 2
    lan_targets: str | list[str] = "auto"
    isp_hop: str = "auto"
    external_targets: list[str] = field(default_factory=lambda: ["1.1.1.1", "8.8.8.8"])
    dns_names: list[str] = field(default_factory=lambda: ["www.google.com", "www.cloudflare.com"])
    https_urls: list[str] = field(
        default_factory=lambda: [
            "https://www.google.com/generate_204",
            "https://www.cloudflare.com/cdn-cgi/trace",
        ]
    )
    ping_timeout_s: float = 1.5
    dns_timeout_s: float = 2.0
    https_timeout_s: float = 3.0


@dataclass(frozen=True)
class Dashboard:
    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 8050
    refresh_s: int = 30


@dataclass(frozen=True)
class Report:
    tariff: str = ""
    customer_name: str = ""
    customer_number: str = ""
    address: str = ""


@dataclass(frozen=True)
class Config:
    path: Path
    sha256: str
    general: General = field(default_factory=General)
    reachability: Reachability = field(default_factory=Reachability)
    dashboard: Dashboard = field(default_factory=Dashboard)
    report: Report = field(default_factory=Report)

    @property
    def base_dir(self) -> Path:
        return self.path.parent

    @property
    def data_dir(self) -> Path:
        return (self.base_dir / self.general.data_dir).resolve()

    @property
    def store_dir(self) -> Path:
        return self.data_dir / "messdaten"

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.general.timezone)


def _section(cls: type, raw: dict[str, Any], name: str) -> Any:
    known = {item.name for item in fields(cls)}
    unknown = set(raw) - known
    if unknown:
        raise ConfigError(f"Unbekannte Einstellung in [{name}]: {', '.join(sorted(unknown))}")
    return cls(**raw)


def _validate(config: Config) -> None:
    try:
        ZoneInfo(config.general.timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConfigError(f"Unbekannte Zeitzone: {config.general.timezone}") from exc

    reach = config.reachability
    if not 1 <= reach.interval_s <= 60:
        raise ConfigError("reachability.interval_s muss zwischen 1 und 60 liegen")
    if reach.failure_threshold < 1 or reach.recovery_threshold < 1:
        raise ConfigError("Schwellwerte in [reachability] müssen mindestens 1 sein")
    if len(reach.external_targets) < 2:
        raise ConfigError("reachability.external_targets braucht mindestens zwei Ziele")
    if not reach.dns_names or not reach.https_urls:
        raise ConfigError("reachability.dns_names und https_urls dürfen nicht leer sein")
    if not (reach.lan_targets == "auto" or isinstance(reach.lan_targets, list)):
        raise ConfigError('reachability.lan_targets muss "auto" oder eine Liste sein')


def find_config_path(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit).resolve()
    from_env = os.environ.get("LINKMON_CONFIG")
    if from_env:
        return Path(from_env).resolve()
    return (Path.cwd() / "config.toml").resolve()


def load_config(path: Path) -> Config:
    if not path.is_file():
        raise ConfigError(f"Konfigurationsdatei nicht gefunden: {path} (Vorlage: config.example.toml)")
    content = path.read_bytes()
    try:
        raw = tomllib.loads(content.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise ConfigError(f"{path} ist nicht lesbar: {exc}") from exc

    sections = {
        "general": General,
        "reachability": Reachability,
        "dashboard": Dashboard,
        "report": Report,
    }
    removed = set(raw) & REMOVED_SECTIONS
    if removed:
        raise ConfigError(f"Abschnitt {', '.join(sorted(removed))} gibt es nicht mehr: linkmon misst nur noch die "
                          "Verfügbarkeit. Entferne ihn aus der Konfiguration (Vorlage: config.example.toml).")
    unknown = set(raw) - set(sections)
    if unknown:
        raise ConfigError(f"Unbekannter Abschnitt: {', '.join(sorted(unknown))}")
    parsed = {name: _section(cls, raw.get(name, {}), name) for name, cls in sections.items()}

    config = Config(path=path, sha256=hashlib.sha256(content).hexdigest(), **parsed)
    _validate(config)
    return config
