"""Authenticated pull-only test nodes; their results never alter primary tests."""
import hashlib
import math
import os
import re
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path

from flask import Blueprint, jsonify, request

from .parser import ConfigError
from .store import connect

MAX_BATCH = 128
MAX_NODES = 20
LEASE_SECONDS = 1800


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def node_views(db, items):
    """Attach only results for the current successful primary measurement."""
    for item in items.values():
        item["node_results"] = {}
    if not items:
        return
    ids = list(items)
    # Chunk to keep SQLite parameter limits bounded on large installations.
    for start in range(0, len(ids), 400):
        chunk = ids[start:start + 400]
        sql = """SELECT r.node_id,r.config_id,r.status,r.latency_ms,r.checked_at
                 FROM node_results r JOIN configs c ON c.id=r.config_id
                 WHERE c.status='healthy' AND r.fingerprint=c.fingerprint
                   AND r.source_checked_at=c.checked_at AND r.config_id IN (""" + ",".join("?" for _ in chunk) + ")"
        for row in db.execute(sql, chunk):
            items[row["config_id"]]["node_results"][str(row["node_id"])] = {
                "status": row["status"], "latency_ms": row["latency_ms"], "checked_at": row["checked_at"]}


def public_nodes(db):
    return [dict(row) for row in db.execute("SELECT id,name,last_seen FROM nodes ORDER BY id")]


def certificate_fingerprint():
    path = Path(os.environ.get("PANEL_CERT_FINGERPRINT_FILE", "/opt/v2ray-desk/cert.sha256"))
    try:
        value = path.read_text(encoding="ascii").strip().replace(":", "").lower()
    except (OSError, UnicodeError):
        return None
    return value if re.fullmatch(r"[0-9a-f]{64}", value) else None


def create_nodes_blueprint(db_path, manager):
    nodes = Blueprint("node_api", __name__)

    def authenticate():
        header = request.headers.get("Authorization", "")
        match = re.fullmatch(r"Bearer ([A-Za-z0-9_-]{43})", header)
        if not match:
            return None
        digest = hashlib.sha256(match[1].encode("ascii")).hexdigest()
        with connect(db_path) as db:
            row = db.execute("SELECT id FROM nodes WHERE token_hash=?", (digest,)).fetchone()
        return row["id"] if row else None

    def body():
        if request.content_length is not None and request.content_length > 65536:
            raise ConfigError("Node request is too large.")
        value = request.get_json(silent=True)
        if not isinstance(value, dict):
            raise ConfigError("Invalid node request.")
        return value

    @nodes.get("/api/nodes")
    def list_nodes():
        with connect(db_path) as db:
            return jsonify(nodes=public_nodes(db), certificate_fingerprint=certificate_fingerprint())

    @nodes.post("/api/nodes")
    def create():
        value = body()
        name = value.get("name")
        if (set(value) != {"name"} or not isinstance(name, str) or not name.strip()
                or len(name) > 40 or any(ord(c) < 32 for c in name)):
            raise ConfigError("Node name must contain 1–40 characters.")
        token = secrets.token_urlsafe(32)
        digest = hashlib.sha256(token.encode("ascii")).hexdigest()
        with connect(db_path) as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT COUNT(*) FROM nodes").fetchone()[0] >= MAX_NODES:
                raise ConfigError("The node limit has been reached.")
            cursor = db.execute("INSERT INTO nodes(name,token_hash,created_at) VALUES (?,?,?)", (name.strip(), digest, now()))
            node = {"id": cursor.lastrowid, "name": name.strip(), "last_seen": None}
        return jsonify(node=node, token=token), 201

    @nodes.delete("/api/nodes/<int:node_id>")
    def delete(node_id):
        with manager.lock, connect(db_path) as db:
            cursor = db.execute("DELETE FROM nodes WHERE id=?", (node_id,))
            if not cursor.rowcount:
                return jsonify(error="Node not found."), 404
            manager.invalidate()
        return jsonify(ok=True)

    @nodes.post("/api/node/pull")
    def pull():
        node_id = authenticate()
        if node_id is None:
            return jsonify(error="Invalid node credentials."), 401
        if body():
            raise ConfigError("Invalid node request.")
        current = int(time.time())
        lease = secrets.token_urlsafe(24)
        with manager.lock, connect(db_path) as db:
            db.execute("BEGIN IMMEDIATE")
            # Recheck revocation after taking the transaction lock.
            if not db.execute("SELECT 1 FROM nodes WHERE id=?", (node_id,)).fetchone():
                return jsonify(error="Invalid node credentials."), 401
            db.execute("UPDATE nodes SET last_seen=? WHERE id=?", (now(), node_id))
            db.execute("""DELETE FROM node_assignments WHERE node_id=? AND (expires_at<=? OR NOT EXISTS
                (SELECT 1 FROM configs c WHERE c.id=node_assignments.config_id AND c.status='healthy'
                 AND c.fingerprint=node_assignments.fingerprint AND c.checked_at=node_assignments.source_checked_at))""",
                (node_id, current))
            candidates = db.execute("""SELECT c.id,c.uri,c.fingerprint,c.checked_at FROM configs c
                LEFT JOIN node_results r ON r.node_id=? AND r.config_id=c.id
                    AND r.fingerprint=c.fingerprint AND r.source_checked_at=c.checked_at
                LEFT JOIN node_assignments a ON a.node_id=? AND a.config_id=c.id
                WHERE c.status='healthy' AND c.checked_at IS NOT NULL AND r.config_id IS NULL
                    AND a.config_id IS NULL ORDER BY c.id LIMIT ?""", (node_id, node_id, MAX_BATCH)).fetchall()
            db.executemany("""INSERT INTO node_assignments(node_id,config_id,fingerprint,source_checked_at,lease,expires_at)
                              VALUES (?,?,?,?,?,?)""", [(node_id, row["id"], row["fingerprint"], row["checked_at"],
                                                        lease, current + LEASE_SECONDS) for row in candidates])
        return jsonify(lease=lease if candidates else None, configs=[{"id": row["id"], "uri": row["uri"]} for row in candidates])

    @nodes.post("/api/node/report")
    def report():
        node_id = authenticate()
        if node_id is None:
            return jsonify(error="Invalid node credentials."), 401
        data = body()
        lease, results = data.get("lease"), data.get("results")
        if (set(data) != {"lease", "results"} or not isinstance(lease, str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{32}", lease) or not isinstance(results, list)
                or not 1 <= len(results) <= MAX_BATCH):
            raise ConfigError("Invalid node result batch.")
        ids = set()
        for row in results:
            if (not isinstance(row, dict) or set(row) != {"id", "status", "latency_ms"}
                    or type(row["id"]) is not int or row["id"] < 1 or row["id"] in ids
                    or not isinstance(row["status"], str) or row["status"] not in {"healthy", "failed", "error", "invalid"}):
                raise ConfigError("Invalid node result.")
            latency = row["latency_ms"]
            if ((row["status"] == "healthy" and (type(latency) not in {int, float} or not 1 <= latency <= 120000
                                                   or not math.isfinite(latency)))
                    or (row["status"] != "healthy" and latency is not None)):
                raise ConfigError("Invalid node latency.")
            ids.add(row["id"])
        accepted, changed = 0, []
        with manager.lock, connect(db_path) as db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute("SELECT 1 FROM nodes WHERE id=?", (node_id,)).fetchone():
                return jsonify(error="Invalid node credentials."), 401
            assignments = {r["config_id"]: r for r in db.execute("""SELECT a.*,c.status,c.fingerprint AS current_fingerprint,
                c.checked_at FROM node_assignments a JOIN configs c ON c.id=a.config_id
                WHERE a.node_id=? AND a.lease=? AND a.expires_at>?""", (node_id, lease, int(time.time())))}
            if not ids.issubset(assignments):
                return jsonify(error="The node batch has expired or is invalid."), 409
            for item in results:
                assignment = assignments[item["id"]]
                if (assignment["status"] == "healthy" and assignment["current_fingerprint"] == assignment["fingerprint"]
                        and assignment["checked_at"] == assignment["source_checked_at"]):
                    db.execute("""INSERT INTO node_results(node_id,config_id,fingerprint,source_checked_at,status,latency_ms,checked_at)
                        VALUES (?,?,?,?,?,?,?) ON CONFLICT(node_id,config_id) DO UPDATE SET
                        fingerprint=excluded.fingerprint,source_checked_at=excluded.source_checked_at,status=excluded.status,
                        latency_ms=excluded.latency_ms,checked_at=excluded.checked_at""",
                        (node_id, item["id"], assignment["fingerprint"], assignment["source_checked_at"], item["status"],
                         round(item["latency_ms"]) if item["latency_ms"] is not None else None, now()))
                    accepted += 1
                    changed.append(item["id"])
                db.execute("DELETE FROM node_assignments WHERE node_id=? AND config_id=?", (node_id, item["id"]))
            db.execute("UPDATE nodes SET last_seen=? WHERE id=?", (now(), node_id))
        for config_id in changed:
            manager.changed(config_id)
        return jsonify(accepted=accepted)

    return nodes
