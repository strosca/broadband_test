#!/bin/sh
# Richtet linkmon unter Linux oder auf dem Raspberry Pi ein.
#   ./deploy/linux/install.sh            Umgebung und config.toml anlegen
#   ./deploy/linux/install.sh --enable   zusätzlich den systemd-Dienst installieren und starten (nutzt sudo)
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
VENV="$ROOT/.venv"

for tool in python3 ping ip traceroute; do
    if ! command -v "$tool" >/dev/null 2>&1; then
        echo "Fehlt: $tool. Unter Debian oder Raspberry Pi OS: sudo apt install python3-venv iputils-ping iproute2 traceroute" >&2
        exit 1
    fi
done

[ -x "$VENV/bin/python" ] || python3 -m venv "$VENV"
"$VENV/bin/python" -m pip install --quiet --disable-pip-version-check -e "$ROOT"

if [ ! -f "$ROOT/config.toml" ]; then
    cp "$ROOT/config.example.toml" "$ROOT/config.toml"
    echo "Konfiguration angelegt: $ROOT/config.toml"
fi

if [ "${1:-}" = "--enable" ]; then
    sed -e "s|@ROOT@|$ROOT|g" -e "s|@USER@|$(id -un)|g" "$ROOT/deploy/linux/linkmon.service" \
        | sudo tee /etc/systemd/system/linkmon.service >/dev/null
    sudo systemctl daemon-reload
    sudo systemctl enable --now linkmon.service
    echo "Dienst läuft. Status: systemctl status linkmon"
fi

echo "Nächste Schritte: siehe README.md"
echo "  $VENV/bin/linkmon --config $ROOT/config.toml status"
