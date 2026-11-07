"""Verbindungsqualität aus den Prüfrunden: Ping, Paketverlust und Jitter je Strecke.

Jede Prüfrunde schickt einen Ping an jedes Ziel. Daraus ergeben sich je Strecke die
Antwortzeit (Median, P95, Maximum) und der Anteil unbeantworteter Pings.
"""

from __future__ import annotations

import json
import math
import threading
from collections import OrderedDict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from linkmon.store import CYCLE_COLUMNS, Store, read_rows

KINDS = ("lan", "isp", "ext", "dns", "dnshome", "dnsref", "https")
# Frühere Versionen markierten Prüfrunden während eigener Speedtests. Die Leitung war dann
# ausgelastet: Diese Antwortzeiten zeigen die eigene Last, nicht die Leitung.
LEGACY_SPEEDTEST_MARKER = "_speedtest"
OK = "|ok"
MS = "|ms"
INTERNET = "internet"
# Schrittweiten für den Verlauf. Gewählt wird die kleinste, die höchstens MAX_BUCKETS Punkte ergibt.
BUCKET_STEPS_MIN = (1, 2, 5, 10, 15, 30, 60, 120, 180, 360, 720, 1440)
MAX_BUCKETS = 1500


@dataclass(frozen=True)
class Track:
    """Eine Strecke im Verlauf, etwa "Internet" aus zwei externen Zielen zusammen."""

    key: str
    label: str
    columns: tuple[str, ...]
    is_ping: bool = True


@dataclass(frozen=True)
class TrackStats:
    track: Track
    sent: int
    lost: int
    median_ms: float | None
    p95_ms: float | None
    max_ms: float | None
    jitter_ms: float | None

    @property
    def loss_pct(self) -> float | None:
        return 100 * self.lost / self.sent if self.sent else None


def parse_cycles(rows: Iterable[dict[str, Any]]) -> pd.DataFrame:
    """Macht aus Prüfrunden eine breite Tabelle: je Ziel Erfolg (1, 0, -1 = nicht geprüft) und Antwortzeit."""
    stamps: list[str] = []
    oks: dict[str, list[int]] = {}
    times: dict[str, list[float]] = {}
    for row in rows:
        try:
            results: dict[str, list[Any]] = json.loads(row["results"])
        except (TypeError, ValueError):
            continue
        # Die Runde bleibt als Lebenszeichen erhalten, zählt aber nicht in Ping und Verlust.
        excluded = LEGACY_SPEEDTEST_MARKER in results
        index = len(stamps)
        stamps.append(row["ts_utc"])
        for key, item in results.items():
            if key.split(":")[0] not in KINDS:
                continue
            if key not in oks:
                oks[key] = [-1] * index
                times[key] = [math.nan] * index
            if excluded:
                oks[key].append(-1)
                times[key].append(math.nan)
                continue
            ok = int(item[0])
            oks[key].append(ok)
            times[key].append(float(item[1]) if ok and item[1] is not None else math.nan)
        for key, values in oks.items():
            if len(values) < len(stamps):
                values.append(-1)
                times[key].append(math.nan)
    data: dict[str, Any] = {"ts": pd.to_datetime(stamps, utc=True, format="ISO8601")}
    for key in oks:
        data[key + OK] = np.asarray(oks[key], dtype=np.int8)
        data[key + MS] = np.asarray(times[key], dtype=np.float32)
    return pd.DataFrame(data)


class SampleCache:
    """Behält gelesene Tagesdateien im Speicher. Neu gelesen wird nur, was sich geändert hat."""

    def __init__(self, max_files: int = 45) -> None:
        self._max = max_files
        self._items: OrderedDict[Path, tuple[tuple[int, int], pd.DataFrame]] = OrderedDict()
        self._lock = threading.Lock()

    def frame(self, path: Path) -> pd.DataFrame:
        try:
            stat = path.stat()
        except FileNotFoundError:
            return parse_cycles([])
        version = (stat.st_mtime_ns, stat.st_size)
        with self._lock:
            cached = self._items.get(path)
            if cached and cached[0] == version:
                self._items.move_to_end(path)
                return cached[1]
        frame = parse_cycles(read_rows(path, CYCLE_COLUMNS))
        with self._lock:
            self._items[path] = (version, frame)
            self._items.move_to_end(path)
            while len(self._items) > self._max:
                self._items.popitem(last=False)
        return frame


def load_samples(store: Store, start: datetime, end: datetime, cache: SampleCache | None = None) -> pd.DataFrame:
    files = store.cycle_files(start, end)
    frames = [cache.frame(path) if cache else parse_cycles(read_rows(path, CYCLE_COLUMNS)) for path in files]
    frames = [frame for frame in frames if not frame.empty]
    if not frames:
        return parse_cycles([])
    frame = pd.concat(frames, ignore_index=True, sort=False)
    for column in frame.columns:
        if column.endswith(OK):
            frame[column] = frame[column].fillna(-1).astype(np.int8)
    low, high = pd.Timestamp(start).tz_convert("UTC"), pd.Timestamp(end).tz_convert("UTC")
    return frame[(frame["ts"] >= low) & (frame["ts"] < high)].reset_index(drop=True)


def result_keys(frame: pd.DataFrame) -> list[str]:
    return [column[: -len(OK)] for column in frame.columns if column.endswith(OK)]


def _addresses(keys: list[str]) -> str:
    return ", ".join(key.split(":", 1)[1] for key in keys)


def tracks(frame: pd.DataFrame) -> list[Track]:
    """Strecken in fester Reihenfolge: Internet, Provider-Hop, Heimnetz, dann DNS und HTTPS."""
    keys = result_keys(frame)
    external = [key for key in keys if key.startswith("ext:")]
    result: list[Track] = []
    if external:
        result.append(Track(INTERNET, f"Internet ({_addresses(external)})", tuple(external)))
    for key in keys:
        if key.startswith("isp:"):
            result.append(Track(key, f"Provider-Hop {key[4:]}", (key,)))
    for key in keys:
        if key.startswith("lan:"):
            result.append(Track(key, f"Heimnetz {key[4:]}", (key,)))
    if "dns" in keys:
        result.append(Track("dns", "DNS-Antwort", ("dns",), is_ping=False))
    for key in keys:
        if key.startswith("dnshome:"):
            result.append(Track(key, f"DNS Heimnetz {key[8:]}", (key,), is_ping=False))
    if "dnsref" in keys:
        result.append(Track("dnsref", "DNS öffentlich", ("dnsref",), is_ping=False))
    if "https" in keys:
        result.append(Track("https", "HTTPS-Abruf", ("https",), is_ping=False))
    return result


def ping_tracks(frame: pd.DataFrame) -> list[Track]:
    return [track for track in tracks(frame) if track.is_ping]


def find_track(frame: pd.DataFrame, key: str) -> Track | None:
    return next((track for track in tracks(frame) if track.key == key), None)


def _long(frame: pd.DataFrame, track: Track, groups: pd.Series | pd.DataFrame | None = None) -> pd.DataFrame:
    """Stapelt die Ziele einer Strecke untereinander, je Zeile ein Ping."""
    parts = []
    for key in track.columns:
        part = pd.DataFrame({"ok": frame[key + OK].to_numpy(), "ms": frame[key + MS].to_numpy(dtype=float)})
        if groups is not None:
            group_frame = groups.to_frame() if isinstance(groups, pd.Series) else groups
            part = pd.concat([group_frame.reset_index(drop=True), part], axis=1)
        parts.append(part)
    long = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame({"ok": [], "ms": []})
    return long[long["ok"] >= 0]


def _jitter(frame: pd.DataFrame, track: Track) -> float | None:
    """Mittlere Abweichung aufeinanderfolgender Antwortzeiten je Ziel (nach RFC 3550 vereinfacht)."""
    steps = [frame[key + MS].dropna().astype(float).diff().abs().dropna() for key in track.columns]
    values = pd.concat(steps) if steps else pd.Series(dtype=float)
    return float(values.mean()) if len(values) else None


def _clean(value: Any) -> float | None:
    return None if value is None or pd.isna(value) else float(value)


def track_stats(frame: pd.DataFrame, track: Track) -> TrackStats:
    long = _long(frame, track)
    times = long["ms"].dropna()
    return TrackStats(
        track=track,
        sent=int(len(long)),
        lost=int((long["ok"] == 0).sum()),
        median_ms=_clean(times.median()) if len(times) else None,
        p95_ms=_clean(times.quantile(0.95)) if len(times) else None,
        max_ms=_clean(times.max()) if len(times) else None,
        jitter_ms=_jitter(frame, track),
    )


def all_track_stats(frame: pd.DataFrame) -> list[TrackStats]:
    return [track_stats(frame, track) for track in tracks(frame)]


def _aggregate(long: pd.DataFrame, by: str | list[str]) -> pd.DataFrame:
    long = long.assign(lost=(long["ok"] == 0).astype(int))
    grouped = long.groupby(by, sort=True)
    table = pd.DataFrame({
        "sent": grouped["ok"].size(),
        "lost": grouped["lost"].sum(),
        "median": grouped["ms"].median(),
        "p95": grouped["ms"].quantile(0.95),
        "max": grouped["ms"].max(),
    })
    table["loss_pct"] = 100 * table["lost"] / table["sent"]
    return table


def bucket_size(start: datetime, end: datetime) -> pd.Timedelta:
    span_min = max((end - start).total_seconds() / 60, 1)
    for step in BUCKET_STEPS_MIN:
        if span_min / step <= MAX_BUCKETS:
            return pd.Timedelta(minutes=step)
    return pd.Timedelta(minutes=BUCKET_STEPS_MIN[-1])


def bucket_table(frame: pd.DataFrame, track: Track, size: pd.Timedelta) -> pd.DataFrame:
    """Kennzahlen je Zeitintervall. Intervalle ohne Prüfung bleiben leer, damit Linien dort abbrechen."""
    columns = ["sent", "lost", "median", "p95", "max", "loss_pct"]
    if frame.empty:
        return pd.DataFrame(columns=columns, index=pd.DatetimeIndex([], tz="UTC"))
    buckets = frame["ts"].dt.floor(size).rename("bucket")
    table = _aggregate(_long(frame, track, buckets), "bucket")
    full = pd.date_range(buckets.min(), buckets.max(), freq=size)
    return table.reindex(full)


def local_day(frame: pd.DataFrame, zone: ZoneInfo) -> pd.Series:
    return frame["ts"].dt.tz_convert(zone).dt.strftime("%Y-%m-%d").rename("day")


def day_table(frame: pd.DataFrame, track: Track, zone: ZoneInfo) -> pd.DataFrame:
    if frame.empty:
        return pd.DataFrame(columns=["sent", "lost", "median", "p95", "max", "loss_pct"])
    return _aggregate(_long(frame, track, local_day(frame, zone)), "day")


def hour_matrix(frame: pd.DataFrame, track: Track, zone: ZoneInfo, value: str, days: list[str]) -> pd.DataFrame:
    """Kennzahl je Stunde (Zeilen 0 bis 23) und lokalem Tag (Spalten). Ohne Prüfung bleibt die Zelle leer."""
    empty = pd.DataFrame(np.nan, index=range(24), columns=days)
    if frame.empty:
        return empty
    local = frame["ts"].dt.tz_convert(zone)
    groups = pd.DataFrame({"day": local.dt.strftime("%Y-%m-%d"), "hour": local.dt.hour})
    table = _aggregate(_long(frame, track, groups), ["day", "hour"])
    matrix = table[value].unstack("day")
    return matrix.reindex(index=range(24), columns=days)


def days_between(start: datetime, end: datetime, zone: ZoneInfo) -> list[str]:
    first = start.astimezone(zone).date()
    last = (end - timedelta(seconds=1)).astimezone(zone).date()
    return [(first + timedelta(days=offset)).isoformat() for offset in range((last - first).days + 1)]


def latest_internet_ms(results_json: str | None) -> float | None:
    """Mittlere Antwortzeit der externen Ziele in der letzten Prüfrunde."""
    if not results_json:
        return None
    results = json.loads(results_json)
    values = [float(item[1]) for key, item in results.items()
              if key.startswith("ext:") and item[0] and item[1] is not None]
    return sum(values) / len(values) if values else None
