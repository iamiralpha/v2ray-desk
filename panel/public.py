"""Read-only, intentionally public configuration browsing and link copying."""
import math
import re

from flask import Blueprint, jsonify, request

from .parser import ConfigError
from .store import connect
from .nodes import node_views, public_nodes


def create_public_blueprint(db_path):
    public = Blueprint("public", __name__)

    @public.get("/api/public/configs")
    def configs():
        values = {key: request.args.get(key, "") for key in ("country", "protocol", "status", "gemini", "q", "sort")}
        allowed = {
            "protocol": {"", "vless", "vmess", "trojan", "ss"},
            "status": {"", "healthy", "failed", "pending", "testing", "invalid", "error"},
            "gemini": {"", "http200", "blocked", "limited", "login", "redirect", "unreachable", "other", "untested"},
            "sort": {"", "latency", "newest"},
        }
        page_text = request.args.get("page", "1")
        if (any(values[key] not in choices for key, choices in allowed.items())
                or not re.fullmatch(r"(?:[A-Z]{2}|unknown)?", values["country"])
                or len(values["q"]) > 200 or not re.fullmatch(r"[1-9][0-9]{0,5}", page_text)):
            raise ConfigError("Invalid public list filter.")
        clauses, params = [], []
        for field in ("protocol", "status", "gemini"):
            if values[field]:
                column = "gemini_status" if field == "gemini" else field
                clauses.append(column + "=?")
                params.append(values[field])
        if values["country"]:
            clauses.append("COALESCE(country,'unknown')=?")
            params.append(values["country"])
        if values["q"]:
            clauses.append("instr(lower(name),?)>0")
            params.append(values["q"].lower())
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        order = "id DESC" if values["sort"] == "newest" else "CASE WHEN status='healthy' AND latency_ms IS NOT NULL THEN 0 ELSE 1 END,latency_ms,id"
        with connect(db_path) as db:
            total = db.execute("SELECT COUNT(*) FROM configs" + where, params).fetchone()[0]
            pages = max(1, math.ceil(total / 50))
            page = min(int(page_text), pages)
            rows = db.execute("""SELECT id,name,protocol,status,latency_ms,country,gemini_status,gemini_code,checked_at
                                 FROM configs""" + where + " ORDER BY " + order + " LIMIT 50 OFFSET ?",
                              [*params, (page - 1) * 50]).fetchall()
            countries = [dict(row) for row in db.execute("SELECT country,COUNT(*) AS count FROM configs GROUP BY country")]
            stats = dict(db.execute("""SELECT COUNT(*) AS total,
                COALESCE(SUM(status='healthy'),0) AS healthy,
                MIN(CASE WHEN status='healthy' THEN latency_ms END) AS best FROM configs""").fetchone())
            items = {row["id"]: dict(row) for row in rows}
            node_views(db, items)
            nodes = public_nodes(db)
        return jsonify(configs=list(items.values()), nodes=nodes, total=total, page=page, pages=pages,
                       countries=countries, stats=stats)

    @public.get("/api/public/configs/<int:config_id>")
    def config(config_id):
        with connect(db_path) as db:
            row = db.execute("SELECT uri FROM configs WHERE id=?", (config_id,)).fetchone()
        if not row:
            return jsonify(error="کانفیگ پیدا نشد."), 404
        return jsonify(uri=row["uri"])

    @public.get("/api/public/copy")
    def copy_configs():
        raw_ids = request.args.get("ids", "").split(",")
        if (not 1 <= len(raw_ids) <= 500
                or any(not re.fullmatch(r"[1-9][0-9]{0,17}", value) for value in raw_ids)):
            raise ConfigError("بین ۱ تا ۵۰۰ شناسهٔ معتبر کانفیگ انتخاب کنید.")
        ids = list(dict.fromkeys(int(value) for value in raw_ids))
        placeholders = ",".join("?" for _ in ids)
        with connect(db_path) as db:
            rows = db.execute("SELECT id,uri FROM configs WHERE id IN (" + placeholders + ")", ids).fetchall()
        by_id = {row["id"]: dict(row) for row in rows}
        return jsonify(configs=[by_id[config_id] for config_id in ids if config_id in by_id])

    return public
