import copy
import ipaddress
import json
import shutil
import socket
import statistics
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import requests

from .parser import ConfigError, parse_uri
from .store import connect

TRACE_URL = "https://www.cloudflare.com/cdn-cgi/trace"
GEMINI_URL = "https://gemini.google.com/app"


def probe_gemini(port, timeout):
    """HTTP accessibility only; never claim an account can use Gemini."""
    try:
        with requests.Session() as client:
            client.trust_env = False
            client.proxies = {"http": f"socks5h://127.0.0.1:{port}",
                              "https": f"socks5h://127.0.0.1:{port}"}
            target = GEMINI_URL
            for _ in range(6):
                with client.get(target, timeout=(timeout, timeout), stream=True, allow_redirects=False) as response:
                    code = response.status_code
                    if code in {301, 302, 303, 307, 308}:
                        target = urljoin(target, response.headers.get("Location", ""))
                        parsed = urlsplit(target)
                        if parsed.scheme != "https" or not (parsed.hostname or "").endswith(".google.com"):
                            return "redirect", code
                        if parsed.hostname == "accounts.google.com":
                            return "login", code
                        continue
                    return {403: "blocked", 429: "limited", 200: "http200"}.get(code, "other"), code
            return "redirect", code
    except requests.RequestException:
        return "unreachable", None


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def public_address(host, port):
    """Resolve once and pin that address, preventing DNS rebinding into the LAN."""
    try:
        addresses = list(dict.fromkeys(entry[4][0] for entry in socket.getaddrinfo(
            host, port, type=socket.SOCK_STREAM)))
    except socket.gaierror:
        raise ConfigError("نام دامنهٔ سرور پیدا نشد.") from None
    if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
        raise ConfigError("تست آدرس‌های محلی، خصوصی و رزروشده مجاز نیست.")
    # Keep the resolver's order; IPv6-only hosts are supported.
    return addresses[0]


def make_config(parsed, address, socks_port):
    outbound = copy.deepcopy(parsed["outbound"])
    server = outbound["settings"].get("vnext", outbound["settings"].get("servers"))[0]
    server["address"] = address
    # Pin transport Host before replacing the server hostname with an IP.
    transport = outbound.get("streamSettings", {})
    if transport.get("network") == "ws":
        transport["wsSettings"].setdefault("headers", {}).setdefault("Host", parsed["host"])
    for key in ("httpupgradeSettings", "xhttpSettings"):
        if key in transport and not transport[key].get("host"):
            transport[key]["host"] = parsed["host"]
    if transport.get("network") == "grpc":
        transport["grpcSettings"].setdefault("authority", parsed["host"])
    return {
        "log": {"loglevel": "none"},
        "inbounds": [{"listen": "127.0.0.1", "port": socks_port, "protocol": "socks",
                      "settings": {"auth": "noauth", "udp": False}}],
        # No direct fallback: the HTTP probe can only leave through this outbound.
        "outbounds": [outbound],
    }


def result(status, error=None, **values):
    return {"status": status, "error": error, "latency_ms": None, "country": None,
            "exit_ip": None, "checked_at": utc_now(), "successes": 0, "samples": 0,
            "gemini_status": "untested", "gemini_code": None, **values}


def probe(uri, executable, stop, timeout=10, samples=3):
    if stop.is_set():
        return result("error", "تست متوقف شد.")
    try:
        parsed = parse_uri(uri)
        address = public_address(parsed["host"], parsed["port"])
    except ConfigError as exc:
        return result("error", str(exc))
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    process = None
    with tempfile.TemporaryDirectory(prefix="v2ray-desk-") as folder:
        config_path = Path(folder) / "config.json"
        config_path.write_text(json.dumps(make_config(parsed, address, port)), encoding="utf-8")
        config_path.chmod(0o600)
        kwargs = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
        if hasattr(subprocess, "CREATE_NO_WINDOW"):
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        try:
            validation = subprocess.run([executable, "run", "-test", "-c", str(config_path)],
                                        timeout=8, **kwargs)
            if validation.returncode:
                return result("error", "هستهٔ Xray این تنظیمات را نپذیرفت؛ لینک یا نسخهٔ هسته را بررسی کنید.")
            process = subprocess.Popen([executable, "run", "-c", str(config_path)], **kwargs)
            ready = False
            until = time.monotonic() + 5
            while time.monotonic() < until and not stop.is_set():
                if process.poll() is not None:
                    return result("error", "هستهٔ Xray هنگام اجرا متوقف شد.")
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=.2):
                        ready = True
                        break
                except OSError:
                    stop.wait(.05)
            if not ready:
                return result("error", "هستهٔ تست آماده نشد یا تست متوقف شد.")
            latencies, countries, ips = [], [], []
            for _ in range(samples):
                if stop.is_set():
                    return result("error", "تست متوقف شد.")
                try:
                    with requests.Session() as client:
                        client.trust_env = False
                        client.proxies = {"http": f"socks5h://127.0.0.1:{port}",
                                          "https": f"socks5h://127.0.0.1:{port}"}
                        started = time.perf_counter()
                        with client.get(TRACE_URL, timeout=(timeout, timeout), allow_redirects=False,
                                        stream=True) as response:
                            if response.status_code != 200:
                                continue
                            content = bytearray()
                            for chunk in response.iter_content(1024):
                                content.extend(chunk)
                                if len(content) > 16384 or time.perf_counter() - started > timeout * 2:
                                    raise requests.RequestException()
                            elapsed = (time.perf_counter() - started) * 1000
                        trace = dict(line.split("=", 1) for line in content.decode("utf-8").splitlines() if "=" in line)
                        exit_ip = str(ipaddress.ip_address(trace.get("ip", "")))
                        if not ipaddress.ip_address(exit_ip).is_global:
                            continue
                        country = trace.get("loc", "")
                        if len(country) != 2 or not country.isalpha():
                            country = None
                        latencies.append(elapsed)
                        countries.append(country)
                        ips.append(exit_ip)
                except (requests.RequestException, ValueError, UnicodeError):
                    continue
            if stop.is_set():
                return result("error", "تست متوقف شد.")
            gemini_status, gemini_code = probe_gemini(port, timeout)
            gemini = {"gemini_status": gemini_status, "gemini_code": gemini_code}
            if not latencies:
                return result("failed", "پاسخ معتبر از مقصد تست دریافت نشد؛ اتصال یا دسترسی به Cloudflare را بررسی کنید.",
                              samples=samples, **gemini)
            country = countries[-1] if len(set(countries)) == 1 else None
            return result("healthy", latency_ms=round(statistics.median(latencies), 1),
                          country=country, exit_ip=ips[-1], successes=len(latencies), samples=samples, **gemini)
        except (OSError, subprocess.TimeoutExpired):
            return result("error", "خطا در اجرای هستهٔ تست؛ وضعیت نصب Xray را بررسی کنید.")
        finally:
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)


class TestManager:
    def __init__(self, db_path, executable="xray", workers=3, probe_function=probe):
        self.db_path, self.executable = db_path, executable
        self.workers, self.probe_function = workers, probe_function
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.state = {"running": False, "total": 0, "done": 0, "cancelled": False}
        with connect(db_path) as db:
            db.execute("UPDATE configs SET status='error', error=? WHERE status='testing'",
                       ("تست با راه‌اندازی مجدد سرویس قطع شد؛ دوباره تست کنید.",))

    def snapshot(self):
        with self.lock:
            return dict(self.state)

    def start(self, ids):
        with self.lock:
            if self.state["running"]:
                raise ValueError("یک تست در حال اجراست.")
            if not shutil.which(self.executable):
                raise FileNotFoundError("هستهٔ Xray نصب نیست یا مسیر آن درست نیست.")
            with connect(self.db_path) as db:
                rows = [dict(row) for row in db.execute(
                    "SELECT id,uri FROM configs WHERE id IN (" + ",".join("?" for _ in ids) + ")", ids)]
            if not rows:
                raise ValueError("کانفیگی برای تست انتخاب نشده است.")
            self.stop.clear()
            self.state = {"running": True, "total": len(rows), "done": 0, "cancelled": False}
            threading.Thread(target=self._run, args=(rows,), daemon=True).start()

    def cancel(self):
        with self.lock:
            self.stop.set()
            self.state["cancelled"] = True

    def _one(self, row):
        if self.stop.is_set():
            return
        try:
            with connect(self.db_path) as db:
                db.execute("UPDATE configs SET status='testing' WHERE id=?", (row["id"],))
            outcome = self.probe_function(row["uri"], self.executable, self.stop)
        except Exception:
            outcome = result("error", "خطای داخلی هنگام تست؛ دوباره تلاش کنید.")
        keys = ["status", "error", "latency_ms", "country", "exit_ip", "checked_at", "successes", "samples",
                "gemini_status", "gemini_code"]
        with connect(self.db_path) as db:
            db.execute("UPDATE configs SET " + ",".join(key + "=?" for key in keys) + " WHERE id=?",
                       [outcome[key] for key in keys] + [row["id"]])
        with self.lock:
            self.state["done"] += 1

    def _run(self, rows):
        try:
            with ThreadPoolExecutor(max_workers=self.workers) as pool:
                list(pool.map(self._one, rows))
        finally:
            with self.lock:
                self.state["running"] = False
