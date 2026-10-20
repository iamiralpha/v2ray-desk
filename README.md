# V2Ray Desk

A private, self-hosted configuration manager for Ubuntu with complete English and Persian interfaces.

[راهنمای فارسی](README.fa.md)

## Features

- Password-protected dashboard, secure sessions, CSRF protection and login rate limiting.
- Bulk import of VLESS, VMess, Trojan and Shadowsocks links from text or files, including Base64 subscription text.
- Subscription URL management: add, edit, refresh individually or together, and delete with optional removal of source-only configurations.
- v2rayN-style Real Delay: two GETs over a reusable connection, minimum latency and 1–32 parallel workers with a shared Xray core.
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
and prints the URL and initial password. It installs a dedicated Xray v26.10.17 binary,
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

Real Delay follows the measurement algorithm in
[v2rayN ConnectionHandler](https://github.com/2dust/v2rayN/blob/57f17d5939e88c8e643ab49b90869d637765e75a/v2rayN/ServiceLib/Handler/ConnectionHandler.cs#L69):
two complete GET requests using one reusable HTTP client through the selected proxy,
with a 100 ms wait after each request. The lowest positive integer duration is displayed.
The default target is `https://www.google.com/generate_204`; Gstatic and Cloudflare 204
endpoints are also available. The default five-second deadline covers both requests and
waits, including stalled TLS handshakes. Cancellation closes the underlying socket instead
of waiting for a TLS shutdown. A failed or timed-out pair is unsuccessful. Hover over a delay to see both timings
and HTTP codes. As in v2rayN, any completed HTTP response can be timed, including 403;
this is a latency measurement, not proof that a particular service is accessible.
Requests follow at most ten redirects and response bodies are limited to 1 MB.

Single and bulk tests use the same timer. Xray startup, country lookup and Gemini checks
are excluded from the delay. The measurement runs from the Ubuntu server; its route, load,
HTTP runtime and network differ from v2rayN on a phone or PC, so values need not match.
Older saved results are marked **Previous measurement** until retested.

Choose 1–32 parallel tests (default 8). Bulk tests share one Xray process per bounded batch,
with a separate loopback SOCKS port and a dedicated outbound route for every configuration.
Only the selected number of workers are active; the queue supports 10,000 stored configurations.
The UI fetches changed rows and renders 50 rows per page. Stop leaves queued rows unchanged
and promptly cancels in-flight Real Delay, country and Gemini HTTP requests. DNS resolution
or Xray startup/validation may still need to finish before the batch ends. Use 1–4 workers
when comparing latency, since excess concurrency can increase host load and contention.

Country is looked up separately from the exit IP reported by Cloudflare, never inferred from
the configuration's name. A missing country does not invalidate a successful delay test.
Failures can be target-specific, so retest before deleting valuable configurations.

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
- Plain link lists, Base64 content and managed subscription URLs with manual refresh.
- Up to 2,000 links per import, 10,000 stored configurations, 4 MB request bodies and 1–32 concurrent tests.
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
On Linux/POSIX, initialization enforces `0700` on the data directory and `0600` on the
SQLite database and its journal, WAL and shared-memory files, including existing files.
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
node --test tests/test_frontend.js
```

The UI regression tests use Node.js 24 in CI and require no npm packages.
Tests cover URI parsing, authentication, CSRF, English diagnostics, path-aware URLs,
safe bulk deletion, password invalidation, port validation and actual Xray proxy roundtrips.
They also cover slow-response deadlines, cancellation, compressed-body limits, private file
permissions on POSIX, country-filter resets and subscription controls during pending requests.
Integration fixtures use synthetic local HTTP endpoints to deterministically check 403 and wrong-password behavior;
production probes use HTTPS.

The project contains no server credentials, private deployment paths, user configurations, or database files.
The bundled [Vazirmatn font](https://github.com/rastikerdar/vazirmatn) is OFL licensed;
see `panel/static/FONT-LICENSE.txt`.
Xray is downloaded separately from its [official release](https://github.com/XTLS/Xray-core/releases/tag/v26.10.17).

## Subscription URLs

Open **Subscriptions**, enter a name and an HTTP/HTTPS URL, then choose **Add & sync**.
Plain-text and Base64 lists are accepted (4 MB and 2,000 links per subscription, up to 100 subscriptions).
Each download has a 45-second total deadline covering DNS, redirects, response headers and body.
The size limit applies to both transferred and decompressed data.
Refresh can be run individually or for all enabled subscriptions; there is no scheduled polling.
Changes to the source are reconciled without duplicating configurations. Manual imports, edited
configurations, and configurations shared with another subscription are preserved. A failed or
unsupported-only download leaves the previous list intact. Unsupported links are counted.
Deleting a subscription keeps its configurations by default; the optional removal deletes only
its unedited, source-only configurations. Deleted configurations may return on the next refresh
if the source still contains them. Subscription URLs may contain secrets and stay in the private
database; they are shown only in the authenticated edit form. Public HTTP/HTTPS destinations
only: private/local addresses, insecure TLS, and HTTPS-to-HTTP redirects are rejected.

## Results table

The fluid workspace uses the available display width. A compact, v2rayN-inspired table
separates name, protocol, address, port, transport, TLS, country, delay, status and Gemini result.
Click a column header to sort in either direction; unmeasured delays stay at the bottom.
Country grouping is optional. Shift-click checkboxes to select a range; double-click a row
to edit. Table headers stay visible while scrolling, and smaller displays scroll inside the
table instead of widening the entire page. All controls are available in English and Persian.


## Additional tools

- Test scope: all filtered/selected rows, untested rows, unsuccessful rows, or both. Invalid settings are excluded from automatic retries; fix them first, then use a row test or All.
- CSV reports preserve the visible/selected order and include delay, country, Gemini HTTP and error details. Proxy credentials and full configuration links are excluded; spreadsheet formulas in imported names are escaped.
- Invalid UUID, REALITY keys/short IDs, Vision combinations and Shadowsocks 2022 keys are rejected with a specific explanation. Saved configurations also pass Xray's `run -test` check. Invalid settings are separate from a timeout or test environment error; one invalid node does not fail the entire batch.
- The web footer and SSH `info` command show the installed Xray version.

## Fixes in 1.3

- Subscription downloads and metadata checks have enforced total deadlines; Stop also cancels country and Gemini requests.
- Malformed VMess links and invalid DNS labels produce individual errors without abandoning valid neighboring configurations. Legacy Base64 Shadowsocks passwords retain literal special characters.
- If a selected country disappears, the filter, table and selection reset together. Subscription controls stay disabled during an operation and recover after failures.
- Initialization repairs the private data-directory and SQLite file permissions described above.

The pinned official Xray **v26.10.17** release is marked **prerelease** by upstream (checked 2026-10-20). The GitHub latest stable endpoint currently points to v26.3.27. This project uses the newest dated official release with pinned SHA-256 checks and integration tests; it does not call it a stable release.
