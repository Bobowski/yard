"""Service-level helpers: a Yard on a temp root, and a fake Caddy admin."""

import json
from pathlib import Path

from app.server import caddy
from app.server.caddy import Caddy
from app.server.store import Config, Store
from app.server.yard import Yard


def make_yard(
    tmp_path: Path, *, skip: bool = True, boot: bool = False, podman_bin: str = "yard-podman-missing"
) -> Yard:
    config = Config(
        root=tmp_path,
        podman_sock="",
        podman_bin=podman_bin,
        skip_deploy=skip,
        boot=boot,
        remote=False,
    )
    return Yard(config, Store.open(tmp_path))


class FakeCaddy(Caddy):
    """The admin socket, faked. `routes` is the route array GETs return."""

    def __init__(self, routes: list | None = None) -> None:
        self.routes = routes if routes is not None else []
        self.calls: list[tuple[str, str, bytes | None]] = []

    async def request(self, method: str, path: str, body: bytes | None = None) -> tuple[int, bytes]:
        self.calls.append((method, path, body))
        if method == "GET" and path == caddy.ROUTES_PATH:
            return 200, json.dumps(self.routes).encode()
        return 200, b"{}"
