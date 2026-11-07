"""Kommandozeile: linkmon run | dashboard | report | export | clear | status."""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import signal
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import IO

from linkmon import __version__, net
from linkmon.config import Config, ConfigError, find_config_path, load_config
from linkmon.store import Store
from linkmon.timeutil import format_local, now_utc, parse_iso, parse_user_time

log = logging.getLogger("linkmon")


def setup_logging(config: Config) -> None:
    directory = config.data_dir / "logs"
    directory.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    file_handler = logging.handlers.RotatingFileHandler(directory / "linkmon.log", maxBytes=5_000_000, backupCount=5,
                                                        encoding="utf-8")
    file_handler.setFormatter(formatter)
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    logging.basicConfig(level=logging.INFO, handlers=[file_handler, console], force=True)


class InstanceLock:
    """Verhindert, dass zwei Prozesse gleichzeitig in dieselben Messdaten schreiben."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._handle: IO[str] | None = None

    def acquire(self) -> bool:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        handle = self._path.open("a+", encoding="ascii")
        try:
            if sys.platform == "win32":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        self._handle = handle
        return True

    def release(self) -> None:
        if self._handle is None:
            return
        try:
            if sys.platform == "win32":
                import msvcrt

                self._handle.seek(0)
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        self._handle.close()  # Unter Linux gibt das Schließen die Sperre frei.
        self._handle = None


def _start_web_interface(config: Config) -> None:
    """Startet das Webinterface. Scheitert es, läuft die Messung trotzdem weiter."""
    try:
        from linkmon.dashboard import start_in_background

        url = start_in_background(config)
    except Exception:
        log.exception("Webinterface konnte nicht gestartet werden, die Messung läuft weiter")
        return
    if url:
        from linkmon.dashboard import listen_urls

        log.info("Webinterface: %s", ", ".join(listen_urls(config.dashboard.host, config.dashboard.port)))
    else:
        log.warning("Webinterface nicht gestartet: Port %d ist belegt. Die Messung läuft weiter.", config.dashboard.port)


def _cmd_run(config: Config, _args: argparse.Namespace) -> int:
    from linkmon.monitor import Monitor

    setup_logging(config)
    lock = _lock(config)  # Muss bis zum Ende leben, sonst ist die Sperre sofort wieder frei.
    if not lock.acquire():
        log.error("Es läuft bereits ein Messdienst mit diesen Daten (%s).", config.data_dir)
        return 2

    store = Store(config.store_dir)
    monitor = Monitor(config, store)
    if config.dashboard.enabled:
        _start_web_interface(config)

    def handle_signal(_number: int, _frame: object) -> None:
        log.info("Stoppsignal erhalten")
        monitor.stop()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, handle_signal)

    try:
        monitor.run()
    finally:
        store.close()
    return 0


def _cmd_dashboard(config: Config, args: argparse.Namespace) -> int:
    from linkmon.dashboard import listen_urls, make_web_server

    host = args.host or config.dashboard.host
    port = args.port or config.dashboard.port
    try:
        server = make_web_server(config, host, port)
    except OSError as exc:
        print(f"Fehler: Port {port} ist belegt oder die Adresse {host} ungültig ({exc}). "
              "Läuft schon der Messdienst mit Webinterface?", file=sys.stderr)
        return 2
    print(f"Webinterface: {', '.join(listen_urls(host, port))}  (Beenden mit Strg+C)")
    try:
        server.run()
    except KeyboardInterrupt:
        pass
    finally:
        server.close()
    return 0


def _read_range(config: Config, args: argparse.Namespace) -> tuple[datetime, datetime]:
    zone = config.zone
    start = parse_user_time(args.date_from, zone)
    end = parse_user_time(args.date_to, zone, end_of_day=True)
    if end <= start:
        raise ConfigError("--to muss nach --from liegen")
    return start, end


def _lock(config: Config) -> InstanceLock:
    return InstanceLock(config.data_dir / "linkmon.lock")


def _open_store(config: Config) -> Store:
    return Store(config.store_dir, readonly=True)


def _cmd_report(config: Config, args: argparse.Namespace) -> int:
    from linkmon.quality import INTERNET
    from linkmon.report import build_report

    start, end = _read_range(config, args)
    stamp = now_utc().astimezone(config.zone).strftime("%Y%m%d_%H%M%S")
    directory = Path(args.out).resolve() if args.out else config.data_dir / "berichte" / f"bericht_{stamp}"
    result = build_report(config, _open_store(config), start, end, directory)
    summary = result.summary
    print(f"Bericht: {result.pdf}")
    print(f"Rohdaten-SHA-256: {result.export.raw_sha256}")
    print(f"Verfügbarkeit Leitung: {_percent(summary.availability_pct, 3)}, "
          f"Ausfälle: {sum(summary.outage_count.values())}")
    internet = summary.track(INTERNET)
    if internet:
        print(f"Internet: Ping Median {_millis(internet.median_ms)}, P95 {_millis(internet.p95_ms)}, "
              f"Verlust {_percent(internet.loss_pct, 2)}")
    return 0


def _cmd_export(config: Config, args: argparse.Namespace) -> int:
    from linkmon.export import export_range

    start, end = _read_range(config, args)
    stamp = now_utc().astimezone(config.zone).strftime("%Y%m%d_%H%M%S")
    directory = Path(args.out).resolve() if args.out else config.data_dir / "exporte" / f"export_{stamp}"
    result = export_range(_open_store(config), start, end, directory, excel_de=args.excel_de)
    print(f"Export: {result.directory}")
    print(f"Ausfälle: {result.outages}, Prüfrunden: {result.probe_cycles}, Minuten mit Prüfungen: {result.minutes}")
    print(f"Rohdaten-SHA-256: {result.raw_sha256}")
    return 0


CLEAR_WAIT_S = 30


def _confirm(question: str) -> bool:
    try:
        answer = input(f"{question} [j/N] ")
    except EOFError:
        return False
    return answer.strip().lower() in {"j", "ja", "y", "yes"}


def _cmd_clear(config: Config, args: argparse.Namespace) -> int:
    from linkmon import clearing
    from linkmon.store import remove_store_dir

    if not Store.exists(config.store_dir):
        print("Keine Messdaten vorhanden, nichts zu leeren.")
        return 0
    store = _open_store(config)
    print(f"Messdaten: {config.store_dir}")
    print(f"Prüfrunden: {store.count_cycles()}, Ausfälle: {len(store.outages())}")
    archive = None if args.delete else clearing.archive_target(config.data_dir, now_utc().astimezone(config.zone))
    action = "endgültig LÖSCHEN" if archive is None else f"nach {archive} verschieben"
    if not args.yes and not _confirm(f"Alle Messdaten {action}? Webinterface und Berichte beginnen dann bei null."):
        print("Abgebrochen, nichts geändert.")
        return 1

    lock = _lock(config)
    if lock.acquire():
        try:
            remove_store_dir(config.store_dir, archive)
        finally:
            lock.release()
        print("Messdaten " + ("gelöscht." if archive is None else f"archiviert: {archive}"))
        return 0

    # Der Messdienst läuft: Er leert selbst und misst danach ohne Unterbrechung weiter.
    clearing.write_request(config.data_dir, archive)
    print("Der Messdienst läuft und leert die Daten vor der nächsten Prüfrunde ...")
    result = clearing.wait_for_result(config.data_dir, CLEAR_WAIT_S)
    if result is None:
        clearing.request_path(config.data_dir).unlink(missing_ok=True)
        print(f"Fehler: Der Messdienst hat in {CLEAR_WAIT_S} s nicht reagiert. Nichts geändert.", file=sys.stderr)
        return 2
    if not result.ok:
        print(f"Fehler: {result.message}", file=sys.stderr)
        return 2
    print(result.message + ". Die Messung läuft weiter.")
    return 0


def _percent(value: float | None, digits: int) -> str:
    return "-" if value is None else f"{value:.{digits}f} %".replace(".", ",")


def _millis(value: float | None) -> str:
    return "-" if value is None else f"{value:.0f} ms"


def _cmd_status(config: Config, _args: argparse.Namespace) -> int:
    from linkmon.hostinfo import collect_host_info, measurement_warnings
    from linkmon.quality import all_track_stats, load_samples

    zone = config.zone
    reach = config.reachability
    print(f"Konfiguration: {config.path}")
    from linkmon.dashboard import listen_urls

    print(f"Webinterface: {', '.join(listen_urls(config.dashboard.host, config.dashboard.port))} "
          "(solange der Messdienst läuft)")
    print(f"DNS-Server: {', '.join(net.system_nameservers()) or '-'}")
    print(f"Prüfung: alle {reach.interval_s:g} s, Ziele {', '.join(reach.external_targets)}, DNS, HTTPS")
    info = collect_host_info()
    print(f"Gerät: {info.hostname}, Interface {info.interface}, {info.connection_type}, {info.link_mbps} Mbit/s")
    for warning in measurement_warnings(info):
        print(f"WARNUNG: {warning}")
    print(f"Messdaten: {config.store_dir}")
    if not Store.exists(config.store_dir):
        print("Noch keine Messdaten vorhanden")
        return 0
    store = _open_store(config)
    last = store.last_cycle_time()
    print(f"Letzte Prüfrunde: {format_local(parse_iso(last), zone) if last else '-'}")
    print(f"Prüfrunden: {store.count_cycles()}, Ausfälle: {len(store.outages())}")
    for row in store.open_outages():
        print(f"OFFENER AUSFALL: {row['level']} seit {format_local(parse_iso(row['start_utc']), zone)}: {row['detail']}")
    end = now_utc()
    samples = load_samples(store, end - timedelta(hours=1), end)
    if not samples.empty:
        print("Letzte Stunde:")
        for item in all_track_stats(samples):
            print(f"  {item.track.label}: Verlust {_percent(item.loss_pct, 2)}, Median {_millis(item.median_ms)}, "
                  f"P95 {_millis(item.p95_ms)}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="linkmon", description=__doc__)
    parser.add_argument("--config", help="Pfad zur config.toml (sonst LINKMON_CONFIG oder ./config.toml)")
    parser.add_argument("--version", action="version", version=f"linkmon {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("run", help="Messdienst starten").set_defaults(handler=_cmd_run)

    dashboard = commands.add_parser("dashboard", help="Nur das Webinterface starten, ohne Messung")
    dashboard.add_argument("--host")
    dashboard.add_argument("--port", type=int)
    dashboard.set_defaults(handler=_cmd_dashboard)

    def add_range(command: argparse.ArgumentParser) -> None:
        command.add_argument("--from", dest="date_from", required=True, help="Start, z. B. 2026-11-01 (lokale Zeit)")
        command.add_argument("--to", dest="date_to", required=True, help="Ende, ein reines Datum zählt bis Tagesende")
        command.add_argument("--out", help="Zielordner")

    report = commands.add_parser("report", help="PDF-Bericht erzeugen")
    add_range(report)
    report.set_defaults(handler=_cmd_report)

    export = commands.add_parser("export", help="CSV, JSON und Rohdaten mit Prüfsumme exportieren")
    add_range(export)
    export.add_argument("--excel-de", action="store_true", help="CSV für deutsches Excel (Semikolon, Dezimalkomma)")
    export.set_defaults(handler=_cmd_export)

    clear = commands.add_parser("clear", help="Alle Messdaten leeren (archivieren), auch bei laufendem Messdienst")
    clear.add_argument("--delete", action="store_true", help="Endgültig löschen statt nach data/archiv verschieben")
    clear.add_argument("--yes", "-y", action="store_true", help="Ohne Rückfrage")
    clear.set_defaults(handler=_cmd_clear)

    commands.add_parser("status", help="Konfiguration und letzten Stand anzeigen").set_defaults(handler=_cmd_status)
    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    args = build_parser().parse_args(argv)
    try:
        config = load_config(find_config_path(args.config))
        return int(args.handler(config, args))
    except (ConfigError, FileNotFoundError, ValueError) as exc:
        print(f"Fehler: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"Fehler: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
