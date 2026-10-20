"""Service-level helpers: a Yard on a temp root, and a fake Caddy admin."""

import json
from pathlib import Path
from urllib.parse import unquote

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
        journal_dir=tmp_path / "no-journal",
        journalctl_bin="journalctl-missing",
        journal_socket=tmp_path / "no-journal.sock",
    )
    return Yard(config, Store.open(tmp_path))


class FakeCaddy(Caddy):
    """The admin socket, faked. Writes mutate `routes` like Caddy does:

    POST on the array appends, PUT on `routes/<i>` inserts, PATCH replaces an
    element or an `/id/<id>` object, DELETE removes. Wrong-method writes fail.
    """

    def __init__(self, routes: list | None = None) -> None:
        self.routes = routes if routes is not None else []
        self.calls: list[tuple[str, str, bytes | None]] = []

    async def request(self, method: str, path: str, body: bytes | None = None) -> tuple[int, bytes]:
        self.calls.append((method, path, body))
        if method == "GET" and path == caddy.ROUTES_PATH:
            return 200, json.dumps(self.routes).encode()
        if method == "POST" and path == caddy.ROUTES_PATH:
            self.routes.append(json.loads(body or b"{}"))
            return 200, b"{}"
        if path.startswith(f"{caddy.ROUTES_PATH}/"):
            return self._at_index(method, int(path.rsplit("/", 1)[1]), body)
        if path.startswith("/id/"):
            return self._at_id(method, unquote(path[4:]), body)
        return 200, b"{}"

    def _at_index(self, method: str, index: int, body: bytes | None) -> tuple[int, bytes]:
        if method == "PUT":
            self.routes.insert(index, json.loads(body or b"{}"))
        elif method == "PATCH":
            if index >= len(self.routes):
                return 404, b"{}"
            self.routes[index] = json.loads(body or b"{}")
        elif method == "DELETE":
            if index >= len(self.routes):
                return 404, b"{}"
            del self.routes[index]
        else:
            return 405, b"{}"
        return 200, b"{}"

    def _at_id(self, method: str, ident: str, body: bytes | None) -> tuple[int, bytes]:
        pos = next((i for i, row in enumerate(self.routes) if row.get("@id") == ident), None)
        if method == "PUT":
            # PUT under /id strictly creates; an existing id is a conflict.
            if pos is not None:
                return 409, b"{}"
            self.routes.append(json.loads(body or b"{}"))
            return 200, b"{}"
        if pos is None:
            return 404, b"{}"
        if method == "PATCH":
            self.routes[pos] = json.loads(body or b"{}")
        elif method == "DELETE":
            del self.routes[pos]
        else:
            return 405, b"{}"
        return 200, b"{}"
