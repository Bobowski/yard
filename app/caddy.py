"""The Caddy config. Yard writes a document and posts it to the admin socket."""

import asyncio
import json
import socket
import stat
import time
from dataclasses import dataclass
from pathlib import Path

from app.compose import APP_PORT
from app.store import Slot

PROXY_PORT = 8443
# The admin socket lives inside the Caddy container; ops/caddy.json is the first config.
ADMIN_SOCK = "/run/caddy/caddy.sock"


@dataclass(slots=True)
class Route:
    host: str
    dial: str


def routes(slots: list[Slot], domain: str) -> list[Route]:
    found = [Route(slot.domain, f"{slot.name}:{APP_PORT}") for slot in slots if slot.domain and not slot.job]
    if domain and all(route.host != domain for route in found):
        found.append(Route(domain, f"yard:{APP_PORT}"))
    return found


def document(routes: list[Route]) -> bytes:
    http_routes = [
        {
            "handle": [
                {
                    "handler": "reverse_proxy",
                    "lb_try_duration": "10s",
                    "lb_try_interval": "250ms",
                    "upstreams": [{"dial": route.dial}],
                }
            ],
            "match": [{"host": [route.host]}],
        }
        for route in routes
    ]
    issuer: dict[str, object] = {"challenges": {"http": {"disabled": True}}, "module": "acme"}
    document = {
        "admin": {"listen": f"unix/{ADMIN_SOCK}|0600"},
        "apps": {
            "http": {
                "https_port": PROXY_PORT,
                "servers": {
                    "yard": {
                        "automatic_https": {"disable_redirects": True},
                        "listen": [f":{PROXY_PORT}"],
                        "routes": http_routes,
                    }
                },
            },
            "tls": {"automation": {"policies": [{"issuers": [issuer]}]}},
        },
    }
    return json.dumps(document, indent="\t", sort_keys=True).encode() + b"\n"


async def wait_socket(sock: Path, seconds: float = 15) -> None:
    """Caddy creates the admin socket after the compose project starts it."""
    deadline = time.monotonic() + seconds
    while True:
        if sock.exists() and stat.S_ISSOCK(sock.stat().st_mode):
            return
        if time.monotonic() > deadline:
            raise RuntimeError(f"caddy socket {sock} is not ready")
        await asyncio.sleep(0.1)


def request(sock: Path, method: str, path: str, body: bytes = b"") -> bytes:
    """Blocking HTTP over the unix admin socket. Call it inside to_thread."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(10)
        client.connect(str(sock))
        header = (
            f"{method} {path} HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n"
        ).encode()
        client.sendall(header + body)
        data = bytearray()
        while chunk := client.recv(65536):
            data += chunk
    head, _, payload = bytes(data).partition(b"\r\n\r\n")
    code = int(head.split(b" ", 2)[1])
    if code >= 300:
        raise RuntimeError(f"caddy {method} {path}: {code}")
    return payload


def load(sock: Path, body: bytes) -> None:
    """Replace the running config. Blocking; call it inside to_thread."""
    request(sock, "POST", "/load", body)
