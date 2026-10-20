import copy
from collections import deque
import ipaddress
import json
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import aiohttp

from .parser import ConfigError, parse_uri
from .store import connect
from .delay import measure_delay
from .http_probe import proxy_get

TRACE_URL = "https://www.cloudflare.com/cdn-cgi/trace"
GEMINI_URL = "https://gemini.google.com/app"
DELAY_TARGETS = {
    "cloudflare": "https://cp.cloudflare.com/generate_204",
    "google": "https://www.google.com/generate_204",
    "gstatic": "https://www.gstatic.com/generate_204",
}
MAX_WORKERS = 128
MAX_BATCH_SIZE = 128
DEFAULT_OPTIONS = {"workers": 8, "timeout": 5, "samples": 2, "target": "google",
                   "gemini": True, "country": True, "auto_delete_failed": False}


class InvalidConfigError(ConfigError):
    pass


class CoreStoppedError(ConfigError):
    pass


def engine_validation(executable, path, timeout=12):
    kwargs = {"capture_output": True, "timeout": timeout}
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    check = subprocess.run([executable, "run", "-test", "-c", str(path)], **kwargs)
    if not check.returncode:
        return
    # Never return raw engine output: it may contain a config's credentials.
    diagnostic = (check.stdout + check.stderr).decode("utf-8", "replace").lower()
    if (check.returncode < 0 or check.returncode >= 0x80000000
            or any(message in diagnostic for message in (
                "fatal error:", "panic:", "out of memory", "cannot allocate memory",
                "failed to create new os thread", "too many open files", "permission denied", "access is denied"))):
        raise ConfigError("The test core could not validate configurations.")
    for keywords, message in [
        (("publickey", "public key"), "Xray rejected the REALITY public key."),
        (("shortid", "short id"), "Xray rejected the REALITY short ID."),
        (("fingerprint",), "Xray rejected the TLS fingerprint."),
        (("encryption",), "Xray rejected the encryption settings."),
        (("flow",), "Xray rejected the flow or its transport/security combination."),
        (("password", "key length"), "Xray rejected the proxy password or key format."),
        (("uuid", "invalid user"), "Xray rejected the user ID."),
        (("certificate",), "Xray rejected the TLS certificate settings."),
        (("transport", "network"), "Xray rejected the transport settings."),
    ]:
        if any(key in diagnostic for key in keywords):
            raise InvalidConfigError(message)
    raise InvalidConfigError("Xray rejected this configuration. Check the protocol, credentials and transport settings.")


def engine_info(executable):
    try:
        run = subprocess.run([executable, "version"], capture_output=True, timeout=3,
                             **({"creationflags": subprocess.CREATE_NO_WINDOW} if hasattr(subprocess, "CREATE_NO_WINDOW") else {}))
        match = re.search(r"Xray (\d+\.\d+\.\d+)", run.stdout.decode("utf-8", "replace"))
        return {"available": run.returncode == 0, "version": match[1] if match else None}
    except (OSError, subprocess.TimeoutExpired):
        return {"available": False, "version": None}


def test_options(options=None):
    supplied = {} if options is None else options
    if not isinstance(supplied, dict) or set(supplied) - set(DEFAULT_OPTIONS):
        raise ConfigError("Invalid test options.")
    values = {**DEFAULT_OPTIONS, **supplied}
    for key, lower, upper in [("workers", 1, MAX_WORKERS), ("timeout", 2, 20), ("samples", 1, 3)]:
        if type(values[key]) is not int or not lower <= values[key] <= upper:
            raise ConfigError("Invalid test options.")
    if values["samples"] != 2 or not isinstance(values["target"], str) or values["target"] not in DELAY_TARGETS:
        raise ConfigError("Invalid test options.")
    if any(type(values[k]) is not bool for k in ("gemini", "country", "auto_delete_failed")):
        raise ConfigError("Invalid test options.")
    return values


def lookup_country(port, timeout, stop=None):
    try:
        code, _, content = proxy_get(port, TRACE_URL, timeout * 2, limit=16384, stop=stop)
        if code != 200:
            return None, None
        trace = dict(line.split("=", 1) for line in content.decode("utf-8").splitlines() if "=" in line)
        address = ipaddress.ip_address(trace.get("ip", ""))
        if not address.is_global:
            return None, None
        country = trace.get("loc", "").upper()
        return country if len(country) == 2 and country.isascii() and country.isalpha() else None, str(address)
    except (aiohttp.ClientError, OSError, ValueError, UnicodeError):
        return None, None


def probe_gemini(port, timeout, stop=None):
    """HTTP accessibility only; never claim an account can use Gemini."""
    try:
        target = GEMINI_URL
        deadline = time.monotonic() + timeout * 2
        for _ in range(6):
            remaining = deadline - time.monotonic()
            if remaining <= 0 or (stop and stop.is_set()):
                return "unreachable", None
            code, headers, _ = proxy_get(port, target, remaining, stop=stop)
            if code in {301, 302, 303, 307, 308}:
                location = headers.get("Location")
                if not location:
                    return "redirect", code
                target = urljoin(target, location)
                parsed = urlsplit(target)
                if (parsed.scheme != "https" or parsed.username is not None or parsed.password is not None
                        or not (parsed.hostname or "").endswith(".google.com")):
                    return "redirect", code
                if parsed.hostname == "accounts.google.com":
                    return "login", code
                continue
            return {403: "blocked", 429: "limited", 200: "http200"}.get(code, "other"), code
        return "redirect", code
    except (aiohttp.ClientError, OSError, ValueError):
        return "unreachable", None


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def public_address(host, port):
    """Resolve once and pin that address, preventing DNS rebinding into the LAN."""
    try:
        addresses = list(dict.fromkeys(entry[4][0] for entry in socket.getaddrinfo(
            host, port, type=socket.SOCK_STREAM)))
    except (OSError, UnicodeError):
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


def make_batch_config(prepared):
    config = {"log": {"loglevel": "none"}, "inbounds": [],
              "outbounds": [{"tag": "reject", "protocol": "blackhole"}],
              "routing": {"rules": []}}
    for item in prepared:
        single = make_config(item["parsed"], item["address"], item["port"])
        inbound, outbound = single["inbounds"][0], single["outbounds"][0]
        inbound["tag"], outbound["tag"] = f'in-{item["id"]}', f'out-{item["id"]}'
        config["inbounds"].append(inbound)
        config["outbounds"].append(outbound)
        config["routing"]["rules"].append({"type": "field", "inboundTag": [inbound["tag"]],
                                          "outboundTag": outbound["tag"]})
    return config


def validate_batch(prepared, executable):
    """Check a split without opening listeners or starting network probes."""
    # Xray's -test mode does not bind these unique placeholder ports.
    validation_rows = [{**item, "port": 20000 + index} for index, item in enumerate(prepared)]
    with tempfile.TemporaryDirectory(prefix="v2ray-desk-check-") as folder:
        path = Path(folder) / "config.json"
        path.write_text(json.dumps(make_batch_config(validation_rows)), encoding="utf-8")
        path.chmod(0o600)
        engine_validation(executable, path)


@contextmanager
def shared_core(prepared, executable, stop):
    process, reservations = None, []
    kwargs = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    with tempfile.TemporaryDirectory(prefix="v2ray-desk-batch-") as folder:
        try:
            for item in prepared:
                sock = socket.socket()
                reservations.append(sock)
                sock.bind(("127.0.0.1", 0))
                item["port"] = sock.getsockname()[1]
            path = Path(folder) / "config.json"
            path.write_text(json.dumps(make_batch_config(prepared)), encoding="utf-8")
            path.chmod(0o600)
            engine_validation(executable, path)
            for sock in reservations:
                sock.close()
            if stop.is_set():
                yield {}
                return
            process = subprocess.Popen([executable, "run", "-c", str(path)], **kwargs)
            # v2rayN also waits one second before issuing the probes.
            stop.wait(1)
            if process.poll() is not None:
                raise ConfigError("The batch test core could not start.")
            yield {item["id"]: item["port"] for item in prepared}
            if not stop.is_set() and process.poll() is not None:
                raise CoreStoppedError("The batch test core stopped before the batch completed.")
        finally:
            for sock in reservations:
                sock.close()
            if process and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)


def result(status, error=None, **values):
    return {"status": status, "error": error, "latency_ms": None, "country": None,
            "exit_ip": None, "checked_at": utc_now(), "successes": 0, "samples": 0,
            "gemini_status": "untested", "gemini_code": None,
            "delay_version": 3, "delay_target": None, "delay_samples": "[]", "delay_codes": "[]", **values}


def probe_connection(port, stop, timeout=5, samples=2, target="google", gemini=True, country=True):
    latencies, codes, latency = [], [], None
    connection_failure = False
    try:
        latency, latencies, codes = measure_delay(port, DELAY_TARGETS[target], timeout, stop)
    except Exception as exc:
        connection_failure = getattr(exc, "real_delay_connection_failure", False) is True
        codes = getattr(exc, "delay_response_codes", [])
    if stop.is_set():
        return result("error", "تست متوقف شد.")
    country_code, exit_ip = lookup_country(port, timeout, stop) if country and latency else (None, None)
    if stop.is_set():
        return result("error", "تست متوقف شد.")
    gemini_status, gemini_code = probe_gemini(port, timeout, stop) if gemini else ("untested", None)
    if stop.is_set():
        return result("error", "تست متوقف شد.")
    metadata = {"gemini_status": gemini_status, "gemini_code": gemini_code,
                "delay_target": target, "delay_samples": json.dumps(latencies), "delay_codes": json.dumps(codes)}
    if latency is None:
        return result("failed", "The two-request delay test failed or timed out.", samples=2,
                      real_delay_connection_failure=connection_failure,
                      real_delay_response_received=bool(codes), **metadata)
    return result("healthy", latency_ms=latency, country=country_code, exit_ip=exit_ip,
                  successes=2, samples=2, **metadata)


def probe(uri, executable, stop, timeout=5, samples=2, target="google", gemini=True, country=True):
    if stop.is_set():
        return result("error", "تست متوقف شد.")
    try:
        parsed = parse_uri(uri)
    except ConfigError as exc:
        return result("invalid", str(exc))
    try:
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
            engine_validation(executable, config_path, timeout=8)
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
            stop.wait(1)
            return probe_connection(port, stop, timeout, samples, target, gemini, country)
        except InvalidConfigError as exc:
            return result("invalid", str(exc))
        except (ConfigError, OSError, subprocess.TimeoutExpired):
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
    def __init__(self, db_path, executable="xray", workers=8, probe_function=probe):
        self.db_path, self.executable = db_path, executable
        self.workers, self.probe_function = workers, probe_function
        self.engine = engine_info(executable)
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.state = {"running": False, "total": 0, "done": 0, "active": 0, "cancelled": False,
                      "workers": workers, "options": {**DEFAULT_OPTIONS, "workers": workers}, "auto_deleted": 0}
        self._outcomes = {}
        self._job_unsafe = False
        self.started = None
        self.ended = None
        self.epoch = uuid.uuid4().hex
        self.sequence = 0
        self.changes = deque(maxlen=20000)
        with connect(db_path) as db:
            db.execute("UPDATE configs SET status='error', error=? WHERE status='testing'",
                       ("تست با راه‌اندازی مجدد سرویس قطع شد؛ دوباره تست کنید.",))

    def snapshot(self):
        with self.lock:
            elapsed = ((self.ended or time.monotonic()) - self.started) if self.started else 0
            done = self.state["done"]
            rate = done / elapsed if elapsed else 0
            waiting = max(0, self.state["total"] - done - self.state["active"])
            return {**self.state, "elapsed": round(elapsed, 1), "rate": round(rate, 2),
                    "queued": waiting,
                    "eta": round((self.state["total"] - done) / rate) if rate and self.state["running"] else None}

    def invalidate(self):
        with self.lock:
            self.epoch = uuid.uuid4().hex
            self.sequence = 0
            self.changes.clear()

    def changed(self, config_id):
        with self.lock:
            self.sequence += 1
            self.changes.append((self.sequence, config_id))

    def delta(self, cursor):
        with self.lock:
            current = f"{self.epoch}:{self.sequence}"
            try:
                epoch, sequence = cursor.split(":")
                sequence = int(sequence)
                if epoch != self.epoch or not 0 <= sequence <= self.sequence:
                    raise ValueError()
                if self.changes and sequence < self.changes[0][0] - 1:
                    raise ValueError()
                return current, {i for s, i in self.changes if s > sequence}
            except (ValueError, AttributeError):
                return current, None

    def start(self, ids, options=None):
        options = test_options(options if options is not None else {"workers": self.workers})
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
            self._outcomes = {}
            self._job_unsafe = False
            self.started, self.ended = time.monotonic(), None
            self.state = {"running": True, "total": len(rows), "done": 0, "active": 0, "cancelled": False,
                          "workers": min(options["workers"], len(rows)), "options": options, "auto_deleted": 0}
            try:
                threading.Thread(target=self._run, args=(rows, options), daemon=True).start()
            except Exception:
                # No worker exists to run _run's cleanup if thread creation fails.
                self.state.update(running=False, phase="idle")
                self.ended = time.monotonic()
                raise

    def cancel(self):
        with self.lock:
            if not self.state["running"]:
                return
            self.stop.set()
            self.state["cancelled"] = True

    def _one(self, row, options, ports=None):
        if self.stop.is_set():
            return
        try:
            with connect(self.db_path) as db:
                db.execute("UPDATE configs SET status='testing' WHERE id=?", (row["id"],))
            self.changed(row["id"])
            arguments = {k: v for k, v in options.items() if k not in {"workers", "auto_delete_failed"}}
            if ports is None:
                outcome = self.probe_function(row["uri"], self.executable, self.stop, **arguments)
            else:
                outcome = probe_connection(ports[row["id"]], self.stop, **arguments)
        except Exception:
            outcome = result("error", "خطای داخلی هنگام تست؛ دوباره تلاش کنید.")
        self._save(row, outcome)

    def _save(self, row, outcome):
        keys = ["status", "error", "latency_ms", "country", "exit_ip", "checked_at", "successes", "samples",
                "gemini_status", "gemini_code", "delay_version", "delay_target", "delay_samples", "delay_codes"]
        with connect(self.db_path) as db:
            db.execute("UPDATE configs SET " + ",".join(key + "=?" for key in keys) + " WHERE id=?",
                       [outcome[key] for key in keys] + [row["id"]])
        with self.lock:
            if row["id"] not in self._outcomes:
                self.state["done"] += 1
            self._outcomes[row["id"]] = (
                "healthy" if outcome["status"] == "healthy" or outcome.get("real_delay_response_received") is True else
                "failed" if outcome["status"] == "failed" and outcome.get("real_delay_connection_failure") is True else None
            )
            self.changed(row["id"])

    def _finish_failure_streaks(self, options):
        """Apply one outcome per configuration only after an uncancelled job."""
        healthy = [(config_id,) for config_id, outcome in self._outcomes.items() if outcome == "healthy"]
        failed = [(config_id,) for config_id, outcome in self._outcomes.items() if outcome == "failed"]
        deleted = 0
        with connect(self.db_path) as db:
            db.execute("BEGIN IMMEDIATE")
            db.executemany("UPDATE configs SET real_failure_streak=0 WHERE id=?", healthy)
            db.executemany("UPDATE configs SET real_failure_streak=real_failure_streak+1 WHERE id=?", failed)
            if options["auto_delete_failed"]:
                # Keep each IN query bounded, including a 10,000-row job.
                for offset in range(0, len(failed), MAX_BATCH_SIZE):
                    ids = [item[0] for item in failed[offset:offset + MAX_BATCH_SIZE]]
                    rows = db.execute("SELECT id,fingerprint FROM configs WHERE real_failure_streak>=2 AND id IN ("
                                      + ",".join("?" for _ in ids) + ")", ids).fetchall()
                    db.executemany("INSERT OR IGNORE INTO rejected_configs(fingerprint) VALUES (?)",
                                   [(row["fingerprint"],) for row in rows])
                    db.executemany("DELETE FROM configs WHERE id=?", [(row["id"],) for row in rows])
                    deleted += len(rows)
        self.state["auto_deleted"] = deleted
        if deleted:
            self.invalidate()

    def _drain(self, rows, options, ports=None):
        # Only worker-count futures exist, even with 10,000 configurations.
        queue = deque(rows)
        def worker():
            while True:
                with self.lock:
                    if self.stop.is_set() or not queue:
                        return
                    row = queue.popleft()
                    self.state["active"] += 1
                try:
                    self._one(row, options, ports)
                finally:
                    with self.lock:
                        self.state["active"] -= 1
        with ThreadPoolExecutor(max_workers=self.state["workers"]) as pool:
            futures = [pool.submit(worker) for _ in range(self.state["workers"])]
            for future in futures:
                future.result()

    def _isolate_invalid(self, prepared, error):
        """Collect valid survivors before starting any network testing."""
        if not prepared or self.stop.is_set():
            return []
        if len(prepared) == 1:
            self._save(prepared[0], result("invalid", str(error)))
            return []
        middle = len(prepared) // 2
        valid = []
        for part in (prepared[:middle], prepared[middle:]):
            if self.stop.is_set():
                break
            try:
                validate_batch(part, self.executable)
            except InvalidConfigError as exc:
                valid.extend(self._isolate_invalid(part, exc))
            else:
                valid.extend(part)
        return valid

    def _core_error(self, prepared):
        self._job_unsafe = True
        for row in prepared:
            if self.stop.is_set():
                break
            # Preserve isolated invalid rows and responses already proven healthy.
            if row["id"] not in self._outcomes or self._outcomes[row["id"]] == "failed":
                self._save(row, result("error", "The batch test core could not start."))

    def _batch(self, prepared, options):
        isolated = False
        while prepared and not self.stop.is_set():
            try:
                with shared_core(prepared, self.executable, self.stop) as ports:
                    with self.lock:
                        self.state["phase"] = "testing"
                    self._drain(prepared, options, ports)
                return
            except CoreStoppedError:
                self._job_unsafe = True
                for row in prepared:
                    if self._outcomes.get(row["id"]) == "failed":
                        self._save(row, result("error", "The batch test core stopped before the batch completed."))
                return
            except InvalidConfigError as exc:
                if isolated:
                    # A combined batch of validated survivors should be valid.
                    # Do not repeat isolation indefinitely on an engine failure.
                    self._core_error(prepared)
                    return
                try:
                    prepared = self._isolate_invalid(prepared, exc)
                except (ConfigError, OSError, subprocess.TimeoutExpired):
                    self._core_error(prepared)
                    return
                isolated = True
            except (ConfigError, OSError, subprocess.TimeoutExpired):
                # Resource/startup failures cannot be isolated to one config.
                self._core_error(prepared)
                return

    def _run(self, rows, options):
        completed = False
        try:
            if self.probe_function is not probe:
                self._drain(rows, options)
                completed = True
                return
            batch_size = min(MAX_BATCH_SIZE, options["workers"] * 4)
            for offset in range(0, len(rows), batch_size):
                if self.stop.is_set():
                    break
                with self.lock:
                    self.state["phase"] = "preparing"
                def prepare(row):
                    if self.stop.is_set():
                        return None
                    try:
                        parsed = parse_uri(row["uri"])
                    except ConfigError as exc:
                        self._save(row, result("invalid", str(exc)))
                        return None
                    try:
                        return {**row, "parsed": parsed, "address": public_address(parsed["host"], parsed["port"])}
                    except ConfigError as exc:
                        self._save(row, result("error", str(exc)))
                        return None
                with ThreadPoolExecutor(max_workers=options["workers"]) as pool:
                    prepared = [item for item in pool.map(prepare, rows[offset:offset + batch_size]) if item]
                self._batch(prepared, options)
            completed = True
        finally:
            with self.lock:
                try:
                    if completed and not self.stop.is_set() and not self._job_unsafe and self.state["done"] == self.state["total"]:
                        self._finish_failure_streaks(options)
                finally:
                    self.state["running"] = False
                    self.state["phase"] = "idle"
                    self.ended = time.monotonic()
