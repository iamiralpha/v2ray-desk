import json
import secrets
import sqlite3
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def connect(path):
    db = sqlite3.connect(path, timeout=20)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    try:
        yield db
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


def initialize(data_dir):
    folder = Path(data_dir)
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    folder.chmod(0o700)
    path = folder / "panel.sqlite3"
    path.touch(mode=0o600, exist_ok=True)
    path.chmod(0o600)
    # Repair existing sidecars before SQLite opens them. New sidecars inherit
    # the database's private mode, independently of the process umask.
    for suffix in ("-wal", "-shm", "-journal"):
        try:
            path.with_name(path.name + suffix).chmod(0o600)
        except FileNotFoundError:
            pass
    with connect(path) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript("""
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS configs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            uri TEXT NOT NULL, name TEXT NOT NULL, protocol TEXT NOT NULL,
            host TEXT NOT NULL, port INTEGER NOT NULL,
            fingerprint TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL DEFAULT 'pending', latency_ms REAL,
            country TEXT, exit_ip TEXT, checked_at TEXT, error TEXT,
            successes INTEGER NOT NULL DEFAULT 0, samples INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
        );
        CREATE TABLE IF NOT EXISTS login_attempts (ip TEXT PRIMARY KEY, count INTEGER, started REAL);
        CREATE TABLE IF NOT EXISTS subscriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, url TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1, updated_at TEXT, error TEXT,
            invalid_count INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS subscription_configs (
            subscription_id INTEGER NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
            config_id INTEGER NOT NULL REFERENCES configs(id) ON DELETE CASCADE,
            PRIMARY KEY(subscription_id,config_id)
        );
        """)
        db.execute("INSERT OR IGNORE INTO settings VALUES ('secret',?)", (secrets.token_hex(32),))
        db.execute("INSERT OR IGNORE INTO settings VALUES ('auth_version','1')")
        columns = {row["name"] for row in db.execute("PRAGMA table_info(configs)")}
        if "delay_codes" not in columns:
            db.execute("ALTER TABLE configs ADD COLUMN delay_codes TEXT NOT NULL DEFAULT '[]'")
        if "transport" not in columns:
            db.execute("ALTER TABLE configs ADD COLUMN transport TEXT NOT NULL DEFAULT ''")
            db.execute("ALTER TABLE configs ADD COLUMN security TEXT NOT NULL DEFAULT ''")
            from .parser import ConfigError, parse_uri
            for row in db.execute("SELECT id,uri FROM configs").fetchall():
                try:
                    parsed = parse_uri(row["uri"])
                    db.execute("UPDATE configs SET transport=?,security=? WHERE id=?",
                               (parsed["transport"], parsed["security"], row["id"]))
                except ConfigError:
                    pass
        if "delay_version" not in columns:
            db.execute("ALTER TABLE configs ADD COLUMN delay_version INTEGER NOT NULL DEFAULT 1")
            db.execute("ALTER TABLE configs ADD COLUMN delay_target TEXT")
            db.execute("ALTER TABLE configs ADD COLUMN delay_samples TEXT NOT NULL DEFAULT '[]'")
        if "manual" not in columns:
            db.execute("ALTER TABLE configs ADD COLUMN manual INTEGER NOT NULL DEFAULT 1")
        if "gemini_status" not in columns:
            db.execute("ALTER TABLE configs ADD COLUMN gemini_status TEXT NOT NULL DEFAULT 'untested'")
            db.execute("ALTER TABLE configs ADD COLUMN gemini_code INTEGER")
    return path


def setting(path, key):
    with connect(path) as db:
        row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None


def save_password(path, password_hash):
    with connect(path) as db:
        db.execute("INSERT OR REPLACE INTO settings VALUES ('password_hash',?)", (password_hash,))
        db.execute("UPDATE settings SET value=CAST(value AS INTEGER)+1 WHERE key='auth_version'")
        db.execute("DELETE FROM login_attempts")
