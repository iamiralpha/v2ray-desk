#!/usr/bin/env bash
set -euo pipefail
umask 027
if [[ $EUID -ne 0 ]]; then echo "Run with sudo: sudo bash install-node.sh" >&2; exit 1; fi
source /etc/os-release
if [[ "$ID" != ubuntu ]]; then echo "This installer targets Ubuntu." >&2; exit 1; fi
SOURCE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
APP=/opt/v2ray-desk-node
DATA=/var/lib/v2ray-desk-node
CONFIG=/etc/v2ray-desk-node
export DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=l
apt-get update
apt-get install -y python3 python3-venv ca-certificates
if ! id -u v2ray-desk-node >/dev/null 2>&1; then
  useradd --system --home-dir "$DATA" --shell /usr/sbin/nologin v2ray-desk-node
fi
install -d -m 0755 "$APP" "$APP/bin"
install -d -o v2ray-desk-node -g v2ray-desk-node -m 0700 "$DATA"
install -d -o root -g v2ray-desk-node -m 0750 "$CONFIG"
if [[ "$SOURCE" != "$APP" ]]; then
  cp -a "$SOURCE/panel" "$SOURCE/tools" "$SOURCE/node.py" "$SOURCE/requirements.txt" "$APP/"
fi
chown -R root:root "$APP"
chmod -R a+rX,go-w "$APP"
python3 -m venv "$APP/venv"
"$APP/venv/bin/python" -m pip install --disable-pip-version-check --upgrade pip==26.2.2
"$APP/venv/bin/pip" install --disable-pip-version-check -r "$APP/requirements.txt"
chmod -R a+rX,go-w "$APP/venv"
if [[ "${PANEL_XRAY_PREINSTALLED:-0}" != 1 ]]; then
  python3 "$APP/tools/install_xray.py" --destination "$APP/bin/xray"
fi
"$APP/bin/xray" version
if [[ ! -f "$CONFIG/config.json" || "${NODE_RECONFIGURE:-0}" == 1 ]]; then
  "$APP/venv/bin/python" "$APP/node.py" configure --data "$CONFIG"
fi
chown root:v2ray-desk-node "$CONFIG/config.json" "$CONFIG/controller.pem"
chmod 0640 "$CONFIG/config.json" "$CONFIG/controller.pem"
cat > /etc/systemd/system/v2ray-desk-node.service <<EOF
[Unit]
Description=V2Ray Desk remote test node
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=v2ray-desk-node
Group=v2ray-desk-node
WorkingDirectory=$APP
Environment=TMPDIR=$DATA
Environment=PYTHONDONTWRITEBYTECODE=1
ExecStart=$APP/venv/bin/python $APP/node.py run --data $CONFIG --xray $APP/bin/xray
Restart=on-failure
RestartSec=5
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
systemctl daemon-reload
systemctl enable --now v2ray-desk-node
systemctl restart v2ray-desk-node
echo "Node is running. It connects out to the panel and needs no inbound port."
echo "Status: sudo systemctl status v2ray-desk-node"
