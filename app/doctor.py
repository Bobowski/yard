"""`yard doctor`: the host checks. Each returns ok and a one-line detail."""

import asyncio
import json
import shutil
import socket
import ssl
import stat
import time
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from app import caddy, podman
from app.config import Config
from app.errors import YardError
from app.render import format_bytes, format_duration
from app.store import Slot

if TYPE_CHECKING:
    from app.yard import Yard


async def run(yard: Yard) -> list[dict]:
    config, store = yard.config, yard.store
    checks = [
        _row("podman", await _podman(yard)),
        _row("network yard", await _exists(yard, "network", "yard")),
        _row("apply image", await _exists(yard, "image", "localhost/yard-apply")),
    ]

    slots = store.list_slots()
    hosts = [route.host for route in caddy.routes(slots, config.domain)]
    if config.caddy_sock is None:
        # A laptop run has no host network; the proxy checks do not apply.
        checks += [
            _row("caddy", (True, "skipped: no host")),
            _row("caddy routes", (True, "skipped: no host")),
            _row("acme", (True, "skipped: no host")),
        ]
    else:
        checks.append(_row("caddy", await _caddy_up(yard)))
        checks.append(_row("caddy routes", await _routes(yard, config.caddy_sock, hosts)))
        checks.append(_row("acme", await _acme()))
        yard_hosts = {slot.domain for slot in slots if slot.name == "yard"}
        for host in hosts:
            checks.append(_row(f"dns {host}", await _dns(host)))
            checks.append(_row(f"certificate {host}", _certificate(config, host)))
            path = "/health" if host in yard_hosts else "/"
            checks.append(_row(f"https {host}", await _https(host, path)))

    checks += [
        _row("disk", _disk(config)),
        _row("env files", _env_files(config, slots)),
        _row("hook token", (True, "exists") if config.token_file.is_file() else (False, "missing")),
    ]
    return checks


def _row(name: str, pair: tuple[bool, str]) -> dict:
    ok, detail = pair
    return {"name": name, "ok": ok, "detail": detail}


async def _podman(yard: Yard) -> tuple[bool, str]:
    try:
        info = podman.parse_info(await yard.podman.run("info", "--format", "json"))
    except YardError as exc:
        return False, str(exc)
    return True, f"podman {info.version}" if info.version else "podman"


async def _exists(yard: Yard, kind: str, name: str) -> tuple[bool, str]:
    try:
        await yard.podman.run(kind, "exists", name)
    except YardError:
        return False, f"{name} missing"
    return True, f"{name} exists"


async def _caddy_up(yard: Yard) -> tuple[bool, str]:
    try:
        found = podman.parse_ps(await yard.podman.run("ps", "-a", "--format", "json")).get("caddy")
    except YardError as exc:
        return False, str(exc)
    if found is None:
        return False, "absent"
    return (True, "running") if found.running else (False, f"{found.state} not running")


async def _routes(yard: Yard, sock: Path, hosts: list[str]) -> tuple[bool, str]:
    try:
        raw = await asyncio.to_thread(
            caddy.request,
            sock,
            "GET",
            "/config/apps/http/servers/yard/routes",
        )
        found = _route_hosts(raw)
    except Exception as exc:
        return False, str(exc)
    missing = sorted(set(hosts) - found)
    extra = sorted(found - set(hosts))
    if missing or extra:
        parts = [f"missing: {', '.join(missing)}" if missing else "", f"extra: {', '.join(extra)}" if extra else ""]
        return False, "  ".join(part for part in parts if part)
    return True, f"{len(found)} routes"


def _route_hosts(raw: bytes) -> set[str]:
    try:
        routes = json.loads(raw)
    except json.JSONDecodeError:
        return set()
    found = set()
    for route in routes if isinstance(routes, list) else []:
        if not isinstance(route, dict):
            continue
        match = route.get("match")
        if isinstance(match, list) and match and isinstance(match[0], dict):
            names = match[0].get("host")
            if isinstance(names, list) and names:
                found.add(str(names[0]))
    return found


async def _acme() -> tuple[bool, str]:
    try:
        conn = await asyncio.to_thread(socket.create_connection, ("acme-v02.api.letsencrypt.org", 443), 10)
        conn.close()
    except OSError as exc:
        return False, str(exc)
    return True, "acme-v02.api.letsencrypt.org:443 open"


async def _https(host: str, path: str) -> tuple[bool, str]:
    # Yard shares Caddy's network, so the check dials the service name.
    request = f"GET {path} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n".encode()

    def round_trip() -> bytes:
        with socket.create_connection(("caddy", 8443), 10) as raw:
            with ssl.create_default_context().wrap_socket(raw, server_hostname=host) as tls:
                tls.sendall(request)
                return tls.recv(4096).split(b"\r\n", 1)[0]

    try:
        line = await asyncio.to_thread(round_trip)
    except (OSError, ssl.SSLError) as exc:
        return False, str(exc)
    if line.startswith(b"HTTP/"):
        return True, line.decode(errors="replace").strip()
    return False, line.decode(errors="replace").strip() or "no answer"


async def _dns(host: str) -> tuple[bool, str]:
    try:
        found = await asyncio.to_thread(socket.getaddrinfo, host, 443)
    except OSError as exc:
        return False, str(exc)
    return True, str(found[0][4][0])


def _certificate(config: Config, host: str) -> tuple[bool, str]:
    root = config.data("caddy") / "lib" / "certificates"
    found = list(root.glob(f"*/{host}/{host}.crt")) if root.is_dir() else []
    if not found:
        return False, "not issued yet"
    age = timedelta(seconds=max(0, time.time() - found[0].stat().st_mtime))
    return True, f"issued {format_duration(age)} ago"


def _disk(config: Config) -> tuple[bool, str]:
    usage = shutil.disk_usage(config.root)
    detail = f"{format_bytes(usage.free)} free of {format_bytes(usage.total)}"
    if usage.free < usage.total * 0.1:
        return False, f"{detail}; try yard prune"
    return True, detail


def _env_files(config: Config, slots: list[Slot]) -> tuple[bool, str]:
    bad = []
    for slot in slots:
        path = config.env(slot.name)
        try:
            ok = path.is_file() and stat.S_IMODE(path.stat().st_mode) == 0o600
        except OSError:
            ok = False
        if not ok:
            bad.append(slot.name)
    if bad:
        return False, ", ".join(bad)
    return True, f"{len(slots)} files"
