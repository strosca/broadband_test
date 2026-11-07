"""Live-Webseite für bandwidth_monitor.py: zeigt die laufende Messung mit Plotly an.

Plotly wird lokal ausgeliefert und nicht aus dem Netz geladen, denn die Seite muss
gerade dann funktionieren, wenn das Internet ausfällt.
"""

import importlib.util
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

MAX_PLAUSIBLE_PING_MS = 10_000


def plotly_js_path():
    """Findet plotly.min.js im installierten Python-Paket, ohne Plotly zu importieren."""
    spec = importlib.util.find_spec("plotly")
    if spec is None or spec.origin is None:
        return None
    path = Path(spec.origin).parent / "package_data" / "plotly.min.js"
    return path if path.is_file() else None


def _number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def live_point(row):
    """Macht aus einer Messung einen Plotpunkt.

    speedtest-cli liefert bei gescheitertem Latenztest Platzhalter wie 1.800.000 ms oder
    0 Mbit/s. Sie sind keine Messwerte und würden die Achsen sprengen, deshalb zeigt
    die Seite sie als fehlgeschlagen an.
    """
    ping = _number(row["ping_ms"])
    download = _number(row["download_mbps"])
    upload = _number(row["upload_mbps"])
    reason = row["error"]
    if not reason:
        if ping is None or ping <= 0 or ping > MAX_PLAUSIBLE_PING_MS:
            reason = f"Unplausibler Ping: {row['ping_ms']} ms"
        elif download is None or download <= 0:
            reason = f"Unplausibler Download: {row['download_mbps']} Mbit/s"
        elif upload is None or upload <= 0:
            reason = f"Unplausibler Upload: {row['upload_mbps']} Mbit/s"
    if reason:
        return {"time": row["timestamp"], "ok": False, "reason": reason, "server": row["server"],
                "ping": None, "download": None, "upload": None}
    return {"time": row["timestamp"], "ok": True, "reason": "", "server": row["server"],
            "ping": ping, "download": download, "upload": upload}


def start_live_server(rows, status, lock, port):
    """Startet die Seite im Hintergrund. Gibt die Adresse zurück oder None, wenn es nicht geht.

    Die Messung ist wichtiger als die Anzeige: Fehlt Plotly oder ist der Port belegt,
    läuft sie mit einem Hinweis ohne Live-Seite weiter.
    """
    js_path = plotly_js_path()
    if js_path is None:
        print("Hinweis: Plotly fehlt, daher keine Live-Seite (pip install plotly).", flush=True)
        return None
    plotly_js = js_path.read_bytes()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/":
                self._send(PAGE.encode("utf-8"), "text/html; charset=utf-8", "no-store")
            elif self.path == "/plotly.min.js":
                self._send(plotly_js, "text/javascript", "max-age=86400")
            elif self.path == "/data":
                with lock:
                    payload = {"points": [live_point(row) for row in rows], **status}
                self._send(json.dumps(payload).encode("utf-8"), "application/json", "no-store")
            else:
                self.send_error(404)

        def _send(self, body, content_type, cache):
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", cache)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass  # Die Abrufe alle paar Sekunden würden die Messausgabe überdecken.

    try:
        # Nur lokal: Über diese Adresse ist die Seite von anderen Geräten aus nicht erreichbar.
        server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError as exc:
        print(f"Hinweis: Port {port} nicht verfügbar ({exc}), daher keine Live-Seite. "
              "Anderer Port mit --port.", flush=True)
        return None
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, name="live-server", daemon=True).start()
    return f"http://127.0.0.1:{port}/"


PAGE = """<!doctype html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Bandbreite live</title>
<script src="/plotly.min.js"></script>
<style>
:root {
  color-scheme: light;
  --surface: #fcfcfb; --text: #0b0b0b; --muted: #52514e; --grid: #e1e0d9;
  --border: rgba(11,11,11,0.10); --download: #2a78d6; --upload: #eb6834; --ping: #1baf7a;
  --fail-bg: #fdf0ef;
}
@media (prefers-color-scheme: dark) {
  :root {
    color-scheme: dark;
    --surface: #1a1a19; --text: #f0efec; --muted: #c3c2b7; --grid: #2c2c2a;
    --border: rgba(255,255,255,0.10); --download: #3987e5; --upload: #d95926; --ping: #199e70;
    --fail-bg: #3a2322;
  }
}
* { box-sizing: border-box; }
body { margin: 0; padding: 20px 24px 28px; background: var(--surface); color: var(--text);
       font: 14px/1.4 "Segoe UI", system-ui, sans-serif; }
header { display: flex; flex-wrap: wrap; align-items: baseline; gap: 4px 14px; margin-bottom: 14px; }
h1 { margin: 0; font-size: 22px; }
#state { color: var(--muted); }
#state.problem { color: var(--text); font-weight: 600; }
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 10px; margin-bottom: 10px; }
.tile { padding: 10px 14px; border: 1px solid var(--border); border-radius: 6px; }
.tile .label { color: var(--muted); font-size: 12px; }
.tile .value { font-size: 23px; font-weight: 600; font-variant-numeric: tabular-nums; }
.tile .unit { color: var(--muted); font-size: 13px; font-weight: 400; }
#chart { width: 100%; height: 620px; }
#error { display: none; margin-bottom: 10px; padding: 8px 12px; border-radius: 6px; background: var(--fail-bg); }
</style>
</head>
<body>
<header><h1>Bandbreite live</h1><span id="state" role="status">Lade …</span></header>
<div id="error"></div>
<div class="tiles">
  <div class="tile"><div class="label">Download, zuletzt</div><div class="value" id="t-down">–</div></div>
  <div class="tile"><div class="label">Upload, zuletzt</div><div class="value" id="t-up">–</div></div>
  <div class="tile"><div class="label">Ping, zuletzt</div><div class="value" id="t-ping">–</div></div>
  <div class="tile"><div class="label">Download, Median</div><div class="value" id="t-median">–</div></div>
  <div class="tile"><div class="label">Messungen (fehlgeschlagen)</div><div class="value" id="t-count">–</div></div>
</div>
<div id="chart"></div>
<script>
const POLL_MS = 5000;
const css = name => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const number = (value, digits = 1) => value == null ? "–"
  : value.toLocaleString("de-DE", {minimumFractionDigits: digits, maximumFractionDigits: digits});
const clock = iso => new Date(iso).toLocaleString("de-DE");
const escapeHtml = text => String(text).replace(/[&<>"]/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]));

function median(values) {
  const sorted = values.filter(v => v != null).sort((a, b) => a - b);
  if (!sorted.length) return null;
  const middle = Math.floor(sorted.length / 2);
  return sorted.length % 2 ? sorted[middle] : (sorted[middle - 1] + sorted[middle]) / 2;
}

function setTile(id, value, unit) {
  const element = document.getElementById(id);
  element.textContent = value;
  if (unit) {
    const span = document.createElement("span");
    span.className = "unit";
    span.textContent = " " + unit;
    element.append(span);
  }
}

function hover(point) {
  const head = `<b>${clock(point.time)}</b>`;
  if (!point.ok) return `${head}<br>Fehlgeschlagen: ${escapeHtml(point.reason)}`;
  return `${head}<br>Download: ${number(point.download)} Mbit/s<br>Upload: ${number(point.upload)} Mbit/s`
    + `<br>Ping: ${number(point.ping)} ms<br>Server: ${escapeHtml(point.server)}`;
}

function line(points, field, name, color, axis, showlegend = true) {
  return {
    x: points.map(p => p.time), y: points.map(p => p[field]), name, xaxis: "x", yaxis: axis, showlegend,
    mode: "lines+markers", connectgaps: false, line: {color, width: 2}, marker: {color, size: 7},
    hoverinfo: "text", hovertext: points.map(hover), hoverlabel: {align: "left"},
  };
}

function failures(points, axis, showlegend) {
  const failed = points.filter(p => !p.ok);
  return {
    x: failed.map(p => p.time), y: failed.map(() => 0), name: "Fehlgeschlagen", xaxis: "x", yaxis: axis, showlegend,
    mode: "markers", legendgroup: "failed", marker: {color: css("--muted"), size: 10, symbol: "x"},
    hoverinfo: "text", hovertext: failed.map(hover), hoverlabel: {align: "left"}, cliponaxis: false,
  };
}

function layout() {
  const axis = {gridcolor: css("--grid"), linecolor: css("--grid"), zeroline: false, automargin: true};
  const heading = (text, y) => ({text: `<b>${text}</b>`, x: 0, y, xref: "paper", yref: "paper", xanchor: "left",
                                 yanchor: "bottom", showarrow: false, font: {size: 13}});
  return {
    uirevision: "keep-zoom", paper_bgcolor: "rgba(0,0,0,0)", plot_bgcolor: "rgba(0,0,0,0)",
    font: {family: '"Segoe UI", system-ui, sans-serif', color: css("--text"), size: 12},
    margin: {l: 50, r: 16, t: 34, b: 36}, hovermode: "closest",
    legend: {orientation: "h", x: 1, xanchor: "right", y: 1.0, yanchor: "bottom"},
    grid: {rows: 2, columns: 1, roworder: "top to bottom"},
    xaxis: {...axis, type: "date", showgrid: false, anchor: "y2",
            // Abstand, damit die Fehler-Markierungen auf der Nulllinie die Beschriftung nicht verdecken.
            ticks: "outside", ticklen: 10, tickcolor: "rgba(0,0,0,0)", hoverformat: "%d.%m.%Y %H:%M:%S",
            // Das Format folgt dem Zoom, sonst stünde bei kurzen Läufen mehrfach dieselbe Minute da.
            tickformatstops: [
              {dtickrange: [null, 59999], value: "%H:%M:%S"},
              {dtickrange: [60000, 43199999], value: "%H:%M"},
              {dtickrange: [43200000, null], value: "%d.%m."},
            ]},
    yaxis: {...axis, domain: [0.40, 1], rangemode: "tozero", title: {text: "Mbit/s"}},
    yaxis2: {...axis, domain: [0, 0.29], rangemode: "tozero", title: {text: "ms"}},
    annotations: [heading("Download und Upload", 1), heading("Ping", 0.29)],
  };
}

function render(data) {
  const points = data.points;
  const traces = [
    line(points, "download", "Download", css("--download"), "y"),
    line(points, "upload", "Upload", css("--upload"), "y"),
    failures(points, "y", true),
    line(points, "ping", "Ping", css("--ping"), "y2", false),
    failures(points, "y2", false),
  ];
  Plotly.react("chart", traces, layout(), {responsive: true, displaylogo: false, locale: "de"});

  const valid = points.filter(p => p.ok);
  const last = valid[valid.length - 1];
  setTile("t-down", number(last && last.download), "Mbit/s");
  setTile("t-up", number(last && last.upload), "Mbit/s");
  setTile("t-ping", number(last && last.ping), "ms");
  setTile("t-median", number(median(valid.map(p => p.download))), "Mbit/s");
  setTile("t-count", `${points.length} (${points.length - valid.length})`);

  const box = document.getElementById("error");
  const newest = points[points.length - 1];
  box.style.display = newest && !newest.ok ? "block" : "none";
  box.textContent = newest && !newest.ok ? `Letzte Messung fehlgeschlagen: ${newest.reason}` : "";
}

function stateText(data) {
  if (data.stopped) return "Messung beendet";
  if (data.measuring) return "Messung läuft …";
  if (data.next_at) return `Nächste Messung um ${new Date(data.next_at).toLocaleTimeString("de-DE")}`;
  return "Warte …";
}

async function refresh() {
  const state = document.getElementById("state");
  try {
    const response = await fetch("/data", {cache: "no-store"});
    if (!response.ok) throw new Error(response.status);
    const data = await response.json();
    render(data);
    state.textContent = stateText(data);
    state.className = "";
  } catch (error) {
    state.textContent = "Keine Verbindung zum Skript: Es wurde beendet. Die letzten Werte bleiben sichtbar.";
    state.className = "problem";
  }
}

refresh();
setInterval(refresh, POLL_MS);
</script>
</body>
</html>
"""
