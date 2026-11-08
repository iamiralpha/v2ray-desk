"""Outbound-only worker that tests primary-approved links from this machine."""
import hashlib
import hmac
import json
import re
import signal
import socket
import ssl
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

import requests

from .parser import ConfigError, parse_uri
from .store import connect, initialize
from .tester import MAX_WORKERS, TestManager

MAX_BATCH = 128


def controller_url(value):
    if not isinstance(value, str) or any(ord(c) < 33 for c in value):
        raise ValueError("Invalid controller URL.")
    try:
        parsed = urlsplit(value)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.query or parsed.fragment
                or parsed.port is not None and not 1 <= parsed.port <= 65535):
            raise ValueError()
    except ValueError:
        raise ValueError("Use the HTTPS panel URL without credentials or a query.") from None
    return value.rstrip("/")


def fetch_pinned_certificate(url, fingerprint):
    """Verify the operator-provided certificate fingerprint before sending a token."""
    url = controller_url(url)
    digest = fingerprint.strip() if isinstance(fingerprint, str) else ""
    if "=" in digest:
        label, digest = digest.split("=", 1)
        if not re.fullmatch(r"(?:sha\s*256\s+)?fingerprint", label.strip(), re.IGNORECASE):
            raise ValueError("Enter the panel's SHA-256 certificate fingerprint.")
    digest = digest.replace(":", "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("Enter the panel's SHA-256 certificate fingerprint.")
    parsed = urlsplit(url)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    with socket.create_connection((parsed.hostname, parsed.port or 443), timeout=10) as raw:
        with context.wrap_socket(raw, server_hostname=parsed.hostname) as tls:
            certificate = tls.getpeercert(binary_form=True)
    if not certificate or not hmac.compare_digest(hashlib.sha256(certificate).hexdigest(), digest):
        raise ValueError("The panel certificate fingerprint does not match.")
    return ssl.DER_cert_to_PEM_cert(certificate)


def validate_batch(configs):
    if not isinstance(configs, list) or len(configs) > MAX_BATCH:
        raise ValueError("Invalid controller batch.")
    ids = set()
    for row in configs:
        if (not isinstance(row, dict) or set(row) != {"id", "uri"} or type(row["id"]) is not int
                or row["id"] < 1 or row["id"] in ids or not isinstance(row["uri"], str)
                or len(row["uri"]) > 16384):
            raise ValueError("Invalid controller configuration.")
        ids.add(row["id"])


def run_batch(configs, executable, workers=32, stop=None):
    """Use the same bounded Xray batch and Real Delay algorithm as the panel."""
    validate_batch(configs)
    if type(workers) is not int or not 1 <= workers <= MAX_WORKERS:
        raise ValueError("Invalid worker count.")
    stop = stop or threading.Event()
    results, local_to_remote = {}, {}
    with tempfile.TemporaryDirectory(prefix="v2ray-desk-node-") as folder:
        db_path = initialize(folder)
        with connect(db_path) as db:
            for row in configs:
                try:
                    parsed = parse_uri(row["uri"])
                    cursor = db.execute("""INSERT INTO configs(uri,name,protocol,host,port,fingerprint)
                        VALUES(?,?,?,?,?,?)""", [parsed[k] for k in ("uri", "name", "protocol", "host", "port", "fingerprint")])
                    local_to_remote[cursor.lastrowid] = row["id"]
                except ConfigError:
                    results[row["id"]] = {"id": row["id"], "status": "invalid", "latency_ms": None}
        if local_to_remote and not stop.is_set():
            manager = TestManager(db_path, executable, workers=workers)
            manager.start(list(local_to_remote), {"workers": workers, "timeout": 5, "samples": 2, "target": "google",
                                                 "gemini": False, "country": False, "auto_delete_failed": False})
            while manager.snapshot()["running"]:
                if stop.is_set():
                    manager.cancel()
                    time.sleep(.05)
                else:
                    stop.wait(.1)
            if not stop.is_set():
                with connect(db_path) as db:
                    for row in db.execute("SELECT id,status,latency_ms FROM configs"):
                        status = row["status"] if row["status"] in {"healthy", "failed", "invalid", "error"} else "error"
                        remote = local_to_remote[row["id"]]
                        results[remote] = {"id": remote, "status": status,
                                           "latency_ms": row["latency_ms"] if status == "healthy" else None}
    if stop.is_set():
        return []
    return [results[row["id"]] for row in configs]


class FingerprintAdapter(requests.adapters.HTTPAdapter):
    """Check the originally verified leaf certificate on every TLS connection."""
    def __init__(self, fingerprint):
        self.fingerprint = fingerprint
        super().__init__()

    def init_poolmanager(self, connections, maxsize, block=False, **kwargs):
        kwargs["assert_fingerprint"] = self.fingerprint
        return super().init_poolmanager(connections, maxsize, block=block, **kwargs)

    def build_connection_pool_key_attributes(self, request, verify, cert=None):
        host, options = super().build_connection_pool_key_attributes(request, verify, cert)
        options["assert_fingerprint"] = self.fingerprint
        return host, options


class NodeClient:
    def __init__(self, url, token, certificate):
        self.url = controller_url(url)
        if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
            raise ValueError("Invalid node token.")
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.verify = str(certificate)
        bundle = Path(certificate).read_text(encoding="ascii")
        leaf = bundle.split("-----END CERTIFICATE-----", 1)[0] + "-----END CERTIFICATE-----\n"
        digest = hashlib.sha256(ssl.PEM_cert_to_DER_cert(leaf)).hexdigest()
        self.session.mount("https://", FingerprintAdapter(digest))
        self.session.headers.update({"Authorization": "Bearer " + token, "Accept": "application/json"})

    def post(self, route, payload):
        with self.session.post(self.url + route, json=payload, timeout=(10, 30),
                               allow_redirects=False, stream=True) as response:
            if not 200 <= response.status_code < 300:
                raise ValueError(f"Controller returned HTTP {response.status_code}.")
            data = bytearray()
            for chunk in response.iter_content(16384):
                data.extend(chunk)
                if len(data) > 4 * 1024 * 1024:
                    raise ValueError("Controller response is too large.")
            return json.loads(data)

    def once(self, executable, workers, stop):
        batch = self.post("/api/node/pull", {})
        if not isinstance(batch, dict) or set(batch) != {"lease", "configs"}:
            raise ValueError("Invalid controller response.")
        configs, lease = batch["configs"], batch["lease"]
        validate_batch(configs)
        if not configs:
            if lease is not None:
                raise ValueError("Invalid empty controller batch.")
            return False
        if not isinstance(lease, str) or not re.fullmatch(r"[A-Za-z0-9_-]{32}", lease):
            raise ValueError("Invalid controller lease.")
        outcomes = run_batch(configs, executable, workers, stop)
        if not stop.is_set():
            self.post("/api/node/report", {"lease": lease, "results": outcomes})
        return True


def run(config_path, executable):
    config = json.loads(Path(config_path).read_text(encoding="utf-8"))
    client = NodeClient(config["url"], config["token"], config["certificate"])
    workers = config.get("workers", 32)
    if type(workers) is not int or not 1 <= workers <= MAX_WORKERS:
        raise ValueError("Invalid worker count.")
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    while not stop.is_set():
        try:
            had_work = client.once(executable, workers, stop)
            if not had_work:
                stop.wait(30)
        except (requests.RequestException, OSError, ValueError, KeyError):
            # Never print tokens, configuration URLs, or raw network exceptions.
            print("Node connection or batch failed; retrying shortly.", flush=True)
            stop.wait(30)
