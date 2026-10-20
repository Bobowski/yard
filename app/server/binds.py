"""The binds: one public host for one dial, written through the Caddy admin socket."""

from dataclasses import dataclass
from urllib.parse import quote

from stario import json

from app.server import caddy, journal, podman
from app.server.yard import Yard
from app.shared import YardError, check_domain, check_window


@dataclass(slots=True)
class Bind:
    host: str
    dial: str
    id: str
    window: str
    state: str = ""


async def read(yard: Yard) -> list[Bind]:
    """The bind list from the admin socket. No container state here."""
    _code, raw = await yard.caddy.request("GET", caddy.ROUTES_PATH)
    return [Bind(row["host"], row["dial"], row["id"], row["try"]) for row in caddy.read_routes(raw)]


async def list_binds(yard: Yard) -> list[Bind]:
    found = await read(yard)
    listed = await _containers(yard)
    for bind in found:
        name = bind.dial.split(":", 1)[0] if bind.dial else ""
        bind.state = _state(name, listed)
    return found


async def put_bind(yard: Yard, host: str, dial: str, window: str, *, actor: str = "") -> Bind:
    """One public name for one dial. The same name replaces that bind. Another name adds a bind."""
    host = _public_host(host)
    target = caddy.normalize_dial(dial)
    span = check_window(window)
    body = json.dumps(caddy.proxy_route(host, target, span)).encode()
    found = await _find(yard, host)
    if found is None:
        await yard.caddy.request("POST", caddy.ROUTES_PATH, body)
    else:
        # A stored id is one object. An older route has no id, so the write uses its index.
        index, ident = found
        path = f"/id/{quote(ident, safe='')}" if ident else f"{caddy.ROUTES_PATH}/{index}"
        await yard.caddy.request("PUT", path, body)
    bind = Bind(host, target, caddy.route_id(host), span)
    bind.state = _state(target.split(":", 1)[0], await _containers(yard))
    journal.send(yard.config, "bind.put", f"bind {host}", host=host, dial=target, actor=actor)
    return bind


async def delete_bind(yard: Yard, host: str, *, actor: str = "") -> str:
    """Remove one public name."""
    host = _public_host(host)
    found = await _find(yard, host)
    if found is None:
        raise YardError(
            404,
            "not_found",
            "route does not exist",
            context={"host": host},
            help_text="Run yard bind ls to see the binds.",
            example=f"yard bind rm {host}",
        )
    index, ident = found
    path = f"/id/{quote(ident, safe='')}" if ident else f"{caddy.ROUTES_PATH}/{index}"
    await yard.caddy.request("DELETE", path)
    journal.send(yard.config, "bind.delete", f"unbind {host}", host=host, actor=actor)
    return host


async def _find(yard: Yard, host: str) -> tuple[int, str] | None:
    _code, raw = await yard.caddy.request("GET", caddy.ROUTES_PATH)
    return caddy.find_route(raw, host)


async def _containers(yard: Yard) -> dict[str, podman.Container]:
    try:
        return podman.parse_ps(await yard.podman.run("ps", "-a", "--format", "json"))
    except YardError:
        return {}


def _state(name: str, listed: dict[str, podman.Container]) -> str:
    """The dial host is the container name. Compose names the app `<name>-app-1`."""
    item = listed.get(name)
    if item is None:
        return "absent"
    return "up" if item.running else "down"


def _public_host(host: str) -> str:
    host = host.strip()
    check_domain(host)
    if host:
        return host
    raise YardError(
        400,
        "bad_domain",
        "domain must be a hostname",
        help_text="Use a lowercase hostname.",
        example="heldso.example.com",
    )
