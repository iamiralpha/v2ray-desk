"""Bounded subscription downloads and transactional source reconciliation."""
import ssl
import time
from urllib.parse import urljoin, urlsplit

import certifi
import urllib3

from .parser import ConfigError, import_lines, parse_uri
from .tester import public_address

LIMIT = 4 * 1024 * 1024


def validate_url(url):
    if not isinstance(url, str) or len(url) > 4096 or any(ord(c) < 33 for c in url):
        raise ConfigError("Subscription URL is invalid.")
    try:
        parts = urlsplit(url)
        if (parts.scheme not in {"http", "https"} or not parts.hostname or
                parts.username is not None or parts.password is not None or parts.fragment):
            raise ValueError()
        port = parts.port or (443 if parts.scheme == "https" else 80)
        if not 1 <= port <= 65535:
            raise ValueError()
    except ValueError:
        raise ConfigError("Subscription URL is invalid.") from None
    return parts, port


def fetch_subscription(url):
    started = time.monotonic()
    try:
        for redirect in range(5):
            parts, port = validate_url(url)
            address = public_address(parts.hostname, port)
            options = dict(port=port, timeout=urllib3.Timeout(connect=6, read=12), retries=False)
            if parts.scheme == "https":
                pool = urllib3.HTTPSConnectionPool(
                    address, server_hostname=parts.hostname, assert_hostname=parts.hostname,
                    cert_reqs=ssl.CERT_REQUIRED, ca_certs=certifi.where(), **options)
            else:
                pool = urllib3.HTTPConnectionPool(address, **options)
            host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
            target = parts.path or "/"
            if parts.query:
                target += "?" + parts.query
            with pool:
                response = pool.request("GET", target, headers={
                    "Host": f"{host}:{port}", "User-Agent": "V2Ray-Desk/1.0",
                    "Accept": "text/plain", "Accept-Encoding": "identity",
                }, redirect=False, preload_content=False)
                try:
                    if response.status in {301, 302, 303, 307, 308}:
                        location = response.headers.get("Location")
                        if not location or redirect == 4:
                            raise ConfigError("Subscription redirect limit reached.")
                        next_url = urljoin(url, location)
                        if parts.scheme == "https" and urlsplit(next_url).scheme != "https":
                            raise ConfigError("Subscription HTTPS downgrade was blocked.")
                        url = next_url
                        continue
                    if response.status != 200:
                        raise ConfigError(f"Subscription HTTP {response.status}.")
                    chunks, total = [], 0
                    while True:
                        if time.monotonic() - started > 45:
                            raise ConfigError("Subscription download timed out.")
                        chunk = response.read(65536, decode_content=True)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > LIMIT:
                            raise ConfigError("Subscription exceeds 4 MB.")
                        chunks.append(chunk)
                    return b"".join(chunks).decode("utf-8-sig")
                finally:
                    response.close()
    except ConfigError:
        raise
    except Exception:
        # URLs can contain credentials; never include the network exception.
        raise ConfigError("Subscription download failed.") from None


def reconcile(db, subscription_id, text):
    parsed, invalid = {}, 0
    for _, line in import_lines(text):
        try:
            item = parse_uri(line)
            parsed[item["fingerprint"]] = item
        except ConfigError:
            invalid += 1
    if not parsed:
        raise ConfigError("Subscription contains no supported configurations.")
    previous = {r[0] for r in db.execute(
        "SELECT config_id FROM subscription_configs WHERE subscription_id=?", (subscription_id,))}
    current, added = set(), 0
    count = db.execute("SELECT COUNT(*) FROM configs").fetchone()[0]
    for fingerprint, item in parsed.items():
        row = db.execute("SELECT id FROM configs WHERE fingerprint=?", (fingerprint,)).fetchone()
        if row:
            config_id = row[0]
        else:
            if count + added >= 10000:
                raise ConfigError("Configuration limit (10,000) reached.")
            config_id = db.execute(
                "INSERT INTO configs(uri,name,protocol,host,port,fingerprint,manual) VALUES (?,?,?,?,?,?,0)",
                [item[k] for k in ("uri", "name", "protocol", "host", "port", "fingerprint")]).lastrowid
            added += 1
        current.add(config_id)
    db.execute("DELETE FROM subscription_configs WHERE subscription_id=?", (subscription_id,))
    db.executemany("INSERT INTO subscription_configs VALUES (?,?)", [(subscription_id, i) for i in current])
    removed = prune(db, previous - current)
    db.execute("""UPDATE subscriptions SET updated_at=strftime('%Y-%m-%dT%H:%M:%SZ','now'),
                  error=NULL,invalid_count=? WHERE id=?""", (invalid, subscription_id))
    return dict(added=added, removed=removed, total=len(current), invalid=invalid)


def prune(db, candidates):
    removed = 0
    for config_id in candidates:
        removed += db.execute("""DELETE FROM configs WHERE id=? AND manual=0 AND NOT EXISTS
            (SELECT 1 FROM subscription_configs WHERE config_id=configs.id)""", (config_id,)).rowcount
    return removed
