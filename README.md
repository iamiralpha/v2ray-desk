# V2Ray Desk

A private, self-hosted configuration manager for Ubuntu with complete English and Persian interfaces.

[راهنمای فارسی](README.fa.md)

## Features

- Password-protected dashboard, secure sessions, CSRF protection and login rate limiting.
- Bulk import of VLESS, VMess, Trojan and Shadowsocks links from text or files, including Base64 subscription text.
- Subscription URL management: add, edit, refresh individually or together, and delete with optional removal of source-only configurations.
- Real Delay through an isolated Xray process: three HTTPS requests, median successful latency and success count.
- Exit-country detection, selectable country filtering, grouping, search and delay sorting.
- Gemini HTTP check through the same proxy: 200, 403, 429, sign-in redirect and connection failures.
- Edit, copy, export and delete configurations, including nonresponding configurations in the current filter.
- SSH menu for panel details, random/manual port selection, port checks, random/manual login paths, passwords and service control.
- Random 10- or 20-character paths, a generated password shown once, HTTPS and automatic service startup.

## Install on Ubuntu

Requires Ubuntu 22.04 or newer, x86_64 or ARM64, root access, and outbound access to Ubuntu packages, PyPI and GitHub.

Download/extract this repository, then run:

```bash
sudo bash install.sh
```

Enter the server IP or domain. Choose an automatic or manual password.
The installer selects a free random HTTPS port and a random 20-character login path,
and prints the URL and initial password. It installs a dedicated Xray v26.9.9 binary,
verifies its SHA-256, and starts the panel under a restricted service account.

To select the port and path during installation:

```bash
sudo PANEL_ADDRESS=panel.example.com PANEL_HTTPS_PORT=8443 \
  PANEL_BASE_PATH=random10 bash install.sh

sudo PANEL_ADDRESS=panel.example.com PANEL_HTTPS_PORT=9443 \
  PANEL_BASE_PATH=MyPrivateDashboard PANEL_PASSWORD_MODE=manual bash install.sh
```

`PANEL_BACKEND_PORT` defaults to `18080` and is bound to loopback only.
Existing Nginx virtual hosts are preserved. A new Nginx installation does not bind port 80.
The installer does not change firewall rules; allow the printed HTTPS port in UFW and any provider firewall.

The included TLS certificate is self-signed, so a browser trust warning is expected.
The installer prints its SHA-256 fingerprint. For a domain with a trusted certificate,
replace `/etc/v2ray-desk/cert.pem` and `/etc/v2ray-desk/key.pem`, then reload Nginx.

## SSH management

```bash
sudo v2ray-desk
```

The interactive menu supports:

| Action | Non-interactive command |
| --- | --- |
| Show URL, ports, path and service status | `sudo v2ray-desk info` |
| Check current port and local HTTPS | `sudo v2ray-desk check` |
| Check another port | `sudo v2ray-desk check 8443` |
| Choose a random free port | `sudo v2ray-desk set-port random` |
| Set a specific port | `sudo v2ray-desk set-port 8443` |
| Generate a 10-character path | `sudo v2ray-desk set-path random10` |
| Generate a 20-character path | `sudo v2ray-desk set-path random20` |
| Set a custom path | `sudo v2ray-desk set-path MyPrivateDashboard` |
| Generate and display a new password | `sudo v2ray-desk password random` |
| Enter and display a new password | `sudo v2ray-desk password` |

Changing a port validates Nginx before reloading it. If UFW is already active,
the SSH manager adds a rule for the new panel port and preserves existing rules.
Provider firewalls may still require a separate change. A listening port and a successful
server-side connection do not prove that every external client can reach it.

Passwords are hashed with scrypt. They are shown when set and cannot be recovered later.
Use password reset if needed; changing the password invalidates every existing session.
The random path supplements password authentication.

## What the tests mean

Real Delay is the elapsed time of a complete HTTPS request to
`https://www.cloudflare.com/cdn-cgi/trace` through the selected proxy, including connection
setup, proxy negotiation, TLS and response transfer. It is neither ICMP ping nor a TCP port check.
Xray startup time is excluded. Three independent requests are made and the median of successful
ones is displayed alongside the success count.

The measurement runs from your Ubuntu server. Performance from your phone or home connection can differ.
The country comes from the actual exit IP reported by Cloudflare, not from the configuration's name.
Untested or nonresponding configurations have an unknown country. A failure to reach Cloudflare
may be target-specific, so retest before deleting valuable configurations.

Gemini is checked separately at `https://gemini.google.com/app` through the same Xray proxy.
HTTP 403 is reported exactly; it does not determine whether the cause is geography, bot detection
or another restriction. HTTP 200 means the web endpoint responded; it does not guarantee access
with a particular Google account or subscription.

No direct outbound fallback is included in the test configuration.
Engine failures and unsupported configurations are kept separate from nonresponding connections.
The bulk-delete action for nonresponding configurations never deletes untested or engine-error rows.
With country grouping enabled, delay sorting is applied within each group; disable grouping for global sorting.

## Supported formats and limits

- VLESS and VMess with standard UUIDs; Trojan; AEAD and 2022 Shadowsocks.
- TCP, WebSocket, gRPC, HTTPUpgrade and basic XHTTP, with TLS or REALITY where compatible.
- Plain link lists and Base64 subscription content; automatic fetching of subscription URLs is not included.
- Up to 2,000 links per import, 10,000 stored configurations, 4 MB request bodies and three concurrent tests.
- Unsupported: arbitrary Xray JSON, Shadowsocks plugins, KCP/QUIC, custom XHTTP `extra`,
  legacy VMess with nonzero alterId, and disabled TLS certificate verification.
- Local, private and reserved server addresses are rejected. The resolved public address is pinned during a test.
- Changing configurations is temporarily disabled during an active test batch.

## Operations and data

```bash
sudo systemctl status v2ray-desk
sudo systemctl restart v2ray-desk
sudo journalctl -u v2ray-desk -n 100 --no-pager
```

Application: `/opt/v2ray-desk`  
Data: `/var/lib/v2ray-desk/panel.sqlite3`  
Panel address settings: `/etc/v2ray-desk/config.json`  
Nginx site: `/etc/nginx/conf.d/v2ray-desk.conf`

Configuration links contain credentials and are stored unencrypted in the private data directory.
Back up the complete data directory with the service stopped, or use SQLite's online backup API.
Do not copy live SQLite files without their WAL state. Keep backups private.
Run one web process: the test queue is held in memory. Restarted tests become explicit errors and can be retried.
Sessions expire after eight hours. Login attempts are limited to five within fifteen minutes.
Behind the supplied reverse proxy, this limit is shared for this single-admin panel.

## Development and verification

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt pytest
python manage.py init --generate
PANEL_COOKIE_SECURE=0 python manage.py serve
```

`PANEL_COOKIE_SECURE=0` is for localhost development only.

```bash
python tools/install_xray.py --destination /tmp/v2ray-desk-xray
XRAY_TEST_BIN=/tmp/v2ray-desk-xray python -m pytest -q
```

Tests cover URI parsing, authentication, CSRF, English diagnostics, path-aware URLs,
safe bulk deletion, password invalidation, port validation and actual Xray proxy roundtrips.
Integration fixtures use synthetic local HTTP endpoints to deterministically check 403 and wrong-password behavior;
production probes use HTTPS.

The project contains no server credentials, private deployment paths, user configurations, or database files.
The bundled [Vazirmatn font](https://github.com/rastikerdar/vazirmatn) is OFL licensed;
see `panel/static/FONT-LICENSE.txt`.
Xray is downloaded separately from its [official release](https://github.com/XTLS/Xray-core/releases/tag/v26.9.9).

## Subscription URLs

Open **Subscriptions**, enter a name and an HTTP/HTTPS URL, then choose **Add & sync**.
Plain-text and Base64 lists are accepted (4 MB and 2,000 links per subscription, up to 100 subscriptions).
Refresh can be run individually or for all enabled subscriptions; there is no scheduled polling.
Changes to the source are reconciled without duplicating configurations. Manual imports, edited
configurations, and configurations shared with another subscription are preserved. A failed or
unsupported-only download leaves the previous list intact. Unsupported links are counted.
Deleting a subscription keeps its configurations by default; the optional removal deletes only
its unedited, source-only configurations. Deleted configurations may return on the next refresh
if the source still contains them. Subscription URLs may contain secrets and stay in the private
database; they are shown only in the authenticated edit form. Public HTTP/HTTPS destinations
only: private/local addresses, insecure TLS, and HTTPS-to-HTTP redirects are rejected.
