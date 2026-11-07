"""Archiv, abgelöst von linkmon. Misst regelmäßig Ping, Download und Upload bis zum Hotkey.

Während der Messung läuft eine Live-Seite im Browser (Standard: http://127.0.0.1:8765/).
Danach liegen CSV und PNG in bandwidth_logs/ und der Plot wird angezeigt.

Gemessen wird mit der Ookla CLI (tools/ookla/speedtest.exe) und nur gegen feste Server in Frankfurt.

Installation:  pip install pynput matplotlib plotly
Start:         python bandwidth_monitor.py [--interval SEKUNDEN] [--hotkey KOMBINATION]
               [--port PORT] [--no-browser] [--server ID [ID ...]]
Neu plotten:   python bandwidth_monitor.py --plot bandwidth_logs/DATEI.csv
"""

import argparse
import csv
import json
import math
import statistics
import subprocess
import threading
import time
import webbrowser
from datetime import datetime, timedelta
from pathlib import Path

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
from pynput import keyboard

from bandwidth_live import start_live_server

# Bewusst ohne Strg+Alt+<Taste>: Auf deutschen Tastaturen sendet AltGr dasselbe,
# Strg+Alt+Q würde also schon beim Tippen von "@" die Messung beenden.
DEFAULT_HOTKEY = "<ctrl>+<shift>+<f12>"
PROJECT_DIR = Path(__file__).resolve().parent.parent
LOG_DIR = PROJECT_DIR / "bandwidth_logs"
OOKLA_BINARY = PROJECT_DIR / "tools" / "ookla" / "speedtest.exe"
# Beide in Frankfurt: 23M GmbH und, nur wenn dieser Server scheitert, GSL Networks.
FRANKFURT_SERVERS = [44081, 50771]
OOKLA_TIMEOUT_S = 150
CSV_FIELDS = ["timestamp", "ping_ms", "download_mbps", "upload_mbps", "server", "error"]
TREND_MIN_POINTS = 30  # Ab so vielen Messungen zeigt der Plot Einzelwerte blass und darüber den Trend.

COLOR_DOWNLOAD = "#2a78d6"
COLOR_UPLOAD = "#eb6834"
COLOR_PING = "#1baf7a"
COLOR_SURFACE = "#fcfcfb"
COLOR_TEXT = "#0b0b0b"
COLOR_TEXT_MUTED = "#52514e"
COLOR_GRID = "#e1e0d9"


def run_ookla(server_id, stop):
    """Startet die Ookla CLI gegen genau diesen Server und liefert das Ergebnis oder wirft RuntimeError."""
    process = subprocess.Popen(
        [str(OOKLA_BINARY), "--format=json", "--accept-license", "--accept-gdpr", f"--server-id={server_id}"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
    )
    deadline = time.monotonic() + OOKLA_TIMEOUT_S
    while True:
        try:
            stdout, stderr = process.communicate(timeout=1)
            break
        except subprocess.TimeoutExpired:
            # Beim Stoppen die Messung abbrechen, sonst belastet sie die Leitung noch eine Weile.
            if stop.is_set() or time.monotonic() >= deadline:
                process.kill()
                process.communicate()
                raise RuntimeError("Zeitüberschreitung oder abgebrochen") from None

    message = ""
    for line in stdout.splitlines():
        try:
            document = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(document, dict) and document.get("type") == "result":
            return document
        if isinstance(document, dict) and (document.get("level") == "error" or "error" in document):
            message = str(document.get("message") or document.get("error"))
    last_lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    raise RuntimeError(message or (last_lines[-1] if last_lines else f"Exit-Code {process.returncode}"))


def measure(servers, stop):
    """Misst gegen den ersten festen Server, die weiteren nur, wenn er scheitert.

    Ein Fehler (z. B. Ausfall) wird zu einer Zeile mit Fehlertext. Eine automatische
    Serverwahl gibt es bewusst nicht, sonst wären die Messungen nicht vergleichbar.
    """
    row = dict.fromkeys(CSV_FIELDS, "")
    row["timestamp"] = datetime.now().isoformat(timespec="seconds")
    errors = []
    for server_id in servers:
        try:
            result = run_ookla(server_id, stop)
            row["ping_ms"] = round(result["ping"]["latency"], 1)
            row["download_mbps"] = round(result["download"]["bandwidth"] * 8 / 1e6, 2)
            row["upload_mbps"] = round(result["upload"]["bandwidth"] * 8 / 1e6, 2)
            row["server"] = f"{result['server']['name']} ({result['server']['location']})"
            return row
        except (RuntimeError, OSError, KeyError, TypeError) as exc:
            errors.append(f"Server {server_id}: {type(exc).__name__}: {exc}")
    row["error"] = " | ".join(errors)
    return row


def describe(row):
    if row["error"]:
        return f"FEHLER: {row['error']}"
    return (
        f"Ping {row['ping_ms']:>6} ms | Download {row['download_mbps']:>8} Mbit/s | "
        f"Upload {row['upload_mbps']:>8} Mbit/s | {row['server']}"
    )


def measurement_loop(csv_path, interval_s, stop, lock, rows, status, servers):
    """Misst im festen Raster, damit die Messdauer das Intervall nicht verschiebt."""
    start = time.monotonic()
    tick = 0
    with csv_path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, CSV_FIELDS)
        writer.writeheader()
        file.flush()
        while not stop.is_set():
            with lock:
                status["measuring"] = True
            row = measure(servers, stop)
            with lock:
                status["measuring"] = False
                # Eine vom Hotkey abgebrochene Messung ist unvollständig und wird verworfen.
                if stop.is_set():
                    return
                # Sofort schreiben, damit bei Absturz oder Stromausfall nichts verloren geht.
                writer.writerow(row)
                file.flush()
                rows.append(row)
            print(f"[{row['timestamp']}] {describe(row)}", flush=True)

            # Ist eine Messung länger als das Intervall, entfallen die verpassten Termine.
            tick = max(tick + 1, int((time.monotonic() - start) / interval_s) + 1)
            wait_s = start + tick * interval_s - time.monotonic()
            with lock:
                status["next_at"] = (datetime.now().astimezone() + timedelta(seconds=wait_s)).isoformat()
            stop.wait(wait_s)


def rolling_median(values, window):
    """Zentrierter gleitender Median. Ausfälle bleiben Lücken und werden nicht überbrückt."""
    half = window // 2
    result = []
    for i, value in enumerate(values):
        around = [v for v in values[max(0, i - half):i + half + 1] if not math.isnan(v)]
        result.append(float("nan") if math.isnan(value) else statistics.median(around))
    return result


def plot_results(rows, png_path):
    def series(field):
        return [float(row[field]) if row[field] != "" else float("nan") for row in rows]

    def median_of(values):
        return statistics.median(v for v in values if not math.isnan(v))

    times = [datetime.fromisoformat(row["timestamp"]) for row in rows]
    failures = [t for t, row in zip(times, rows) if row["error"]]
    has_values = len(failures) < len(rows)
    use_trend = len(rows) >= TREND_MIN_POINTS
    window = max(5, len(rows) // 30) | 1  # Ungerade, damit das Fenster zentriert ist.

    def draw(ax, values, color, label=None):
        """Zeichnet eine Reihe und gibt die Werte der sichtbaren Hauptlinie zurück."""
        if not use_trend:
            ax.plot(times, values, color=color, label=label, linewidth=2, marker="o", markersize=5,
                    markeredgecolor=COLOR_SURFACE, markeredgewidth=1)
            return values
        trend = rolling_median(values, window)
        ax.plot(times, values, color=color, linewidth=0.8, alpha=0.3)
        ax.plot(times, trend, color=color, label=label, linewidth=2.2, solid_capstyle="round")
        return trend

    with plt.rc_context({"font.family": ["Segoe UI", "DejaVu Sans"]}):
        fig, (ax_speed, ax_ping) = plt.subplots(
            2, 1, sharex=True, figsize=(12, 7), height_ratios=[2, 1],
            layout="constrained", facecolor=COLOR_SURFACE,
        )
        # Platz oben für Titel und Untertitel, unten für den Hinweis zum Trend.
        fig.get_layout_engine().set(rect=(0.005, 0.035, 0.99, 0.875))

        download = series("download_mbps")
        upload = series("upload_mbps")
        ping = series("ping_ms")
        download_line = draw(ax_speed, download, COLOR_DOWNLOAD, "Download")
        upload_line = draw(ax_speed, upload, COLOR_UPLOAD, "Upload")
        ping_line = draw(ax_ping, ping, COLOR_PING)

        speed_title = "Download und Upload in Mbit/s"
        ping_title = "Ping in ms"
        if has_values:
            speed_title += f"  (Median {median_of(download):.0f} und {median_of(upload):.0f})"
            ping_title += f"  (Median {median_of(ping):.0f})"
        ax_speed.set_title(speed_title, loc="left", color=COLOR_TEXT, fontsize=12, fontweight="bold", pad=10)
        ax_ping.set_title(ping_title, loc="left", color=COLOR_TEXT, fontsize=12, fontweight="bold", pad=10)

        for ax in (ax_speed, ax_ping):
            ax.set_facecolor(COLOR_SURFACE)
            ax.margins(x=0.05, y=0.15)
            ax.set_ylim(bottom=0)
            if not has_values:  # Ohne Messwerte käme sonst eine Achse von 0 bis 0,05 heraus.
                ax.set_ylim(0, 1)
            ax.grid(axis="y", color=COLOR_GRID, linewidth=0.8)
            ax.set_axisbelow(True)
            ax.tick_params(colors=COLOR_TEXT_MUTED, length=0, labelsize=10)
            for side in ("top", "right", "left"):
                ax.spines[side].set_visible(False)
            ax.spines["bottom"].set_color(COLOR_GRID)
            if failures:
                # Ausfälle liegen als Markierung auf der Nulllinie, sonst wären sie nur eine Lücke.
                ax.plot(failures, [0] * len(failures), linestyle="none", marker="x", markersize=7,
                        markeredgewidth=1.8, color=COLOR_TEXT_MUTED, clip_on=False, zorder=5,
                        label="Messung fehlgeschlagen")

        # Ende der Hauptlinie beschriften: der höhere Wert nach oben, der niedrigere nach unten,
        # damit sich die Texte bei ähnlichen Werten nicht überlagern.
        if has_values:
            last = max(i for i, row in enumerate(rows) if not row["error"])
            down_end, up_end = download_line[last], upload_line[last]
            labels = [(ax_speed, down_end, 6 if down_end >= up_end else -6),
                      (ax_speed, up_end, 6 if up_end > down_end else -6),
                      (ax_ping, ping_line[last], 6)]
            for ax, value, offset in labels:
                ax.annotate(f"{value:.0f}", (times[last], value), xytext=(7, offset),
                            textcoords="offset points", va="center", color=COLOR_TEXT, fontsize=10)

        locator = mdates.AutoDateLocator()
        ax_ping.xaxis.set_major_locator(locator)
        ax_ping.xaxis.set_major_formatter(mdates.ConciseDateFormatter(
            locator,
            formats=["%Y", "%m.%Y", "%d.%m.", "%H:%M", "%H:%M", "%H:%M:%S"],
            zero_formats=["", "%Y", "%m.%Y", "%d.%m.", "%H:%M", "%H:%M"],
            offset_formats=[""] * 6,  # Der Zeitraum steht bereits im Untertitel.
        ))
        if len(set(times)) == 1:  # Ein einzelner Zeitpunkt würde die Achse auf mehrere Jahre aufziehen.
            ax_ping.set_xlim(times[0] - timedelta(minutes=10), times[0] + timedelta(minutes=10))

        ax_speed.legend(loc="lower right", bbox_to_anchor=(1.0, 1.0), ncols=3, frameon=False,
                        labelcolor=COLOR_TEXT, fontsize=10, borderaxespad=0.2)

        count = "1 Messung" if len(rows) == 1 else f"{len(rows)} Messungen"
        fig.text(0.012, 0.965, "Bandbreitenmessung", color=COLOR_TEXT, fontsize=18, fontweight="bold", va="center")
        fig.text(0.012, 0.925, f"{times[0]:%d.%m.%Y %H:%M} bis {times[-1]:%d.%m.%Y %H:%M}  ·  {count}, "
                               f"davon {len(failures)} fehlgeschlagen",
                 color=COLOR_TEXT_MUTED, fontsize=11, va="center")
        if use_trend:
            fig.text(0.012, 0.018, f"Kräftige Linie: gleitender Median über {window} Messungen. "
                                   "Blasse Linie: Einzelmessungen.",
                     color=COLOR_TEXT_MUTED, fontsize=9, va="center")

        fig.savefig(png_path, dpi=150, facecolor=COLOR_SURFACE)


def plot_existing_csv(csv_path):
    with csv_path.open(newline="", encoding="utf-8") as file:
        rows = list(csv.DictReader(file))
    if not rows:
        print(f"{csv_path} enthält keine Messungen, daher kein Plot.")
        return
    png_path = csv_path.with_suffix(".png")
    plot_results(rows, png_path)
    print(f"Plot: {png_path}", flush=True)
    plt.show()


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--interval", type=float, default=300, help="Sekunden zwischen zwei Messungen (Standard: 300 = 5 min)")
    parser.add_argument("--hotkey", default=DEFAULT_HOTKEY, help=f"Stopp-Hotkey (Standard: {DEFAULT_HOTKEY})")
    parser.add_argument("--plot", type=Path, metavar="CSV", help="Nicht messen, nur eine vorhandene CSV neu plotten")
    parser.add_argument("--port", type=int, default=8765, help="Port der Live-Seite (Standard: 8765)")
    parser.add_argument("--no-browser", action="store_true", help="Live-Seite nicht automatisch im Browser öffnen")
    parser.add_argument("--server", type=int, nargs="+", default=FRANKFURT_SERVERS, metavar="ID",
                        help="Feste Server-IDs: der erste, die weiteren nur als Ausweichserver "
                             f"(Standard: {' '.join(map(str, FRANKFURT_SERVERS))}, Frankfurt)")
    args = parser.parse_args()
    if args.interval <= 0:
        parser.error("--interval muss größer als 0 sein")
    if args.plot:
        plot_existing_csv(args.plot)
        return
    if not OOKLA_BINARY.is_file():
        parser.error(f"Ookla CLI nicht gefunden: {OOKLA_BINARY} (Download siehe deploy/windows/install.ps1)")

    LOG_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = LOG_DIR / f"bandwidth_{stamp}.csv"
    png_path = LOG_DIR / f"bandwidth_{stamp}.png"

    stop = threading.Event()
    lock = threading.Lock()
    rows = []
    status = {"measuring": False, "next_at": None, "stopped": False}

    # Daemon-Thread: Eine laufende Messung dauert rund 30 s und lässt sich nicht abbrechen,
    # der Hotkey soll aber sofort greifen.
    worker = threading.Thread(
        target=measurement_loop, args=(csv_path, args.interval, stop, lock, rows, status, args.server), daemon=True
    )
    hotkey = keyboard.GlobalHotKeys({args.hotkey: stop.set})
    hotkey.start()
    worker.start()
    print(f"Messe alle {args.interval:g} s gegen Server {args.server}. Beenden mit {args.hotkey} (oder Strg+C).",
          flush=True)
    live_url = start_live_server(rows, status, lock, args.port)
    if live_url:
        print(f"Live-Ansicht: {live_url}", flush=True)
        if not args.no_browser:
            webbrowser.open(live_url)

    try:
        # Kurzes Warten in Schleife: Unter Windows käme Strg+C sonst nicht durch.
        while not stop.wait(0.2):
            pass
    except KeyboardInterrupt:
        stop.set()
    hotkey.stop()

    with lock:
        finished = list(rows)
        status["stopped"] = True
        status["measuring"] = False
    print(f"\nGestoppt, abgeschlossene Messungen: {len(finished)}. CSV: {csv_path}", flush=True)
    if not finished:
        print("Keine abgeschlossene Messung, daher kein Plot.")
        return

    plot_results(finished, png_path)
    print(f"Plot: {png_path}", flush=True)
    plt.show()


if __name__ == "__main__":
    main()
