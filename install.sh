#!/usr/bin/env bash
set -euo pipefail
umask 027
if [[ $EUID -ne 0 ]]; then
  echo "Run with sudo: sudo bash install.sh"
  exit 1
fi
source /etc/os-release
if [[ "$ID" != "ubuntu" ]]; then
  echo "This installer targets Ubuntu 22.04 / 24.04 / 26.04."
  exit 1
fi
SOURCE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
APP=/opt/v2ray-desk
DATA=/var/lib/v2ray-desk
BIND_PORT="${PANEL_BACKEND_PORT:-}"
if [[ -z "$BIND_PORT" ]]; then
  if [[ -f /etc/v2ray-desk/config.json ]]; then
    BIND_PORT="$(python3 -c 'import json;print(json.load(open("/etc/v2ray-desk/config.json")).get("backend_port",18080))')"
  else
    BIND_PORT="$(python3 -c 'import sys;sys.path.insert(0,sys.argv[1]);from admin import port_free,random_port;print(18080 if port_free(18080) else random_port())' "$SOURCE")"
  fi
fi
if [[ ! "$BIND_PORT" =~ ^[0-9]{1,5}$ ]] || ((10#$BIND_PORT < 1024 || 10#$BIND_PORT > 65535)); then
  echo "Use a backend port between 1024 and 65535."
  exit 1
fi
BIND_PORT=$((10#$BIND_PORT))
PORT="${PANEL_HTTPS_PORT:-random}"
if [[ "$PORT" == "random" ]]; then
  if [[ -f /etc/v2ray-desk/config.json ]]; then
    PORT="$(python3 -c 'import json;print(json.load(open("/etc/v2ray-desk/config.json"))["port"])')"
  else
    PORT="$(python3 -c 'import sys;sys.path.insert(0,sys.argv[1]);from admin import random_port;print(random_port(exclude={int(sys.argv[2])}))' "$SOURCE" "$BIND_PORT")"
  fi
fi
PANEL_ADDRESS="${PANEL_ADDRESS:-}"
if [[ ! "$PORT" =~ ^[0-9]{1,5}$ ]] || ((10#$PORT < 1024 || 10#$PORT > 65535 || 10#$PORT == BIND_PORT)); then
  echo "Use distinct ports between 1024 and 65535."
  exit 1
fi
PORT=$((10#$PORT))
if [[ -z "$PANEL_ADDRESS" && -f /etc/v2ray-desk/config.json ]]; then
  PANEL_ADDRESS="$(python3 -c 'import json;print(json.load(open("/etc/v2ray-desk/config.json")).get("address", ""))')"
fi
if [[ -z "$PANEL_ADDRESS" ]]; then
  PANEL_ADDRESS="$(python3 -c 'import sys;sys.path.insert(0,sys.argv[1]);from admin import detect_address;print(detect_address())' "$SOURCE")"
fi
if [[ -z "$PANEL_ADDRESS" ]]; then
  if [[ ! -t 0 ]]; then
    echo "Could not detect the public IP. Set PANEL_ADDRESS to the server IP or domain and retry." >&2
    exit 1
  fi
  read -r -p "Could not detect the public IP. Enter the server IP or domain: " PANEL_ADDRESS
fi
if [[ ! "$PANEL_ADDRESS" =~ ^[a-zA-Z0-9.:-]+$ ]]; then
  echo "Invalid server IP/domain."
  exit 1
fi
PANEL_INITIAL_PASSWORD=""
PANEL_PASSWORD_SHOWN=0
show_password_on_failure() {
  local code=$?
  if ((code != 0)) && [[ -n "$PANEL_INITIAL_PASSWORD" && "$PANEL_PASSWORD_SHOWN" == 0 ]]; then
    printf '\nInstallation did not finish. The new admin password was already created:\nPanel password: %s\n' "$PANEL_INITIAL_PASSWORD" >&2
  fi
}
trap show_password_on_failure EXIT
if [[ ! -f /etc/systemd/system/v2ray-desk.service ]]; then
  if ! python3 -c 'import sys;sys.path.insert(0,sys.argv[1]);from admin import port_free;sys.exit(0 if port_free(int(sys.argv[2])) and port_free(int(sys.argv[3])) else 1)' "$SOURCE" "$PORT" "$BIND_PORT"; then
    echo "A selected port is already in use. Choose PANEL_HTTPS_PORT and PANEL_BACKEND_PORT."
    exit 1
  fi
fi
export DEBIAN_FRONTEND=noninteractive
export NEEDRESTART_MODE=l
apt-get update
# On a fresh Nginx installation, start without the default port-80 site.
# Existing Nginx installations and their virtual hosts are preserved.
if ! command -v nginx >/dev/null 2>&1 && [[ ! -e /etc/nginx/nginx.conf ]]; then
  install -d -m 0755 /etc/nginx/conf.d
  cat > /etc/nginx/nginx.conf <<'NGINX'
user www-data;
worker_processes auto;
pid /run/nginx.pid;
include /etc/nginx/modules-enabled/*.conf;
events { worker_connections 1024; }
http {
    include /etc/nginx/mime.types;
    default_type application/octet-stream;
    sendfile on;
    keepalive_timeout 65;
    include /etc/nginx/conf.d/*.conf;
}
NGINX
fi
apt-get -o Dpkg::Options::="--force-confold" install -y python3 python3-venv ca-certificates nginx openssl
if ! id -u v2ray-desk >/dev/null 2>&1; then
  useradd --system --home-dir "$DATA" --shell /usr/sbin/nologin v2ray-desk
fi
install -d -m 0755 "$APP" "$APP/bin"
install -d -o v2ray-desk -g v2ray-desk -m 0700 "$DATA"
if [[ "$SOURCE" != "$APP" ]]; then
  cp -a "$SOURCE/panel" "$SOURCE/tools" "$SOURCE/manage.py" "$SOURCE/admin.py" "$SOURCE/requirements.txt" "$APP/"
fi
chown -R root:root "$APP"
chmod -R a+rX,go-w "$APP"
python3 -m venv "$APP/venv"
"$APP/venv/bin/python" -m pip install --disable-pip-version-check --upgrade pip==26.2.2
"$APP/venv/bin/pip" install --disable-pip-version-check -r "$APP/requirements.txt"
chmod -R a+rX,go-w "$APP/venv"
if [[ "${PANEL_XRAY_PREINSTALLED:-0}" != "1" ]]; then
  python3 "$APP/tools/install_xray.py" --destination "$APP/bin/xray"
fi
"$APP/bin/xray" version
if [[ "${PANEL_SKIP_INIT:-0}" != "1" ]]; then
  MODE="${PANEL_PASSWORD_MODE:-auto}"
  if [[ "$MODE" != auto && "$MODE" != manual ]]; then
    echo "PANEL_PASSWORD_MODE must be auto or manual." >&2
    exit 1
  fi
  FLAGS=(--show-password)
  if [[ "$MODE" == auto ]]; then FLAGS+=(--generate); fi
  INIT_OUTPUT="$(runuser -u v2ray-desk -- "$APP/venv/bin/python" "$APP/manage.py" init --data "$DATA" "${FLAGS[@]}")"
  while IFS= read -r line; do
    case "$line" in
      'Panel password: '*) PANEL_INITIAL_PASSWORD="${line#Panel password: }" ;;
      *) printf '%s\n' "$line" ;;
    esac
  done <<< "$INIT_OUTPUT"
  unset INIT_OUTPUT line
fi
install -d -m 0700 /etc/v2ray-desk
if [[ ! -f /etc/v2ray-desk/cert.pem ]]; then
  SAN="$(python3 -c 'import ipaddress,sys; v=sys.argv[1]
try: ipaddress.ip_address(v); print("IP:"+v)
except ValueError: print("DNS:"+v)' "$PANEL_ADDRESS")"
  openssl req -x509 -newkey rsa:3072 -sha256 -nodes -days 365 \
    -keyout /etc/v2ray-desk/key.pem -out /etc/v2ray-desk/cert.pem \
    -subj "/CN=$PANEL_ADDRESS" -addext "subjectAltName=$SAN"
  chmod 0600 /etc/v2ray-desk/key.pem
fi
python3 - "$APP/cert.sha256" <<'PY'
import hashlib, pathlib, ssl, sys
certificate = pathlib.Path('/etc/v2ray-desk/cert.pem').read_text(encoding='ascii')
path = pathlib.Path(sys.argv[1])
path.write_text(hashlib.sha256(ssl.PEM_cert_to_DER_cert(certificate)).hexdigest() + '\n', encoding='ascii')
path.chmod(0o644)
PY
cat > /etc/systemd/system/v2ray-desk.service <<EOF
[Unit]
Description=V2Ray Desk private configuration panel
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=v2ray-desk
Group=v2ray-desk
WorkingDirectory=$APP
Environment=PANEL_DATA=$DATA
Environment=XRAY_BIN=$APP/bin/xray
Environment=PANEL_COOKIE_SECURE=1
Environment=PYTHONDONTWRITEBYTECODE=1
ExecStart=$APP/venv/bin/python $APP/manage.py serve --host 127.0.0.1 --port $BIND_PORT
Restart=on-failure
RestartSec=3
TimeoutStopSec=20
KillMode=control-group
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=true
ProtectSystem=strict
ReadWritePaths=$DATA
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
ProtectKernelTunables=true
ProtectControlGroups=true
CapabilityBoundingSet=
LimitNOFILE=8192
TasksMax=512

[Install]
WantedBy=multi-user.target
EOF
"$APP/venv/bin/python" "$APP/admin.py" configure --address "$PANEL_ADDRESS" --port "$PORT" --backend-port "$BIND_PORT" --path "${PANEL_BASE_PATH:-}"
chmod 0755 "$APP/admin.py"
ln -sf "$APP/admin.py" /usr/local/bin/v2ray-desk

nginx -t
systemctl daemon-reload
if [[ "${PANEL_SKIP_START:-0}" != "1" ]]; then
  systemctl enable --now v2ray-desk
  systemctl restart v2ray-desk
fi
systemctl enable --now nginx
systemctl reload nginx
if command -v ufw >/dev/null 2>&1 && LC_ALL=C ufw status | grep -q '^Status: active'; then
  if ! ufw allow "$PORT/tcp"; then
    echo "Could not add the UFW rule; allow TCP $PORT manually." >&2
  fi
fi
printf '\n========================================\n  V2Ray Desk installation complete\n========================================\n'
"$APP/venv/bin/python" "$APP/admin.py" info
echo "A self-signed TLS certificate is installed. Verify its fingerprint before trusting it."
openssl x509 -in /etc/v2ray-desk/cert.pem -noout -fingerprint -sha256
echo "If your provider has a firewall, allow TCP $PORT there."
if [[ -n "$PANEL_INITIAL_PASSWORD" ]]; then
  printf 'Panel password: %s\n' "$PANEL_INITIAL_PASSWORD"
  PANEL_PASSWORD_SHOWN=1
else
  echo "Panel password: unchanged (existing passwords cannot be displayed)."
fi
echo "SSH management menu: sudo v2ray-desk"
