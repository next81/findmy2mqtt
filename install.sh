#!/usr/bin/env bash
# Copyright (c) 2026 Andreas Planer
# Repository: https://github.com/next81/findmy2mqtt
# Licensed under the PolyForm Strict License 1.0.0
# https://polyformproject.org/licenses/strict/1.0.0
set -euo pipefail

APP_USER="findmy2mqtt"
APP_DIR="/opt/findmy2mqtt"
CONF_DIR="/etc/findmy2mqtt"
STATE_DIR="/var/lib/findmy2mqtt"
SERVICE_FILE="/etc/systemd/system/findmy2mqtt.service"
COMMAND_FILE="/usr/local/bin/findmy2mqtt"

# Systemweite Verzeichnisse, Benutzer und systemd benötigen root-Rechte.
if [[ "${EUID}" -ne 0 ]]; then
  echo "Please run this installer as root: sudo ./install.sh" >&2
  exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 is required." >&2
  exit 1
fi

python3 - <<'PY_VERSION'
import sys
if sys.version_info < (3, 10):
    raise SystemExit("Python >= 3.10 is required.")
PY_VERSION

if ! python3 -m venv --help >/dev/null 2>&1; then
  echo "Python venv support is required (on Raspberry Pi OS: sudo apt install python3-venv)." >&2
  exit 1
fi

# Ein eigener Systembenutzer begrenzt den Zugriff auf Apple-Credentials und Sessions.
if ! id "${APP_USER}" >/dev/null 2>&1; then
  useradd \
    --system \
    --home-dir "${STATE_DIR}" \
    --create-home \
    --shell /usr/sbin/nologin \
    "${APP_USER}"
fi

# Programmcode bleibt root-owned; nur State/Credentials werden dem Service-User gegeben.
install -d -o root -g root -m 0755 \
  "${APP_DIR}" \
  "${APP_DIR}/lib"

install -d -o root -g "${APP_USER}" -m 0750 \
  "${CONF_DIR}"

install -d -o "${APP_USER}" -g "${APP_USER}" -m 0750 \
  "${STATE_DIR}" \
  "${STATE_DIR}/sessions" \
  "${STATE_DIR}/credentials"

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

install -o root -g root -m 0755 \
  "${SCRIPT_DIR}/findmy2mqtt.py" \
  "${APP_DIR}/findmy2mqtt.py"

# Alte Modulstände werden entfernt, damit bei Updates keine nicht mehr
# benötigten Python-Dateien aus früheren Versionen liegen bleiben.
find "${APP_DIR}/lib" \
  -mindepth 1 \
  -maxdepth 1 \
  -type f \
  -name '*.py' \
  -delete

for module in "${SCRIPT_DIR}"/lib/*.py; do
  install -o root -g root -m 0644 \
    "${module}" \
    "${APP_DIR}/lib/$(basename "${module}")"
done

install -o root -g root -m 0644 \
  "${SCRIPT_DIR}/requirements.txt" \
  "${APP_DIR}/requirements.txt"

install -o root -g root -m 0644 \
  "${SCRIPT_DIR}/LICENSE" \
  "${APP_DIR}/LICENSE"

install -o root -g root -m 0644 \
  "${SCRIPT_DIR}/findmy2mqtt.service" \
  "${SERVICE_FILE}"

if [[ ! -f "${CONF_DIR}/config.json" ]]; then
  install -o root -g "${APP_USER}" -m 0640 \
    "${SCRIPT_DIR}/config.json.example" \
    "${CONF_DIR}/config.json"
  echo "Created ${CONF_DIR}/config.json from example."
else
  # Vorhandene Accounts, Passwörter und MQTT-Einstellungen werden bei Updates behalten.
  echo "Keeping existing ${CONF_DIR}/config.json."
fi

# Alle pip-Pakete bleiben im eigenen venv; das OS-Python wird nicht verändert.
if [[ ! -x "${APP_DIR}/venv/bin/python" ]]; then
  python3 -m venv "${APP_DIR}/venv"
fi

"${APP_DIR}/venv/bin/python" -m pip install --upgrade pip
"${APP_DIR}/venv/bin/pip" install -r "${APP_DIR}/requirements.txt"

# Erst nach erfolgreicher Python-Installation wird der kurze Systembefehl ersetzt.

# Der Wrapper versteckt Python-Pfad und Standard-Config. --config bleibt nur
# für ausdrücklich abweichende Konfigurationsdateien verfügbar.
cat > "${COMMAND_FILE}" <<'EOF_WRAPPER'
#!/usr/bin/env bash
# Copyright (c) 2026 Andreas Planer
# Repository: https://github.com/next81/findmy2mqtt
# Licensed under the PolyForm Strict License 1.0.0
# https://polyformproject.org/licenses/strict/1.0.0
set -euo pipefail

PYTHON="/opt/findmy2mqtt/venv/bin/python"
PROGRAM="/opt/findmy2mqtt/findmy2mqtt.py"
SERVICE_USER="findmy2mqtt"

if [[ "${EUID}" -eq 0 ]]; then
  exec runuser -u "${SERVICE_USER}" -- "${PYTHON}" "${PROGRAM}" "$@"
fi

if [[ "$(id -un)" == "${SERVICE_USER}" ]]; then
  exec "${PYTHON}" "${PROGRAM}" "$@"
fi

echo "Run findmy2mqtt with sudo, e.g.: sudo findmy2mqtt devices person1" >&2
exit 1
EOF_WRAPPER
chmod 0755 "${COMMAND_FILE}"
chown root:root "${COMMAND_FILE}"

systemctl daemon-reload

cat <<EOF_SUMMARY

findmy2mqtt installed.

Default configuration:
  ${CONF_DIR}/config.json

Typical commands:
  sudo findmy2mqtt set-password person1
  sudo findmy2mqtt auth person1
  sudo findmy2mqtt devices person1
  sudo findmy2mqtt locate person1 "Person1 iPhone"
  sudo findmy2mqtt locate person1 DEVICE_ID
  sudo findmy2mqtt once

For a non-default configuration only:
  sudo findmy2mqtt --config /path/to/config.json devices person1

After configuration and authentication:
  sudo systemctl enable --now findmy2mqtt

Logs:
  journalctl -u findmy2mqtt -f
EOF_SUMMARY
