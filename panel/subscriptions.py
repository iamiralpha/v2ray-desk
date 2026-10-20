"""Bounded subscription downloads and transactional source reconciliation."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import ssl
from urllib.parse import urljoin, urlsplit

import certifi
import aiohttp
from yarl import URL

from .parser import ConfigError, import_lines, parse_uri
from .tester import public_address
from .http_probe import BodyTooLarge, read_limited, run_bounded

LIMIT = 4 * 1024 * 1024
DOWNLOAD_TIMEOUT = 45
# A bounded resolver pool keeps an OS DNS stall out of the HTTP worker's deadline.
DNS_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="subscription-dns")


def validate_url(url):
    if not isinstance(url, str) or len(url) > 4096 or any(ord(c) < 33 for c in url):
        raise ConfigError("Subscription URL is invalid.")
    try:
        parts = urlsplit(url)
        if (parts.scheme not in {"http", "https"} or not parts.hostname or
                parts.username is not None or parts.password is not None or parts.fragment):
            raise ValueError()
        port = parts.port if parts.port is not None else (443 if parts.scheme == "https" else 80)
        if not 1 <= port <= 65535:
            raise ValueError()
    except ValueError:
        raise ConfigError("Subscription URL is invalid.") from None
    return parts, port


def fetch_subscription(url):
    try:
        return run_bounded(_download(url), DOWNLOAD_TIMEOUT)
    except ConfigError:
        raise
    except TimeoutError:
        raise ConfigError("Subscription download timed out.") from None
    except BodyTooLarge:
        raise ConfigError("Subscription exceeds 4 MB.") from None
    except Exception:
        # URLs can contain credentials; never include the network exception.
        raise ConfigError("Subscription download failed.") from None


async def _download(url):
    context = ssl.create_default_context(cafile=certifi.where())
    async with aiohttp.ClientSession(trust_env=False, auto_decompress=False,
                                    timeout=aiohttp.ClientTimeout(total=None, sock_connect=6, sock_read=12),
                                    connector=aiohttp.TCPConnector(ssl=context)) as client:
        for redirect in range(5):
            parts, port = validate_url(url)
            address = await asyncio.get_running_loop().run_in_executor(DNS_POOL, public_address, parts.hostname, port)
            host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
            target = URL(url, encoded=True).with_host(address)
            options = {"server_hostname": parts.hostname} if parts.scheme == "https" else {}
            async with client.get(target, headers={
                    "Host": f"{host}:{port}", "User-Agent": "V2Ray-Desk/1.3",
                    "Accept": "text/plain", "Accept-Encoding": "identity",
                }, allow_redirects=False, **options) as response:
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
                return (await read_limited(response, LIMIT)).decode("utf-8-sig")


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
    rejected = {row[0] for row in db.execute("SELECT fingerprint FROM rejected_configs")}
    count = db.execute("SELECT COUNT(*) FROM configs").fetchone()[0]
    for fingerprint, item in parsed.items():
        if fingerprint in rejected:
            continue
        row = db.execute("SELECT id FROM configs WHERE fingerprint=?", (fingerprint,)).fetchone()
        if row:
            config_id = row[0]
        else:
            if count + added >= 10000:
                raise ConfigError("Configuration limit (10,000) reached.")
            config_id = db.execute(
                "INSERT INTO configs(uri,name,protocol,host,port,fingerprint,transport,security,manual) VALUES (?,?,?,?,?,?,?,?,0)",
                [item[k] for k in ("uri", "name", "protocol", "host", "port", "fingerprint", "transport", "security")]).lastrowid
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
