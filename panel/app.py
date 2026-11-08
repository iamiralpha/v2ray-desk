import base64
import csv
import hmac
import io
import os
import secrets
import sqlite3
import threading
import time
from datetime import timedelta
from pathlib import Path

from flask import Flask, Response, jsonify, redirect, render_template, request, session, url_for
from werkzeug.exceptions import HTTPException
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.middleware.proxy_fix import ProxyFix

from .parser import ConfigError, import_lines, parse_uri
from .i18n import translate
from .store import connect, initialize, save_password, setting
from .tester import TestManager
from .subscriptions import fetch_subscription, validate_url, reconcile, prune
from .presets import PRESETS, PROVIDERS, SEED_MARKER, existing_sources, install_presets, url_identity
from .public import create_public_blueprint
from .nodes import create_nodes_blueprint, node_views, public_nodes


def create_app(data_dir=None, test_config=None):
    app = Flask(__name__)
    # Waitress listens on loopback; Nginx overwrites this header with the configured path.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=0, x_proto=0, x_host=0, x_port=0, x_prefix=1)
    db_path = initialize(data_dir or os.environ.get("PANEL_DATA", "./data"))
    app.register_blueprint(create_public_blueprint(db_path))
    app.config.update(
        SECRET_KEY=setting(db_path, "secret"),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Strict",
        SESSION_COOKIE_SECURE=os.environ.get("PANEL_COOKIE_SECURE", "1") == "1",
        PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
        SESSION_REFRESH_EACH_REQUEST=False,
        MAX_CONTENT_LENGTH=4 * 1024 * 1024,
        DB_PATH=db_path,
    )
    if test_config:
        app.config.update(test_config)
    manager = TestManager(db_path, os.environ.get("XRAY_BIN", "xray"))
    app.extensions["tester"] = manager
    app.register_blueprint(create_nodes_blueprint(db_path, manager))
    syncing = set()
    sync_progress = {"total": 0, "done": 0}
    if not setting(db_path, "password_hash"):
        raise RuntimeError("Run python manage.py init before starting the panel.")

    def sync_snapshot():
        with manager.lock:
            return {"running": bool(syncing), **sync_progress}

    def reserve_sync(ids):
        # Called under manager.lock, before the worker can begin or tests start.
        if not syncing:
            sync_progress.update(total=0, done=0)
        syncing.update(ids)
        sync_progress["total"] += len(ids)

    def finish_sync(sub_id):
        with manager.lock:
            syncing.discard(sub_id)
            sync_progress["done"] += 1

    def sync_one(sub_id):
        with connect(db_path) as db:
            row = db.execute("SELECT url FROM subscriptions WHERE id=?", (sub_id,)).fetchone()
        if not row:
            raise ConfigError("Subscription not found.")
        text = fetch_subscription(row["url"])
        with manager.lock:
            with connect(db_path) as db:
                result = reconcile(db, sub_id, text)
            manager.invalidate()
        return result

    def save_sync_error(sub_id, message):
        with manager.lock, connect(db_path) as db:
            db.execute("UPDATE subscriptions SET error=? WHERE id=?", (message, sub_id))

    def queue_sync(ids):
        if not ids:
            return 0
        reserve_sync(ids)

        def worker():
            pending = set(ids)
            try:
                for sub_id in ids:
                    try:
                        sync_one(sub_id)
                    except ConfigError as exc:
                        save_sync_error(sub_id, str(exc))
                    except Exception as exc:
                        app.logger.error("Subscription sync failed: %s", type(exc).__name__)
                        save_sync_error(sub_id, "Subscription download failed.")
                    finally:
                        finish_sync(sub_id)
                        pending.discard(sub_id)
            finally:
                # Even an unavailable database must not reserve the test queue forever.
                for sub_id in pending:
                    finish_sync(sub_id)

        thread = threading.Thread(target=worker, name="subscription-presets", daemon=True)
        app.extensions["subscription_sync_thread"] = thread
        try:
            thread.start()
        except RuntimeError:
            try:
                for sub_id in ids:
                    save_sync_error(sub_id, "Subscription sync could not start. Refresh this source again.")
            finally:
                syncing.difference_update(ids)
                sync_progress["done"] += len(ids)
            return 0
        return len(ids)

    app.extensions["subscription_sync"] = sync_snapshot

    def csrf():
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(32)
        return session["csrf"]

    app.jinja_env.globals["csrf_token"] = csrf

    @app.before_request
    def protect():
        if request.args.get("lang") in {"fa", "en"}:
            session["lang"] = request.args["lang"]
        if request.path == "/healthz":
            return None
        # Nodes use narrowly scoped bearer credentials; all admin endpoints keep
        # the existing session and CSRF protections.
        if request.endpoint in {"node_api.pull", "node_api.report"}:
            return None
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            supplied = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token", "")
            expected = session.get("csrf", "")
            if not expected or not hmac.compare_digest(supplied, expected):
                return jsonify(error="نشست نامعتبر است؛ صفحه را تازه کنید."), 403
        if request.endpoint not in {"login", "static", "index", "public.configs", "public.config", "public.copy_configs"}:
            authenticated = session.get("authenticated") and session.get("auth_version") == setting(db_path, "auth_version")
            if not authenticated:
                if request.path.startswith("/api/"):
                    return jsonify(error="لطفاً دوباره وارد شوید."), 401
                return redirect(url_for("login"))

    @app.after_request
    def headers(response):
        if response.is_json:
            data = response.get_json()
            if isinstance(data, dict):
                if isinstance(data.get("error"), str):
                    data["error"] = translate(data["error"], session.get("lang", "fa"))
                if isinstance(data.get("last_error"), str):
                    data["last_error"] = translate(data["last_error"], session.get("lang", "fa"))
                for key in ("configs", "errors", "subscriptions"):
                    for item in data.get(key, []):
                        if isinstance(item.get("error"), str):
                            item["error"] = translate(item["error"], session.get("lang", "fa"))
                response.set_data(app.json.dumps(data))
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; font-src 'self'; "
            "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        return response

    @app.errorhandler(ConfigError)
    def bad_config(exc):
        return jsonify(error=str(exc)), 400

    @app.errorhandler(HTTPException)
    def http_error(exc):
        return jsonify(error="درخواست نامعتبر است." if exc.code != 413 else "حداکثر حجم ورودی ۴ مگابایت است."), exc.code

    @app.errorhandler(Exception)
    def unexpected_error(exc):
        # Do not log request bodies, credentials, or raw config links.
        app.logger.error("Request failed: %s", type(exc).__name__)
        return jsonify(error="خطای داخلی؛ وضعیت سرویس را بررسی کنید."), 500

    def payload():
        data = request.get_json()
        if not isinstance(data, dict):
            raise ConfigError("بدنهٔ درخواست باید JSON باشد.")
        return data

    def selected_ids(data):
        ids = data.get("ids")
        if not isinstance(ids, list) or not ids or len(ids) > 10000:
            raise ConfigError("حداقل یک کانفیگ و حداکثر ۱۰۰۰۰ کانفیگ انتخاب کنید.")
        if any(type(value) is not int or value < 1 for value in ids):
            raise ConfigError("شناسهٔ کانفیگ معتبر نیست.")
        return list(dict.fromkeys(ids))

    def busy():
        return jsonify(error="تا پایان یا توقف تست، امکان تغییر کانفیگ‌ها وجود ندارد."), 409

    @app.get("/healthz")
    def health():
        return jsonify(ok=True)

    @app.route("/login", methods=["GET", "POST"])
    def login():
        error = None
        status = 200
        if request.method == "POST":
            ip = request.remote_addr or "unknown"
            now = time.time()
            with connect(db_path) as db:
                db.execute("DELETE FROM login_attempts WHERE started < ?", (now - 900,))
                attempts = db.execute("SELECT * FROM login_attempts WHERE ip=?", (ip,)).fetchone()
                if attempts and attempts["count"] >= 5:
                    error, status = "تلاش‌های ورود بیش از حد مجاز است؛ ۱۵ دقیقه بعد دوباره تلاش کنید.", 429
                elif check_password_hash(setting(db_path, "password_hash"), request.form.get("password", "")[:1024]):
                    db.execute("DELETE FROM login_attempts WHERE ip=?", (ip,))
                    language = session.get("lang", "fa")
                    session.clear()
                    session["lang"] = language
                    session.permanent = True
                    session["authenticated"] = True
                    session["auth_version"] = setting(db_path, "auth_version")
                    csrf()
                    return redirect(url_for("index"))
                else:
                    db.execute("""INSERT INTO login_attempts VALUES (?,1,?)
                                  ON CONFLICT(ip) DO UPDATE SET count=count+1""", (ip, now))
                    error, status = "رمز عبور درست نیست.", 401
        language = session.get("lang", "fa")
        return render_template("login.en.html" if language == "en" else "login.html",
                               error=translate(error, language)), status

    @app.post("/api/logout")
    def logout():
        language = session.get("lang", "fa")
        session.clear()
        session["lang"] = language
        return jsonify(ok=True)

    @app.get("/")
    def index():
        if not (session.get("authenticated") and session.get("auth_version") == setting(db_path, "auth_version")):
            return render_template("public.html", language=session.get("lang", "fa"))
        return render_template("index.en.html" if session.get("lang") == "en" else "index.html")

    @app.get("/api/configs")
    def configs():
        with manager.lock:
            cursor, changed = manager.delta(request.args.get("cursor"))
            query = """SELECT id,name,protocol,host,port,status,latency_ms,country,
                       exit_ip,checked_at,error,successes,samples,gemini_status,gemini_code,
                       delay_version,delay_target,delay_samples,delay_codes,transport,security FROM configs"""
            params = []
            if changed is not None:
                params = list(changed)
                query += " WHERE id IN (" + ",".join("?" for _ in params) + ")" if params else " WHERE 0"
            with connect(db_path) as db:
                rows = db.execute(query + " ORDER BY id DESC", params).fetchall()
                items = {row["id"]: {**dict(row), "subscription_ids": []} for row in rows}
                node_views(db, items)
                if items:
                    memberships = db.execute("SELECT config_id,subscription_id FROM subscription_configs WHERE config_id IN ("
                                             + ",".join("?" for _ in items) + ") ORDER BY subscription_id", list(items))
                    for membership in memberships:
                        items[membership["config_id"]]["subscription_ids"].append(membership["subscription_id"])
                sources = [dict(row) for row in db.execute("""SELECT s.id,s.name,COUNT(sc.config_id) AS config_count
                    FROM subscriptions s LEFT JOIN subscription_configs sc ON sc.subscription_id=s.id
                    GROUP BY s.id ORDER BY s.id""")]
                current_nodes = public_nodes(db)
            return jsonify(configs=list(items.values()), nodes=current_nodes, job=manager.snapshot(), subscriptions=sources,
                           subscription_sync=sync_snapshot(), cursor=cursor, full=changed is None, engine=manager.engine)

    @app.get("/api/configs/<int:config_id>")
    def get_config(config_id):
        with connect(db_path) as db:
            row = db.execute("SELECT id,name,uri FROM configs WHERE id=?", (config_id,)).fetchone()
        if not row:
            return jsonify(error="کانفیگ پیدا نشد."), 404
        return jsonify(dict(row))

    @app.post("/api/import")
    def import_configs():
        data = payload()
        if not isinstance(data.get("text"), str):
            raise ConfigError("فهرست کانفیگ‌ها را وارد کنید.")
        lines = import_lines(data["text"])
        added, duplicates, errors = 0, 0, []
        with manager.lock:
            if manager.state["running"]:
                return busy()
            with connect(db_path) as db:
                existing = db.execute("SELECT COUNT(*) FROM configs").fetchone()[0]
                for line_number, line in lines:
                    try:
                        parsed = parse_uri(line)
                        if db.execute("SELECT 1 FROM configs WHERE fingerprint=?", (parsed["fingerprint"],)).fetchone():
                            db.execute("UPDATE configs SET manual=1,real_failure_streak=0 WHERE fingerprint=?", (parsed["fingerprint"],))
                            db.execute("DELETE FROM rejected_configs WHERE fingerprint=?", (parsed["fingerprint"],))
                            duplicates += 1
                            continue
                        if existing + added >= 10000:
                            raise ConfigError("سقف ذخیرهٔ ۱۰۰۰۰ کانفیگ پر شده است.")
                        db.execute("""INSERT INTO configs(uri,name,protocol,host,port,fingerprint,transport,security)
                                      VALUES (?,?,?,?,?,?,?,?)""",
                                   [parsed[key] for key in ("uri", "name", "protocol", "host", "port", "fingerprint", "transport", "security")])
                        db.execute("DELETE FROM rejected_configs WHERE fingerprint=?", (parsed["fingerprint"],))
                        added += 1
                    except ConfigError as exc:
                        errors.append({"line": line_number, "error": str(exc)})
            manager.invalidate()
        return jsonify(added=added, duplicates=duplicates, errors=errors)

    @app.put("/api/configs/<int:config_id>")
    def edit_config(config_id):
        data = payload()
        if not isinstance(data.get("uri"), str) or not isinstance(data.get("name", ""), str):
            raise ConfigError("لینک یا نام کانفیگ معتبر نیست.")
        parsed = parse_uri(data["uri"])
        name = data.get("name", "").strip() or parsed["name"]
        if len(name) > 200:
            raise ConfigError("نام کانفیگ بیش از ۲۰۰ نویسه است.")
        with manager.lock:
            if manager.state["running"]:
                return busy()
            with connect(db_path) as db:
                try:
                    cursor = db.execute("""UPDATE configs SET uri=?,name=?,protocol=?,host=?,port=?,fingerprint=?,
                        status='pending',latency_ms=NULL,country=NULL,exit_ip=NULL,checked_at=NULL,error=NULL,
                        successes=0,samples=0,gemini_status='untested',gemini_code=NULL,manual=1,real_failure_streak=0,
                        delay_version=3,delay_target=NULL,delay_samples='[]',delay_codes='[]',transport=?,security=? WHERE id=?""",
                        (parsed["uri"], name, parsed["protocol"], parsed["host"], parsed["port"], parsed["fingerprint"],
                         parsed["transport"], parsed["security"], config_id))
                except sqlite3.IntegrityError:
                    raise ConfigError("این کانفیگ قبلاً در فهرست وجود دارد.") from None
                if cursor.rowcount == 0:
                    return jsonify(error="کانفیگ پیدا نشد."), 404
                db.execute("DELETE FROM rejected_configs WHERE fingerprint=?", (parsed["fingerprint"],))
            manager.invalidate()
        return jsonify(ok=True)

    @app.post("/api/delete")
    def delete_configs():
        data = payload()
        ids = selected_ids(data)
        failed_only = data.get("failed_only") is True
        with manager.lock:
            if manager.state["running"]:
                return busy()
            with connect(db_path) as db:
                query = "DELETE FROM configs WHERE id IN (" + ",".join("?" for _ in ids) + ")"
                if failed_only:
                    query += " AND status='failed'"
                deleted = db.execute(query, ids).rowcount
            manager.invalidate()
        return jsonify(deleted=deleted)

    @app.post("/api/export")
    def export_configs():
        data = payload()
        ids = selected_ids(data)
        export_format = data.get("format", "plain")
        if export_format not in ("plain", "base64"):
            raise ConfigError("Export format must be plain or base64.")
        with connect(db_path) as db:
            rows = db.execute("SELECT id,uri FROM configs WHERE id IN (" + ",".join("?" for _ in ids) + ")", ids)
            mapping = {row["id"]: row["uri"] for row in rows}
        content = ("\n".join(mapping[item] for item in ids if item in mapping) + "\n").encode("utf-8")
        if export_format == "base64":
            content = base64.b64encode(content)
        response = Response(content, content_type="text/plain; charset=utf-8")
        filename = "v2ray-configs-base64.txt" if export_format == "base64" else "v2ray-configs.txt"
        response.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
        return response

    @app.post("/api/report")
    def export_report():
        ids = selected_ids(payload())
        columns = ["id", "name", "protocol", "host", "port", "transport", "security",
                   "status", "latency_ms", "country", "exit_ip", "gemini_status",
                   "gemini_code", "checked_at", "delay_target", "error"]
        with connect(db_path) as db:
            rows = db.execute("SELECT " + ",".join(columns) + " FROM configs WHERE id IN ("
                              + ",".join("?" for _ in ids) + ")", ids)
            mapping = {row["id"]: dict(row) for row in rows}
        output = io.StringIO(newline="")
        output.write("\ufeff")
        writer = csv.writer(output)
        writer.writerow(columns)
        for config_id in ids:
            if config_id not in mapping:
                continue
            values = []
            for column in columns:
                value = mapping[config_id][column]
                if column == "error" and value:
                    value = translate(value, session.get("lang", "fa"))
                # Config names are untrusted. Prevent spreadsheet formula execution.
                if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
                    value = "'" + value
                values.append(value)
            writer.writerow(values)
        response = Response(output.getvalue(), content_type="text/csv; charset=utf-8")
        response.headers["Content-Disposition"] = 'attachment; filename="v2ray-report.csv"'
        return response

    @app.post("/api/test")
    def start_tests():
        with manager.lock:
            if syncing:
                return jsonify(error="Subscription refresh is running."), 409
            return launch_tests()

    def launch_tests():
        data = payload()
        ids = selected_ids(data)
        try:
            manager.start(ids, data.get("options"))
        except ConfigError:
            raise
        except FileNotFoundError as exc:
            return jsonify(error=str(exc)), 503
        except ValueError as exc:
            return jsonify(error=str(exc)), 409
        return jsonify(job=manager.snapshot()), 202

    @app.get("/api/subscriptions")
    def subscriptions():
        with connect(db_path) as db:
            rows = db.execute("""SELECT s.*,COUNT(sc.config_id) AS config_count FROM subscriptions s
                LEFT JOIN subscription_configs sc ON sc.subscription_id=s.id GROUP BY s.id ORDER BY s.id DESC""")
            items = []
            for row in rows:
                item = dict(row)
                item["host"] = validate_url(item.pop("url"))[0].hostname
                item["syncing"] = item["id"] in syncing
                items.append(item)
        return jsonify(subscriptions=items, subscription_sync=sync_snapshot())

    @app.get("/api/subscriptions/presets")
    def subscription_presets():
        with manager.lock:
            with connect(db_path) as db:
                existing = existing_sources(db)
            items = []
            for preset in PRESETS:
                installed = existing.get(url_identity(preset["url"]))
                items.append({"key": preset["key"], "provider": preset["provider"], "name": preset["name"],
                              "installed": installed is not None, "subscription_id": installed["id"] if installed else None,
                              "enabled": bool(installed["enabled"]) if installed else bool(preset["enabled"]),
                              "syncing": bool(installed and installed["id"] in syncing)})
            providers = []
            for provider in PROVIDERS:
                entries = [item for item in items if item["provider"] == provider["id"]]
                installed = sum(item["installed"] for item in entries)
                providers.append({**provider, "total": len(entries), "installed": installed,
                                  "missing": len(entries) - installed,
                                  "default_enabled": sum(bool(item["enabled"]) for item in PRESETS if item["provider"] == provider["id"])})
            return jsonify(providers=providers, presets=items, subscription_sync=sync_snapshot())

    @app.post("/api/subscriptions/presets")
    def restore_presets():
        data = payload()
        provider = data.get("provider")
        if provider is not None and (not isinstance(provider, str) or provider not in {item["id"] for item in PROVIDERS}):
            raise ConfigError("Subscription provider is invalid.")
        chosen = [item for item in PRESETS if provider is None or item["provider"] == provider]
        with manager.lock:
            if manager.state["running"] or syncing:
                return busy()
            with connect(db_path) as db:
                inserted, enabled = install_presets(db, chosen)
            if inserted:
                manager.invalidate()
            queued = queue_sync(enabled)
            return jsonify(added=len(inserted), queued=queued, subscription_sync=sync_snapshot()), 202

    @app.get("/api/subscriptions/<int:sub_id>")
    def subscription_detail(sub_id):
        with connect(db_path) as db:
            row = db.execute("SELECT * FROM subscriptions WHERE id=?", (sub_id,)).fetchone()
        return jsonify(dict(row)) if row else (jsonify(error="Subscription not found."), 404)

    @app.post("/api/subscriptions")
    @app.put("/api/subscriptions/<int:sub_id>")
    def save_subscription(sub_id=None):
        data = payload()
        name, url = data.get("name"), data.get("url")
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 200:
            raise ConfigError("Subscription name must be 1–200 characters.")
        validate_url(url)
        enabled = data.get("enabled", True)
        if type(enabled) is not bool:
            raise ConfigError("Subscription enabled value is invalid.")
        with manager.lock:
            if manager.state["running"] or sub_id in syncing or -1 in syncing:
                return busy()
            with connect(db_path) as db:
                if sub_id is None:
                    if db.execute("SELECT COUNT(*) FROM subscriptions").fetchone()[0] >= 100:
                        raise ConfigError("Subscription limit (100) reached.")
                    sub_id = db.execute("INSERT INTO subscriptions(name,url,enabled) VALUES (?,?,?)",
                                        (name.strip(), url, enabled)).lastrowid
                elif not db.execute("UPDATE subscriptions SET name=?,url=?,enabled=? WHERE id=?",
                                    (name.strip(), url, enabled, sub_id)).rowcount:
                    return jsonify(error="Subscription not found."), 404
            manager.invalidate()
        return jsonify(id=sub_id)

    @app.delete("/api/subscriptions/<int:sub_id>")
    def delete_subscription(sub_id):
        data = payload()
        with manager.lock:
            if manager.state["running"] or sub_id in syncing or -1 in syncing:
                return busy()
            with connect(db_path) as db:
                ids = {r[0] for r in db.execute(
                    "SELECT config_id FROM subscription_configs WHERE subscription_id=?", (sub_id,))}
                if not db.execute("DELETE FROM subscriptions WHERE id=?", (sub_id,)).rowcount:
                    return jsonify(error="Subscription not found."), 404
                if data.get("remove_configs") is True:
                    removed = prune(db, ids)
                else:
                    db.executemany("UPDATE configs SET manual=1 WHERE id=?", [(i,) for i in ids])
                    removed = 0
            manager.invalidate()
        return jsonify(removed=removed)

    @app.post("/api/subscriptions/<int:sub_id>/refresh")
    def refresh_subscription(sub_id):
        with manager.lock:
            if manager.state["running"] or -1 in syncing:
                return busy()
            if sub_id in syncing:
                return jsonify(error="Subscription refresh is running."), 409
            with connect(db_path) as db:
                row = db.execute("SELECT url FROM subscriptions WHERE id=?", (sub_id,)).fetchone()
            if not row:
                return jsonify(error="Subscription not found."), 404
            reserve_sync([sub_id])
        try:
            return jsonify(sync_one(sub_id))
        except ConfigError as exc:
            save_sync_error(sub_id, str(exc))
            raise
        finally:
            finish_sync(sub_id)

    @app.post("/api/test/cancel")
    def cancel_tests():
        manager.cancel()
        return jsonify(ok=True)

    @app.post("/api/password")
    def change_password():
        data = payload()
        current = data.get("current", "")
        new = data.get("password", "")
        if not isinstance(current, str) or not isinstance(new, str):
            raise ConfigError("رمز معتبر نیست.")
        if not check_password_hash(setting(db_path, "password_hash"), current[:1024]):
            return jsonify(error="رمز فعلی درست نیست."), 400
        if not 12 <= len(new) <= 128:
            raise ConfigError("رمز جدید باید بین ۱۲ و ۱۲۸ نویسه باشد.")
        save_password(db_path, generate_password_hash(new, method="scrypt"))
        language = session.get("lang", "fa")
        session.clear()
        session["lang"] = language
        return jsonify(ok=True)

    if app.config.get("PRESET_AUTO_SEED", not app.testing):
        with manager.lock:
            with connect(db_path) as db:
                seeded = db.execute("SELECT 1 FROM settings WHERE key=?", (SEED_MARKER,)).fetchone()
                if not seeded:
                    inserted, enabled = install_presets(db, PRESETS, partial=True)
                    db.execute("INSERT INTO settings(key,value) VALUES (?, '1')", (SEED_MARKER,))
                else:
                    inserted, enabled = [], []
            if inserted:
                manager.invalidate()
            queue_sync(enabled)

    from .automation import AutomationManager
    automation = AutomationManager(db_path, manager, syncing)
    app.extensions["automation"] = automation

    @app.get("/api/automation")
    def automation_status():
        return jsonify(automation.snapshot())

    @app.put("/api/automation")
    def configure_automation():
        try:
            return jsonify(automation.configure(payload()))
        except ValueError as exc:
            if isinstance(exc, ConfigError):
                raise
            return jsonify(error=str(exc)), 409

    if app.config.get("AUTOMATION_START", not app.testing):
        automation.start()
    return app
