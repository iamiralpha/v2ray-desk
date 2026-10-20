import base64
import hashlib
import json
import re
import uuid
from urllib.parse import parse_qsl, unquote, urlsplit


class ConfigError(ValueError):
    pass


def decode64(value):
    try:
        value = re.sub(r"\s+", "", value)
        return base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True).decode("utf-8")
    except (ValueError, UnicodeError):
        raise ConfigError("دادهٔ Base64 معتبر نیست.") from None


def port_number(value):
    try:
        number = int(value)
        if not 1 <= number <= 65535:
            raise ValueError()
        return number
    except (ValueError, TypeError):
        raise ConfigError("پورت باید بین ۱ و ۶۵۵۳۵ باشد.") from None


def hostname(value):
    value = str(value or "").strip().strip("[]")
    if not value or len(value) > 253 or re.search(r"[\s/@\\?#\x00-\x1f]", value):
        raise ConfigError("آدرس سرور معتبر نیست.")
    return value


def user_id(value):
    try:
        return str(uuid.UUID(str(value)))
    except ValueError:
        raise ConfigError("شناسهٔ UUID معتبر نیست.") from None


def stream(options, host, default_security="none"):
    network = options.get("type", "tcp") or "tcp"
    network = {"raw": "tcp", "websocket": "ws", "splithttp": "xhttp"}.get(network, network)
    if network not in {"tcp", "ws", "grpc", "httpupgrade", "xhttp"}:
        raise ConfigError("این نوع انتقال پشتیبانی نمی‌شود؛ TCP، WS، gRPC، HTTPUpgrade و XHTTP مجازند.")
    security = options.get("security", default_security) or "none"
    if security not in {"none", "tls", "reality"}:
        raise ConfigError("نوع امنیت پشتیبانی نمی‌شود؛ TLS و REALITY پشتیبانی می‌شوند.")
    if options.get("allowInsecure", options.get("insecure", "0")).lower() in {"1", "true"}:
        raise ConfigError("کانفیگ با غیرفعال‌کردن بررسی گواهی پذیرفته نمی‌شود؛ allowInsecure را اصلاح کنید.")
    result = {"network": network, "security": security}
    sni = options.get("sni") or options.get("serverName") or options.get("host") or host
    if security == "tls":
        result["tlsSettings"] = {"serverName": sni, "allowInsecure": False}
        if options.get("fp"):
            result["tlsSettings"]["fingerprint"] = options["fp"]
        if options.get("alpn"):
            result["tlsSettings"]["alpn"] = options["alpn"].split(",")
    elif security == "reality":
        if network not in {"tcp", "grpc", "xhttp"}:
            raise ConfigError("REALITY با این نوع انتقال سازگار نیست.")
        if not options.get("pbk"):
            raise ConfigError("کلید عمومی REALITY (pbk) وارد نشده است.")
        key = options["pbk"]
        if not re.fullmatch(r"[A-Za-z0-9_-]{43}=?", key) or len(base64.urlsafe_b64decode(key + "=" * (-len(key) % 4))) != 32:
            raise ConfigError("REALITY public key must be a 32-byte Base64URL key.")
        short_id = options.get("sid", "")
        if len(short_id) > 16 or len(short_id) % 2 or not re.fullmatch(r"[0-9a-fA-F]*", short_id):
            raise ConfigError("REALITY short ID must be even-length hexadecimal, up to 16 characters.")
        result["realitySettings"] = {
            "serverName": sni, "fingerprint": options.get("fp") or "chrome",
            "publicKey": options["pbk"], "shortId": options.get("sid", ""),
            "spiderX": options.get("spx", ""),
        }
    path = options.get("path") or "/"
    transport_host = options.get("host", "")
    if network == "ws":
        result["wsSettings"] = {"path": path}
        if transport_host:
            result["wsSettings"]["headers"] = {"Host": transport_host}
    elif network == "httpupgrade":
        result["httpupgradeSettings"] = {"path": path, "host": transport_host}
    elif network == "grpc":
        result["grpcSettings"] = {
            "serviceName": options.get("serviceName", ""),
            "multiMode": options.get("mode") == "multi",
        }
        if options.get("authority"):
            result["grpcSettings"]["authority"] = options["authority"]
    elif network == "xhttp":
        mode = options.get("mode") or "auto"
        if mode not in {"auto", "packet-up", "stream-up", "stream-one"}:
            raise ConfigError("حالت XHTTP معتبر نیست.")
        if options.get("extra"):
            raise ConfigError("XHTTP با extra سفارشی در این نسخه پشتیبانی نمی‌شود.")
        result["xhttpSettings"] = {"path": path, "host": transport_host, "mode": mode}
    elif options.get("headerType", "none") != "none":
        if options["headerType"] != "http":
            raise ConfigError("نوع هدر TCP پشتیبانی نمی‌شود.")
        result["tcpSettings"] = {"header": {"type": "http", "request": {
            "path": [path], "headers": {"Host": transport_host.split(",") if transport_host else [host]},
        }}}
    return result


def parse_uri(raw):
    raw = str(raw or "").strip()
    if not raw or len(raw) > 16384 or re.search(r"[\r\n\x00]", raw):
        raise ConfigError("هر کانفیگ باید یک لینک معتبر در یک خط باشد.")
    scheme = raw.split("://", 1)[0].lower()
    if scheme not in {"vmess", "vless", "trojan", "ss"}:
        raise ConfigError("فقط لینک‌های vmess، vless، trojan و ss پشتیبانی می‌شوند.")
    try:
        if scheme == "vmess":
            item = json.loads(decode64(raw.split("://", 1)[1]))
            if not isinstance(item, dict):
                raise ConfigError("محتوای VMess باید یک شیء JSON باشد.")
            allowed = {"v", "ps", "add", "port", "id", "aid", "scy", "net", "type",
                       "host", "path", "tls", "sni", "alpn", "fp", "allowInsecure"}
            if set(item) - allowed:
                raise ConfigError("این VMess تنظیمات اضافی پشتیبانی‌نشده دارد.")
            host, port = hostname(item.get("add")), port_number(item.get("port"))
            options = {
                "type": str(item.get("net") or "tcp"), "security": str(item.get("tls") or "none"),
                "headerType": str(item.get("type") or "none"), "host": str(item.get("host") or ""),
                "path": str(item.get("path") or "/"), "sni": str(item.get("sni") or ""),
                "alpn": str(item.get("alpn") or ""), "fp": str(item.get("fp") or ""),
                "allowInsecure": str(item.get("allowInsecure", "0")),
            }
            if options["type"] == "grpc":
                options["serviceName"] = str(item.get("path") or "")
            cipher = str(item.get("scy") or "auto")
            if cipher not in {"auto", "aes-128-gcm", "chacha20-poly1305", "none", "zero"}:
                raise ConfigError("رمزنگاری VMess پشتیبانی نمی‌شود.")
            aid = int(item.get("aid") or 0)
            if aid != 0:
                raise ConfigError("VMess قدیمی با alterId غیرصفر پشتیبانی نمی‌شود.")
            name = str(item.get("ps") or host)
            outbound = {"protocol": "vmess", "settings": {"vnext": [{
                "address": host, "port": port,
                "users": [{"id": user_id(item.get("id")), "alterId": 0, "security": cipher}],
            }]}, "streamSettings": stream(options, host)}
        else:
            # Legacy Shadowsocks encodes the entire method:password@host:port.
            value = raw
            if scheme == "ss" and "@" not in raw.split("#", 1)[0]:
                encoded, _, fragment = raw[5:].partition("#")
                value = "ss://" + decode64(encoded) + ("#" + fragment if fragment else "")
            url = urlsplit(value)
            host, port = hostname(url.hostname), port_number(url.port)
            pairs = parse_qsl(url.query, keep_blank_values=True)
            if len({key for key, _ in pairs}) != len(pairs):
                raise ConfigError("پارامتر تکراری در لینک وجود دارد.")
            options = dict(pairs)
            allowed = {"type", "security", "sni", "serverName", "fp", "alpn", "pbk", "sid", "spx",
                       "host", "path", "serviceName", "authority", "mode", "headerType", "flow",
                       "encryption", "allowInsecure", "insecure", "extra"}
            if set(options) - allowed:
                raise ConfigError("لینک تنظیمات اضافی پشتیبانی‌نشده دارد.")
            name = unquote(url.fragment) or host
            credential = unquote(url.netloc.rsplit("@", 1)[0])
            if "@" not in url.netloc or not credential:
                raise ConfigError("اطلاعات ورود کانفیگ وجود ندارد.")
            if scheme in {"vless", "trojan"}:
                transport = stream(options, host, "tls" if scheme == "trojan" else "none")
                if scheme == "vless":
                    user = {"id": user_id(credential), "encryption": options.get("encryption") or "none"}
                    if options.get("flow"):
                        if options["flow"] not in {"xtls-rprx-vision", "xtls-rprx-vision-udp443"}:
                            raise ConfigError("flow پشتیبانی نمی‌شود.")
                        if transport["security"] not in {"tls", "reality"} or transport["network"] not in {"tcp", "xhttp"}:
                            raise ConfigError("Vision flow requires TLS/REALITY with a compatible TCP or XHTTP transport.")
                        user["flow"] = options["flow"]
                    settings = {"vnext": [{"address": host, "port": port, "users": [user]}]}
                else:
                    settings = {"servers": [{"address": host, "port": port, "password": credential}]}
                outbound = {"protocol": scheme, "settings": settings, "streamSettings": transport}
            else:
                if options:
                    raise ConfigError("Shadowsocks با پارامتر اضافه یا plugin پشتیبانی نمی‌شود.")
                if ":" not in credential:
                    credential = decode64(credential)
                method, separator, password = credential.partition(":")
                if not separator or not password:
                    raise ConfigError("رمز Shadowsocks معتبر نیست.")
                if method not in {"aes-128-gcm", "aes-256-gcm", "chacha20-ietf-poly1305",
                                  "2022-blake3-aes-128-gcm", "2022-blake3-aes-256-gcm",
                                  "2022-blake3-chacha20-poly1305"}:
                    raise ConfigError("روش رمزنگاری Shadowsocks پشتیبانی نمی‌شود.")
                if method.startswith("2022-"):
                    length = 16 if method == "2022-blake3-aes-128-gcm" else 32
                    try:
                        if any(len(base64.b64decode(key, validate=True)) != length for key in password.split(":")):
                            raise ValueError()
                    except ValueError:
                        raise ConfigError("Shadowsocks 2022 password must contain Base64 keys of the required length.") from None
                outbound = {"protocol": "shadowsocks", "settings": {"servers": [{
                    "address": host, "port": port, "method": method, "password": password,
                }]}}
        if len(name) > 200:
            name = name[:200]
        outbound["tag"] = "proxy"
        digest = hashlib.sha256(json.dumps(outbound, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        stream_settings = outbound.get("streamSettings", {})
        return {"uri": raw, "name": name, "protocol": scheme, "host": host, "port": port,
                "transport": stream_settings.get("network", "tcp"), "security": stream_settings.get("security", "none"),
                "fingerprint": digest, "outbound": outbound}
    except ConfigError:
        raise
    except (ValueError, TypeError, KeyError, AttributeError):
        raise ConfigError("ساختار لینک معتبر نیست؛ آدرس، پورت و مشخصات ورود را بررسی کنید.") from None


def import_lines(text):
    text = str(text or "").strip().lstrip("\ufeff")
    if "://" not in text and text:
        text = decode64(text)
    lines = [(index, line.strip()) for index, line in enumerate(text.splitlines(), 1)
             if line.strip() and not line.lstrip().startswith("#")]
    if not lines:
        raise ConfigError("فهرست خالی است.")
    if len(lines) > 2000:
        raise ConfigError("در هر بار ورود حداکثر ۲۰۰۰ کانفیگ مجاز است.")
    return lines
