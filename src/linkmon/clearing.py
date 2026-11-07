"""Messdaten leeren, damit Webinterface, Plots und Berichte wieder bei null beginnen.

Läuft kein Messdienst, verschiebt `linkmon clear` den Ordner selbst. Läuft einer, schreibt es einen
Auftrag in den Datenordner. Der Dienst erledigt ihn vor der nächsten Prüfrunde und meldet das
Ergebnis zurück. So muss niemand den Dienst anhalten, und es gibt keine Lücke durch einen Neustart.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

REQUEST_FILE = "leeren_auftrag.json"
RESULT_FILE = "leeren_ergebnis.json"
ARCHIVE_DIR = "archiv"


@dataclass(frozen=True)
class ClearResult:
    ok: bool
    archive: Path | None = None
    message: str = ""


def archive_target(data_dir: Path, moment: datetime) -> Path:
    base = data_dir / ARCHIVE_DIR / f"messdaten_{moment.strftime('%Y%m%d_%H%M%S')}"
    target, number = base, 1
    while target.exists():
        number += 1
        target = base.with_name(f"{base.name}_{number}")
    return target


def request_path(data_dir: Path) -> Path:
    return data_dir / REQUEST_FILE


def result_path(data_dir: Path) -> Path:
    return data_dir / RESULT_FILE


def write_request(data_dir: Path, archive: Path | None) -> None:
    result_path(data_dir).unlink(missing_ok=True)
    temporary = request_path(data_dir).with_suffix(".tmp")
    temporary.write_text(json.dumps({"archive": str(archive) if archive else None}), encoding="utf-8")
    temporary.replace(request_path(data_dir))


def take_request(data_dir: Path) -> tuple[bool, Path | None]:
    """Holt einen wartenden Auftrag ab. Liefert (gibt es einen, Archivziel oder None zum Löschen)."""
    path = request_path(data_dir)
    try:
        content = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return False, None
    except (OSError, ValueError):
        content = {}
    path.unlink(missing_ok=True)
    archive = content.get("archive") if isinstance(content, dict) else None
    # Ein unlesbarer Auftrag löscht nie: Im Zweifel wird archiviert.
    if not isinstance(content, dict) or "archive" not in content:
        return True, archive_target(data_dir, datetime.now())
    return True, Path(archive) if archive else None


def write_result(data_dir: Path, result: ClearResult) -> None:
    payload = {"ok": result.ok, "archive": str(result.archive) if result.archive else None, "message": result.message}
    result_path(data_dir).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def wait_for_result(data_dir: Path, timeout_s: float) -> ClearResult | None:
    """Wartet, bis der Dienst den Auftrag erledigt hat. None heißt: keine Antwort in der Zeit."""
    deadline = time.monotonic() + timeout_s
    path = result_path(data_dir)
    while time.monotonic() < deadline:
        try:
            content = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            time.sleep(0.3)
            continue
        path.unlink(missing_ok=True)
        archive = content.get("archive")
        return ClearResult(bool(content.get("ok")), Path(archive) if archive else None, content.get("message", ""))
    return None
