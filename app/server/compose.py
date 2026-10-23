"""Compose projects. Yard sets the env; the apply image runs docker-compose on the files as they are."""

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from app.server import git
from app.server.store import App
from app.shared import YardError, ensure_file, short_sha

if TYPE_CHECKING:
    from app.server.store import Config

APPLY_DIR = "/usr/local/share/yard/apply"
if not Path(APPLY_DIR).is_dir():
    APPLY_DIR = str(Path(__file__).resolve().parents[2] / "ops" / "apply")
DEFAULT_COMPOSE = "/usr/local/share/yard/default.compose.yml"
IMAGE_COMPOSE = "/usr/local/share/yard/image.compose.yml"
COMPOSE_NAMES = ("compose.yml", "compose.yaml", "ops/compose.yml", "ops/compose.yaml")
DOCKERFILES = ("Containerfile", "Dockerfile", "ops/Containerfile", "ops/Dockerfile")


def image_tag(name: str, sha: str) -> str:
    return f"localhost/{name}:{short_sha(sha)}"


@dataclass(slots=True)
class Project:
    name: str
    files: list[str]
    env: dict[str, str]


def find_compose(checkout: Path) -> Path | None:
    return next((checkout / name for name in COMPOSE_NAMES if (checkout / name).is_file()), None)


def compose_files(app: App, checkout: Path) -> list[str]:
    """The compose file a deploy or purge runs. The record wins; unset falls back to the repo scan."""
    if app.compose:
        return [str(checkout / app.compose)]
    if app.containerfile:
        return [DEFAULT_COMPOSE]
    found = find_compose(checkout)
    return [str(found)] if found else [DEFAULT_COMPOSE]


def run_env(config: Config, app: App, checkout: Path, image: str, sha: str) -> dict[str, str]:
    """The values the compose files interpolate. The same names reach the apply container as process env."""
    data = config.data(app.name)
    data.mkdir(parents=True, exist_ok=True)
    env = {
        "PROJECT_NAME": app.name,
        "IMAGE": image,
        "SHA": sha,
        "CHECKOUT": str(checkout),
        "DATA_DIR": str(data),
        "APP_ENV": str(config.env(app.name)),
    }

    if app.compose:
        pass  # The repo's compose file names its own build.
    elif app.containerfile == "yard":
        # Absolute, so apply.sh uses the stored file instead of a repo path.
        env["DOCKERFILE"] = str(config.app_dir(app.name) / "Containerfile")
    elif app.containerfile:
        env["DOCKERFILE"] = app.containerfile
    if app.name == "yard":
        # ops/compose.yml binds these host paths.
        sock = config.podman_sock or "/run/podman/podman.sock"
        env["YARD_ROOT"] = str(config.root)
        env["YARD_PODMAN_SOCK"] = sock
        env["YARD_PODMAN_DIR"] = str(Path(sock).parent)
    return env


def ensure_env(path: Path) -> None:
    """The app env file, empty when new. Compose refuses a missing env_file."""
    ensure_file(path)


def app_container(name: str) -> str:
    """Compose names the one app container `<project>-app-1`."""
    return f"{name}-app-1"


def prepare(config: Config, app: App, checkout: Path, image: str, sha: str) -> Project:
    """The project a deploy runs. The apply container checks the compose file."""
    env = run_env(config, app, checkout, image, sha)
    ensure_env(config.env(app.name))
    files = compose_files(app, checkout)
    return Project(app.name, files, env)


def saved_project(config: Config, app: App) -> Project | None:
    """Rebuild the project from the app clone. The checkout SHA is the image tag."""
    checkout = config.clone(app.name)
    if not (checkout / ".git").exists():
        return None
    sha = git.head(checkout)
    if not sha:
        return None
    image = image_tag(app.name, sha)
    files = compose_files(app, checkout)
    env = run_env(config, app, checkout, image, sha)
    ensure_env(config.env(app.name))
    return Project(app.name, files, env)


async def apply(
    config: Config,
    app: str,
    command: str,
    project: Project,
    *,
    detach: bool = False,
) -> tuple[int, str]:
    """Run the apply script as one Podman container. A non-zero exit is the deploy log, not an exception."""
    sock = config.podman_sock or "/run/podman/podman.sock"
    # The name is fixed so `podman logs deploy-<name>` is the whole deploy story.
    # --replace replaces the previous container and its log.
    name = f"deploy-{app}"
    argv = _podman(config) + [
        "run",
        "--replace",
        "--name",
        name,
        "--network",
        "yard",
        "-v",
        f"{sock}:/run/podman/podman.sock",
        "-v",
        f"{config.root}:{config.root}",
        "-e",
        f"COMPOSE_PROJECT_NAME={app}",
        "-e",
        f"COMPOSE_FILE={':'.join(project.files)}",
    ]
    for key in sorted(project.env):
        argv += ["-e", f"{key}={project.env[key]}"]
    if detach:
        argv.append("-d")
    argv += ["localhost/yard-apply", command]
    return await _run(argv, _process_env(config))


async def build_apply(config: Config) -> tuple[int, str]:
    """Rebuild the apply image. The layer cache makes a no-change build fast, and each yard upgrade is a new process."""
    argv = _podman(config) + [
        "build",
        "-t",
        "localhost/yard-apply",
        "-f",
        f"{APPLY_DIR}/Containerfile",
        APPLY_DIR,
    ]
    return await _run(argv, _process_env(config))


def _podman(config: Config) -> list[str]:
    args = [config.podman_bin or "podman"]
    if config.remote:
        args.append("--remote")
    return args


def _process_env(config: Config) -> dict[str, str]:
    """The env the host-side podman needs to find the socket."""
    sock = config.podman_sock or "/run/podman/podman.sock"
    env = dict(os.environ)
    host = env.get("CONTAINER_HOST") or f"unix://{sock}"
    env["CONTAINER_HOST"] = host
    env["DOCKER_HOST"] = env.get("DOCKER_HOST") or host
    return env


async def _run(argv: list[str], env: dict[str, str]) -> tuple[int, str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            env=env or None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except FileNotFoundError as exc:
        raise YardError(
            502,
            "compose",
            f"{argv[0]} is not on PATH: {exc}",
            help_text="The yard image ships docker-compose and podman.",
        ) from exc
    out, _ = await proc.communicate()
    return proc.returncode or 0, (out or b"").decode(errors="replace")
