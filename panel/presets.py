"""Verified public subscription sources and idempotent catalog installation."""
from urllib.parse import urlsplit

from .parser import ConfigError


PROVIDERS = [
    {"id": "barry", "name": "Barry"},
    {"id": "delta", "name": "Delta"},
    {"id": "alirewa", "name": "Alirewa"},
    {"id": "radikal", "name": "Radikal"},
    {"id": "mohamed", "name": "Mohamed"},
    {"id": "morpheus", "name": "Morpheus"},
    {"id": "chaishpish", "name": "Chaishpish"},
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
    {"key": "alirewa-1", "provider": "alirewa", "name": "Alirewa · Top 100", "url": "https://raw.githubusercontent.com/Alirewa/V2ray-Configs/main/sub1.txt", "enabled": True},
    {"key": "alirewa-2", "provider": "alirewa", "name": "Alirewa · Sub 2", "url": "https://raw.githubusercontent.com/Alirewa/V2ray-Configs/main/sub2.txt", "enabled": False},
    {"key": "radikal-1", "provider": "radikal", "name": "Radikal · Top 100", "url": "https://raw.githubusercontent.com/0xRadikal/Free-v2ray-Configs/main/top100.txt", "enabled": True},
    {"key": "radikal-2", "provider": "radikal", "name": "Radikal · Fast", "url": "https://raw.githubusercontent.com/0xRadikal/Free-v2ray-Configs/main/fast/configs.txt", "enabled": False},
    {"key": "mohamed-1", "provider": "mohamed", "name": "Mohamed · Batch 1", "url": "https://raw.githubusercontent.com/mrdevmohamed/v2ray-configs/main/Sub1.txt", "enabled": True},
    {"key": "mohamed-2", "provider": "mohamed", "name": "Mohamed · Batch 2", "url": "https://raw.githubusercontent.com/mrdevmohamed/v2ray-configs/main/Sub2.txt", "enabled": False},
    {"key": "morpheus-1", "provider": "morpheus", "name": "Morpheus · Mini", "url": "https://raw.githubusercontent.com/morpheusadam/v2ray-config/main/subs/bundles/mini.txt", "enabled": True},
    {"key": "morpheus-2", "provider": "morpheus", "name": "Morpheus · Medium", "url": "https://raw.githubusercontent.com/morpheusadam/v2ray-config/main/subs/bundles/medium.txt", "enabled": False},
    {"key": "chaishpish-1", "provider": "chaishpish", "name": "Chaishpish · Latest", "url": "https://raw.githubusercontent.com/chaishpish/free_server_v2ray/main/@TFMTProto", "enabled": True},
]
SEED_MARKER = "subscription_presets_seeded_v1"
ADDITIONAL_SEED_MARKER = "subscription_presets_extended_v1"
ADDITIONAL_PROVIDER_IDS = frozenset({"alirewa", "radikal", "mohamed", "morpheus", "chaishpish"})


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
