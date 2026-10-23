"""Process settings and Yard's files. Config is the environment; Store is the records on disk."""

from __future__ import annotations

import hashlib
import os
import secrets
from dataclasses import dataclass
from pathlib import Path

from app.shared import (
    YardError,
    atomic_write,
    check_app_name,
    check_compose,
    check_containerfile,
    check_env,
    check_image,
    check_name,
    check_recipe,
    check_ref,
    check_repo,
    check_trigger,
    ensure_file,
    format_env,
    now,
    parse_env,
)


@dataclass(frozen=True, slots=True)
class Config:
    root: Path
    podman_sock: str
    podman_bin: str
    skip_deploy: bool
    boot: bool
    remote: bool
    check_seconds: float = 300.0
    prune_days: int = 7
    sha: str = ""
    journal_dir: Path = Path("/var/log/journal")
    journalctl_bin: str = "journalctl"
    journal_socket: Path = Path("/run/systemd/journal/socket")

    @property
    def token_file(self) -> Path:
        return self.root / "tokens" / "admin.token"

    def app_dir(self, name: str) -> Path:
        return self.root / "apps" / name

    def data(self, name: str) -> Path:
        return self.app_dir(name) / "data"

    def env(self, name: str) -> Path:
        return self.app_dir(name) / "env"

    def clone(self, name: str) -> Path:
        """The plain git clone for the app. A deploy resets it to the ref tip."""
        return self.app_dir(name) / "repo"

    @property
    def caddy_socket(self) -> Path:
        """The Caddy admin unix socket. The caddy container writes it to run/."""
        return self.app_dir("yard") / "caddy" / "run" / "admin.sock"

    def caddy_data(self) -> Path:
        return self.app_dir("yard") / "caddy" / "data"

    @classmethod
    def from_env(cls) -> Config:
        root = Path(os.environ.get("YARD_ROOT", "yard-data")).resolve()
        boot = os.environ.get("YARD_BOOT", "1") != "0"
        podman_sock = os.environ.get("YARD_PODMAN_SOCK", "")
        remote = bool(podman_sock or os.environ.get("CONTAINER_HOST"))
        return cls(
            root=root,
            podman_sock=podman_sock,
            podman_bin=os.environ.get("YARD_PODMAN_BIN", "podman"),
            skip_deploy=os.environ.get("YARD_SKIP_DEPLOY", "") == "1",
            boot=boot,
            remote=remote,
            check_seconds=float(os.environ.get("YARD_CHECK_SECONDS", "300")),
            prune_days=int(os.environ.get("YARD_PRUNE_DAYS", "7")),
            sha=os.environ.get("SHA", ""),
            journal_dir=Path(os.environ.get("YARD_JOURNAL_DIR", "/var/log/journal")),
            journalctl_bin=os.environ.get("YARD_JOURNALCTL_BIN", "journalctl"),
            journal_socket=Path(os.environ.get("YARD_JOURNAL_SOCKET", "/run/systemd/journal/socket")),
        )


def token_hash(plain: str) -> str:
    return hashlib.sha256(plain.encode()).hexdigest()


@dataclass(slots=True)
class App:
    name: str
    repo: str
    ref: str
    trigger: str
    created: str
    # What the app runs. At most one is set; neither means the deploy decides.
    containerfile: str = ""
    compose: str = ""
    # Repo-less apps run a pulled image or a fetched recipe instead.
    image: str = ""
    recipe: str = ""


@dataclass(slots=True)
class Token:
    name: str
    created: str


class Store:
    """Apps and tokens on disk. Each call reads the files."""

    def __init__(self, root: Path) -> None:
        self.root = root

    @classmethod
    def open(cls, root: Path) -> Store:
        (root / "apps").mkdir(parents=True, exist_ok=True)
        tokens = root / "tokens"
        tokens.mkdir(mode=0o700, parents=True, exist_ok=True)
        return cls(root)

    def _record(self, name: str) -> Path:
        return self.root / "apps" / name / "app"

    def list_apps(self) -> list[App]:
        base = self.root / "apps"
        if not base.is_dir():
            return []
        found: list[App] = []
        for path in base.iterdir():
            file = path / "app"
            if path.is_dir() and file.is_file():
                found.append(_read_app(path.name, file.read_text()))
        return sorted(found, key=lambda app: app.name)

    def app(self, name: str) -> App:
        file = self._record(name)
        if not file.is_file():
            raise YardError(
                404,
                "not_found",
                "app does not exist",
                context={"name": name},
                help_text="Run yard app ls to see the apps on this host.",
            )
        return _read_app(name, file.read_text())

    def create_app(
        self,
        name: str,
        repo: str,
        ref: str = "",
        trigger: str = "",
        containerfile: str = "",
        compose: str = "",
        image: str = "",
        recipe: str = "",
    ) -> App:
        check_app_name(name)
        if repo:
            repo = check_repo(repo)
        ref = ref or "main"
        check_ref(ref)
        if self._record(name).is_file():
            raise YardError(409, "exists", "already exists", help_text="Pick another name.")
        app = App(name, repo, ref, check_trigger(trigger), now())
        self._build_fields(app, containerfile, compose, image, recipe)
        self._write(app)
        return app

    def update_app(
        self,
        name: str,
        *,
        repo: str | None = None,
        ref: str | None = None,
        trigger: str | None = None,
        containerfile: str | None = None,
        compose: str | None = None,
        image: str | None = None,
        recipe: str | None = None,
    ) -> App:
        app = self.app(name)
        if repo is not None:
            app.repo = check_repo(repo)
        if ref is not None:
            ref = ref or "main"
            check_ref(ref)
            app.ref = ref
        if trigger is not None:
            app.trigger = check_trigger(trigger)
        fields = {"containerfile": containerfile, "compose": compose, "image": image, "recipe": recipe}
        given = {key: value for key, value in fields.items() if value is not None}
        if given:
            wanted = {key: value for key, value in given.items() if value}
            if len(wanted) > 1:
                names = sorted(wanted)
                raise YardError(
                    400,
                    "bad_request",
                    f"set --{names[0]} or --{names[1]}, not both",
                    context={key: wanted[key] for key in names},
                )
            if wanted:
                # One non-empty field clears the other three.
                self._build_fields(
                    app,
                    wanted.get("containerfile", ""),
                    wanted.get("compose", ""),
                    wanted.get("image", ""),
                    wanted.get("recipe", ""),
                )
            else:
                # "" clears only its own field.
                for key in given:
                    setattr(app, key, "")
        self._write(app)
        return self.app(name)

    def delete_app(self, name: str) -> None:
        file = self._record(name)
        if not file.is_file():
            raise YardError(
                404,
                "not_found",
                "app does not exist",
                context={"name": name},
                help_text="Run yard app ls to see the apps on this host.",
            )
        file.unlink()

    def create_token(self, name: str) -> str:
        check_name(name)
        path = self.root / "tokens" / name
        if path.exists():
            raise YardError(409, "exists", "already exists", help_text="Pick another name.")
        plain = secrets.token_hex(24)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        atomic_write(path, f"{now()}\n{token_hash(plain)}\n")
        return plain

    def read_env(self, name: str) -> dict[str, str]:
        path = self._env(name)
        if not path.is_file():
            return {}
        return parse_env(path.read_text())

    def write_env(self, name: str, env: dict[str, str]) -> list[str]:
        check_env(env)
        self._write_env(name, env)
        return sorted(env)

    def patch_env(self, name: str, patch: dict[str, str | None]) -> list[str]:
        check_env(patch)
        env = self.read_env(name)
        for key, value in patch.items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = value
        self._write_env(name, env)
        return sorted(env)

    def list_tokens(self) -> list[Token]:
        base = self.root / "tokens"
        if not base.is_dir():
            return []
        found: list[Token] = []
        for path in sorted(base.iterdir(), key=lambda item: item.name):
            token = self._token(path)
            if token is not None:
                found.append(token)
        return found

    def delete_token(self, name: str, actor: str = "") -> None:
        if name == actor:
            raise YardError(
                403,
                "forbidden",
                "you cannot delete your own token",
                context={"name": name},
                help_text="Ask another token to remove it.",
            )
        path = self.root / "tokens" / name
        if not path.is_file():
            raise YardError(
                404,
                "not_found",
                "unknown token",
                context={"name": name},
                help_text="Run yard token ls.",
            )
        path.unlink()

    def token_ok(self, plain: str) -> Token | None:
        if not plain:
            return None
        digest = token_hash(plain)
        base = self.root / "tokens"
        if not base.is_dir():
            return None
        for path in base.iterdir():
            token = self._token(path)
            if token is None:
                continue
            lines = path.read_text().splitlines()
            if len(lines) >= 2 and lines[1] == digest:
                return token
        return None

    def _build_fields(self, app: App, containerfile: str, compose: str, image: str, recipe: str) -> None:
        """What the app runs: one of the four. A repo app builds; a repo-less app pulls."""
        if image and app.repo:
            raise YardError(
                400,
                "bad_request",
                "an image app has no repo",
                context={"image": image},
                help_text="Create a new app: yard app new --app NAME --image REF",
            )
        if recipe and app.repo:
            raise YardError(
                400,
                "bad_request",
                "a recipe app has no repo",
                context={"recipe": recipe},
                help_text="Create a new app: yard app new --app NAME --recipe NAME",
            )
        if (containerfile or compose) and not app.repo:
            raise YardError(
                400,
                "bad_request",
                f"app {app.name} has no repo",
                context={"name": app.name},
                help_text="Set the repo first: yard app set --repo https://<host>/<owner>/<name>",
            )
        app.containerfile = check_containerfile(containerfile) if containerfile else ""
        app.compose = check_compose(compose) if compose else ""
        app.image = check_image(image) if image else ""
        app.recipe = check_recipe(recipe) if recipe else ""

    def _write(self, app: App) -> None:
        text = f"repo {app.repo}\nref {app.ref}\ntrigger {app.trigger}\ncreated {app.created}\n"
        for field in ("containerfile", "compose", "image", "recipe"):
            if getattr(app, field):
                text += f"{field} {getattr(app, field)}\n"
        atomic_write(self._record(app.name), text)

    def _env(self, name: str) -> Path:
        return self.root / "apps" / name / "env"

    def _write_env(self, name: str, env: dict[str, str]) -> None:
        path = self._env(name)
        ensure_file(path)
        atomic_write(path, format_env(env))

    def _token(self, path: Path) -> Token | None:
        if not path.is_file() or path.suffix == ".token" or not _name_ok(path.name):
            return None
        lines = path.read_text().splitlines()
        if len(lines) < 2:
            return None
        return Token(path.name, lines[0])


def _name_ok(name: str) -> bool:
    try:
        check_name(name)
    except YardError:
        return False
    return True


def _read_app(name: str, text: str) -> App:
    fields = {
        "repo": "",
        "ref": "main",
        "trigger": "push",
        "created": "",
        "containerfile": "",
        "compose": "",
        "image": "",
        "recipe": "",
    }
    for line in text.splitlines():
        key, _, value = line.partition(" ")
        if key in fields:
            fields[key] = value.strip()
    return App(
        name,
        fields["repo"],
        fields["ref"] or "main",
        fields["trigger"] or "push",
        fields["created"],
        fields["containerfile"],
        fields["compose"],
        fields["image"],
        fields["recipe"],
    )


def ensure_admin(store: Store) -> str:
    if any(token.name == "admin" for token in store.list_tokens()):
        return ""
    plain = store.create_token("admin")
    atomic_write(store.root / "tokens" / "admin.token", plain + "\n")
    return plain
