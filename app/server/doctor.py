"""Host checks for `yard status --full`. Each returns ok and a one-line detail."""

import asyncio
import shutil
import socket
import ssl
import stat
import time
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from app.server import binds, journal, podman
from app.server.forges import Forge
from app.server.store import Config
from app.shared import YardError, duration, format_bytes, parse

if TYPE_CHECKING:
    from app.server.yard import Yard


@dataclass(slots=True)
class Check:
    name: str
    ok: bool
    detail: str


async def run(yard: Yard) -> list[Check]:
    config = yard.config
    checks = [
        _check("podman", await _podman(yard)),
        _check("network yard", await _exists(yard, "network", "yard")),
        _check("apply image", await _exists(yard, "image", "localhost/yard-apply")),
    ]

    if not config.boot:
        # A laptop run has no host network; the proxy checks do not apply.
        checks += [
            _check("caddy", (True, "skipped: no host")),
            _check("caddy admin", (True, "skipped: no host")),
            _check("caddy routes", (True, "skipped: no host")),
            _check("podman journald", (True, "skipped: no host")),
            _check("journal", (True, "skipped: no host")),
            _check("container logs", (True, "skipped: no host")),
            _check("yard events", (True, "skipped: no host")),
        ]
        hosts: list[str] = []
        yard_hosts: set[str] = set()
    else:
        checks.append(_check("caddy", await _caddy_up(yard)))
        checks.append(_check("caddy admin", await _caddy_admin(yard)))
        ok, detail, found = await _binds(yard)
        checks.append(_check("caddy routes", (ok, detail)))
        checks.append(_check("podman journald", await _podman_journald(yard)))
        checks.append(_check("journal", await _journal(yard)))
        checks.append(_check("container logs", await _container_logs(yard)))
        checks.append(_check("yard events", _yard_events(config)))
        hosts = [row.host for row in found]
        yard_hosts = {row.host for row in found if _yard_dial(row.dial)}
    if config.boot:
        for host in hosts:
            checks.append(_check(f"dns {host}", await _dns(host)))
            checks.append(_check(f"certificate {host}", _certificate(config, host)))
            path = "/health" if host in yard_hosts else "/"
            checks.append(_check(f"https {host}", await _https(host, path)))

    checks += await _forge_rows(yard)
    checks.append(_check("disk", _disk(config)))
    return checks


async def _forge_rows(yard: Yard) -> list[Check]:
    """Each forge: app, installation, token, and the last delivery and check.

    A forge nothing uses and nothing connects to earns no row at all.
    """
    used = {urlsplit(app.repo).hostname or "" for app in yard.store.list_apps()}
    rows = []
    for forge in yard.forges.values():
        if not forge.connected():
            if forge.host in used:
                rows.append(_check(forge.name, (False, f"not connected; run yard forge setup {forge.name}")))
            continue
        rows += [
            _check(f"{forge.name} app", await _forge_app(forge)),
            _check(f"{forge.name} installation", await _forge_installation(forge)),
            _check(f"{forge.name} token", await _forge_token(forge)),
            _check(f"{forge.name} webhook", (True, _ago(forge.last_delivery))),
            _check(f"{forge.name} check", (True, _ago(forge.last_check))),
        ]
    return rows


async def _forge_app(forge: Forge) -> tuple[bool, str]:
    try:
        body = await forge.get_app()
    except YardError as exc:
        return False, str(exc)
    name = body.get("displayName") or body.get("display_name") or body.get("name") or ""
    return True, str(name) if name else "ok"


async def _forge_installation(forge: Forge) -> tuple[bool, str]:
    try:
        installation = await forge.find_installation()
    except YardError as exc:
        return False, str(exc)
    if installation is None:
        return False, "no installation"
    if forge.selection(installation) != "all":
        return False, "selected repos only; new repos need a grant"
    target = installation.get("target")
    account = installation.get("account")
    slug = ""
    if isinstance(target, dict):
        slug = str(target.get("slug") or "")
    if not slug and isinstance(account, dict):
        slug = str(account.get("login") or "")
    return True, slug if slug else "all repos"


async def _forge_token(forge: Forge) -> tuple[bool, str]:
    try:
        await forge.token()
    except YardError as exc:
        return False, str(exc)
    return True, "ok"


def _ago(stamp: str) -> str:
    moment = parse(stamp)
    if moment is None:
        return "none since boot"
    age = timedelta(seconds=max(0, time.time() - moment.timestamp()))
    return f"{duration(age)} ago"


def _check(name: str, pair: tuple[bool, str]) -> Check:
    ok, detail = pair
    return Check(name, ok, detail)


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
        items = podman.parse_ps(await yard.podman.run("ps", "-a", "--format", "json")).values()
        found = next((item for item in items if item.app == "yard" and item.service == "caddy"), None)
    except YardError as exc:
        return False, str(exc)
    if found is None:
        return False, "absent"
    return (True, "running") if found.running else (False, f"{found.state} not running")


async def _caddy_admin(yard: Yard) -> tuple[bool, str]:
    try:
        await yard.caddy.request("GET", "/config/")
    except YardError as exc:
        return False, str(exc)
    return True, "ok"


async def _binds(yard: Yard) -> tuple[bool, str, list[binds.Bind]]:
    try:
        found = await binds.read(yard)
    except YardError as exc:
        return False, str(exc), []
    return True, f"{len(found)} routes", found


async def _podman_journald(yard: Yard) -> tuple[bool, str]:
    """Podman must write container logs and engine events to the journal."""
    try:
        info = podman.parse_info(await yard.podman.run("info", "--format", "json"))
    except YardError as exc:
        return False, str(exc)
    detail = f"logs {info.log_driver or 'unset'} · events {info.event_logger or 'unset'}"
    if info.log_driver != "journald" or info.event_logger != "journald":
        return False, f"{detail}; rerun install.sh"
    return True, detail


def _yard_events(config: Config) -> tuple[bool, str]:
    """Yard's own events go to the journald socket the container mounts."""
    path = config.journal_socket
    try:
        mode = path.stat().st_mode
    except OSError:
        return False, f"no socket at {path}; events fall back to stdout"
    if not stat.S_ISSOCK(mode):
        return False, f"{path} is not a socket"
    return True, str(path)


async def _journal(yard: Yard) -> tuple[bool, str]:
    try:
        await journal.read(yard.config, [], lines=0)
    except YardError as exc:
        return False, str(exc)
    return True, str(yard.config.journal_dir)


async def _container_logs(yard: Yard) -> tuple[bool, str]:
    """Every running container must already log to journald."""
    try:
        listed = podman.parse_ps(await yard.podman.run("ps", "--format", "json"))
        running = [item.name for item in listed.values() if item.running]
        if not running:
            return True, "0 containers"
        types = podman.parse_log_types(await yard.podman.run("inspect", *running))
    except YardError as exc:
        return False, str(exc)
    stale = [name for name in running if types.get(name, "") != "journald"]
    if stale:
        return False, f"redeploy to switch: {', '.join(sorted(stale))}"
    return True, f"{len(running)} containers"


def _yard_dial(dial: str) -> bool:
    host = dial.split(":", 1)[0]
    return host == "yard" or host.startswith("yard-app")


async def _https(host: str, path: str) -> tuple[bool, str]:
    # Yard shares Caddy's network, so the check dials the service name.
    request = f"GET {path} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n".encode()

    def round_trip() -> bytes:
        with socket.create_connection(("caddy", 443), 10) as raw:
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
    # Caddy's /data volume is lib/, and it writes under a caddy/ directory in it.
    root = config.caddy_data() / "caddy" / "certificates"
    found = list(root.glob(f"*/{host}/{host}.crt")) if root.is_dir() else []
    if not found:
        return False, "not issued yet"
    age = timedelta(seconds=max(0, time.time() - found[0].stat().st_mtime))
    return True, f"issued {duration(age)} ago"


def _disk(config: Config) -> tuple[bool, str]:
    usage = shutil.disk_usage(config.root)
    detail = f"{format_bytes(usage.free)} free of {format_bytes(usage.total)}"
    if usage.free < usage.total * 0.1:
        return False, f"{detail}; try yard prune"
    return True, detail
