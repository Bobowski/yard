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

    if find_compose(checkout) is None:
        dockerfile = next((name for name in DOCKERFILES if (checkout / name).is_file()), "")
        if not dockerfile:
            raise YardError(502, "compose", f"no compose file or Containerfile in {checkout}")
        env["DOCKERFILE"] = dockerfile
    if app.name == "yard":
        # ops/compose.yml binds these two host paths.
        env["YARD_ROOT"] = str(config.root)
        env["YARD_PODMAN_SOCK"] = config.podman_sock or "/run/podman/podman.sock"
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
    compose_file = find_compose(checkout)
    if compose_file is None:
        return Project(app.name, [DEFAULT_COMPOSE], env)
    return Project(app.name, [str(compose_file)], env)


def saved_project(config: Config, app: App) -> Project | None:
    """Rebuild the project from the app clone. The checkout SHA is the image tag."""
    checkout = config.clone(app.name)
    if not (checkout / ".git").exists():
        return None
    sha = git.head(checkout)
    if not sha:
        return None
    image = image_tag(app.name, sha)
    compose_file = find_compose(checkout)
    files = [str(compose_file) if compose_file else DEFAULT_COMPOSE]
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
