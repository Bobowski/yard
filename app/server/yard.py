"""Yard's dependencies and runtime state. The services do the work."""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from app.server.caddy import Caddy
from app.server.forges import Forge, GitHub, Origin
from app.server.podman import Podman
from app.server.store import Config, Store
from app.shared import YardError

if TYPE_CHECKING:
    from stario import App as StarioApp


@dataclass(slots=True)
class Tip:
    """What a check last saw for one app."""

    revision: str = ""
    template: str = ""
    error: str = ""
    tried: tuple[str, str] = ("", "")


class Yard:
    """Config, store, the adapters, and what the running server remembers."""

    def __init__(self, config: Config, store: Store) -> None:
        self.config = config
        self.store = store
        self.podman = Podman(config.podman_bin, remote=config.remote)
        self.caddy = Caddy(config.caddy_socket)
        self.forges: dict[str, Forge] = {
            forge.host: forge
            for forge in (
                Origin(config.data("yard") / "origin"),
                GitHub(config.data("yard") / "github"),
            )
        }
        self.skip_deploy = config.skip_deploy
        self.tips: dict[str, Tip] = {}
        self.locks: dict[str, asyncio.Lock] = {}
        self.checked: dict[str, float] = {}
        self.queued: dict[str, Callable[[], Awaitable[None]]] = {}
        self.tasks: list[asyncio.Task[None]] = []
        self.stario: StarioApp | None = None
        self.last_prune = ""
        self.last_prune_freed = 0
        self.last_prune_error = ""

    def attach(self, app: StarioApp) -> None:
        self.stario = app

    def spawn(self, coro) -> asyncio.Task:
        """Background work from a request. The app tracks it when it can."""
        if self.stario is not None:
            return self.stario.create_task(coro)
        return asyncio.get_running_loop().create_task(coro)

    def lock(self, name: str) -> asyncio.Lock:
        """One lock per app: deploys serialize on it."""
        return self.locks.setdefault(name, asyncio.Lock())

    def forge(self, repo: str) -> Forge:
        """The forge that hosts a repo URL."""
        forge = self.forges.get(urlsplit(repo).hostname or "")
        if forge is None:
            raise YardError(
                400,
                "bad_repo",
                f"repo must be a forge URL: {repo}",
                help_text="Use https://<forge>/<owner>/<name>.",
                example="https://github.com/org/heldso",
            )
        return forge

    def forge_named(self, name: str) -> Forge:
        """The forge by its short name, for the /forges/{name} routes."""
        for forge in self.forges.values():
            if forge.name == name:
                return forge
        raise YardError(404, "not_found", f"unknown forge {name}", help_text="The forges are origin and github.")
