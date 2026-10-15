"""The Caddy config. Yard posts one route with curl inside the caddy container."""

import json
import re
from pathlib import Path

from app.compose import APP_PORT
from app.errors import YardError

PROXY_PORT = 8443
CONFIG_PATH = "/config"
ROUTES_PATH = "/config/apps/http/servers/yard/routes"
_WINDOW = re.compile(r"^\d+(?:ms|s|m|h)$")
_DIAL_HOST = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$")
_CURL_MARK = "\n__YARD_"


def document() -> bytes:
    issuer: dict[str, object] = {"challenges": {"http": {"disabled": True}}, "module": "acme"}
    # No admin block: Caddy listens on localhost:2019 inside the container.
    document = {
        "apps": {
            "http": {
                "https_port": PROXY_PORT,
                "servers": {
                    "yard": {
                        "automatic_https": {"disable_redirects": True},
                        "listen": [f":{PROXY_PORT}"],
                        "routes": [],
                    }
                },
            },
            "tls": {"automation": {"policies": [{"issuers": [issuer]}]}},
        },
    }
    return json.dumps(document, indent="\t", sort_keys=True).encode() + b"\n"


def curl_words(method: str, path: str, has_body: bool) -> list[str]:
    """curl inside the caddy container. The status comes back on the last line."""
    words = [
        "curl",
        "-sS",
        "-o",
        "-",
        "-w",
        "\\n__YARD_%{http_code}",
        "-X",
        method,
        "-H",
        "Content-Type: application/json",
    ]
    if has_body:
        words += ["--data-binary", "@-"]
    words.append(f"http://127.0.0.1:2019{path}")
    return words


def split_curl(text: str) -> tuple[int, bytes]:
    head, mark, code = text.rpartition(_CURL_MARK)
    if not mark or not code.isdigit():
        return 502, text.encode()
    return int(code), head.encode()


def autosave_route_json(path: Path) -> str:
    """The routes array from the autosave, as JSON. A missing file is `[]`."""
    if not path.is_file():
        return "[]"
    try:
        doc = json.loads(path.read_text() or "{}")
    except json.JSONDecodeError:
        return "[]"
    if isinstance(doc, list):
        return json.dumps(doc)
    apps = doc.get("apps") if isinstance(doc, dict) else None
    http = apps.get("http") if isinstance(apps, dict) else None
    servers = http.get("servers") if isinstance(http, dict) else None
    routes: list[object] = []
    if isinstance(servers, dict):
        for server in servers.values():
            if not isinstance(server, dict):
                continue
            found = server.get("routes") or []
            if isinstance(found, list):
                routes.extend(found)
    return json.dumps(routes)


def routes_from_autosave(path: Path) -> list[dict[str, str]]:
    """The routes Caddy saved. A missing file is an empty list."""
    return read_routes(autosave_route_json(path))


def route_id(host: str) -> str:
    return f"yard-{host}"


def normalize_dial(text: str) -> str:
    """A container dial. A bare name uses port 8000."""
    raw = text.strip()
    for prefix in ("http://", "https://"):
        if raw.startswith(prefix):
            raw = raw[len(prefix) :]
    raw = raw.split("/", 1)[0]
    host, sep, port = raw.rpartition(":")
    if not sep:
        host, port = raw, str(APP_PORT)
    if not _DIAL_HOST.fullmatch(host) or not port.isdigit() or not 1 <= int(port) <= 65535:
        raise YardError(
            400,
            "bad_dial",
            "dial must be a container name or name:port",
            context={"dial": text},
            help_text="Use the container name. Add :port when it is not 8000.",
            example="heldso:8000",
        )
    return f"{host}:{port}"


def check_window(text: str) -> str:
    window = text.strip() or "10s"
    if _WINDOW.fullmatch(window):
        return window
    raise YardError(
        400,
        "bad_window",
        "try must be a duration",
        context={"try": text},
        help_text="Use a number and ms, s, m, or h.",
        example="10s",
    )


def proxy_route(host: str, dial: str, window: str = "10s") -> dict[str, object]:
    """One site. The id is the host, so a second write replaces this route only."""
    return {
        "@id": route_id(host),
        "handle": [
            {
                "handler": "reverse_proxy",
                "lb_try_duration": window,
                "lb_try_interval": "250ms",
                "upstreams": [{"dial": dial}],
            }
        ],
        "match": [{"host": [host]}],
    }


def read_routes(raw: bytes | str) -> list[dict[str, str]]:
    text = raw.decode() if isinstance(raw, bytes) else raw
    try:
        rows = json.loads(text or "[]")
    except json.JSONDecodeError:
        return []
    if not isinstance(rows, list):
        return []
    found: list[dict[str, str]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        host = _match_host(row)
        if not host:
            continue
        ident = row.get("@id")
        found.append({"host": host, "dial": _dial(row), "id": ident if isinstance(ident, str) else ""})
    return found


def find_route(raw: bytes | str, host: str) -> tuple[int, str] | None:
    """The index of this host in the routes array, and its stored id."""
    text = raw.decode() if isinstance(raw, bytes) else raw
    try:
        rows = json.loads(text or "[]")
    except json.JSONDecodeError:
        return None
    if not isinstance(rows, list):
        return None
    for index, row in enumerate(rows):
        if isinstance(row, dict) and _match_host(row) == host:
            ident = row.get("@id")
            return index, ident if isinstance(ident, str) else ""
    return None


def _match_host(row: dict[str, object]) -> str:
    match = row.get("match")
    if not isinstance(match, list) or not match or not isinstance(match[0], dict):
        return ""
    names = match[0].get("host")
    if isinstance(names, list) and names:
        return str(names[0])
    return ""


def _dial(row: dict[str, object]) -> str:
    handle = row.get("handle")
    if not isinstance(handle, list):
        return ""
    for item in handle:
        if not isinstance(item, dict):
            continue
        upstreams = item.get("upstreams")
        if isinstance(upstreams, list) and upstreams and isinstance(upstreams[0], dict):
            return str(upstreams[0].get("dial") or "")
    return ""
