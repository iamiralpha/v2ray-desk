"""Verified public subscription sources and idempotent catalog installation."""
from urllib.parse import urlsplit

from .parser import ConfigError


PROVIDERS = [
    {"id": "barry", "name": "Barry"},
    {"id": "delta", "name": "Delta"},
    {"id": "epodonios", "name": "Epodonios"},
]
PRESETS = [
    {"key": f"barry-{index}", "provider": "barry", "name": f"Barry · Sub{index}",
     "url": f"https://raw.githubusercontent.com/barry-far/V2ray-config/main/Sub{index}.txt", "enabled": index <= 2}
    for index in range(1, 9)
] + [
    {"key": f"delta-{index:03}", "provider": "delta", "name": f"Delta · Batch {index:03}",
     "url": f"https://raw.githubusercontent.com/Delta-Kronecker/V2ray-Config/main/config/batches/v2ray/batch_{index:03}.txt",
     "enabled": index <= 2}
    for index in range(1, 12)
] + [
    {"key": f"epodonios-{index}", "provider": "epodonios", "name": f"Epodonios · Sub{index}",
     "url": f"https://raw.githubusercontent.com/Epodonios/v2ray-configs/main/Sub{index}.txt", "enabled": index <= 2}
    for index in range(1, 9)
]
SEED_MARKER = "subscription_presets_seeded_v1"


def url_identity(url):
    """Match GitHub raw URL aliases without folding case-sensitive file paths."""
    try:
        parsed = urlsplit(url)
        parts = parsed.path.lstrip("/").split("/")
        if parsed.scheme not in {"http", "https"} or parsed.query or parsed.fragment:
            return ("url", url)
        if parsed.username is not None or parsed.password is not None or parsed.port not in {None, 80, 443}:
            return ("url", url)
        if parsed.hostname in {"raw.githubusercontent.com", "raw.github.com"} and len(parts) >= 4:
            owner, repository, *tail = parts
        elif parsed.hostname in {"github.com", "www.github.com"} and len(parts) >= 5 and parts[2] == "raw":
            owner, repository, _, *tail = parts
        else:
            return ("url", url)
        if tail[:2] == ["refs", "heads"]:
            tail = tail[2:]
        if len(tail) < 2:
            return ("url", url)
        return ("github", owner.lower(), repository.lower(), *tail)
    except ValueError:
        return ("url", url)


def existing_sources(db):
    sources = {}
    for row in db.execute("SELECT id,name,url,enabled FROM subscriptions ORDER BY id"):
        sources.setdefault(url_identity(row["url"]), dict(row))
    return sources


def install_presets(db, presets, *, partial=False):
    """Add missing sources without replacing custom names or enable settings."""
    existing = existing_sources(db)
    missing = [item for item in presets if url_identity(item["url"]) not in existing]
    available = max(0, 100 - db.execute("SELECT COUNT(*) FROM subscriptions").fetchone()[0])
    if len(missing) > available and not partial:
        raise ConfigError("Subscription limit (100) reached.")
    inserted, enabled = [], []
    for item in missing[:available]:
        sub_id = db.execute("INSERT INTO subscriptions(name,url,enabled) VALUES (?,?,?)",
                            (item["name"], item["url"], bool(item["enabled"]))).lastrowid
        inserted.append(sub_id)
        if item["enabled"]:
            enabled.append(sub_id)
    return inserted, enabled
