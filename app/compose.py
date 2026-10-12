"""Compose projects. Yard sets the env; the apply image runs docker-compose on the files as they are."""

import asyncio
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.errors import YardError
from app.store import Slot
from app.values import image_sha, slug

if TYPE_CHECKING:
    from app.config import Config

APPLY_DIR = "/usr/local/share/yard/apply"
if not Path(APPLY_DIR).is_dir():
    APPLY_DIR = str(Path(__file__).resolve().parents[1] / "ops" / "apply")
DEFAULT_COMPOSE = "/usr/local/share/yard/default.compose.yml"
APP_PORT = 8000

COMPOSE_NAMES = ("compose.yml", "compose.yaml", "ops/compose.yml", "ops/compose.yaml")
DOCKERFILES = ("Containerfile", "Dockerfile", "ops/Containerfile", "ops/Dockerfile")


def image_tag(repo: str, directory: str, sha: str) -> str:
    if not directory:
        return f"localhost/{repo}:{sha}"
    return f"localhost/{repo}-{slug(directory)}:{sha}"


@dataclass(slots=True)
class Project:
    name: str
    files: list[str]
    env: dict[str, str]


def find_compose(checkout: Path) -> Path | None:
    return next((checkout / name for name in COMPOSE_NAMES if (checkout / name).is_file()), None)


def run_env(config: Config, slot: Slot, checkout: Path, image: str, sha: str) -> dict[str, str]:
    """The values the compose files interpolate. The same names reach the apply container as process env."""
    data = config.data(slot.name)
    data.mkdir(parents=True, exist_ok=True)
    env = {
        "SLOT_NAME": slot.name,
        "IMAGE": image,
        "SHA": sha,
        "CHECKOUT": str(checkout),
        "DATA_DIR": str(data),
        "APP_ENV": str(config.env(slot.name)),
    }

    if find_compose(checkout) is None:
        dockerfile = next((name for name in DOCKERFILES if (checkout / name).is_file()), "")
        if not dockerfile:
            raise RuntimeError(f"no compose file or Containerfile in {checkout}")
        env["DOCKERFILE"] = dockerfile
    if slot.name == "yard":
        # ops/compose.yml binds these two host paths.
        env["YARD_ROOT"] = str(config.root)
        env["YARD_PODMAN_SOCK"] = config.podman_sock or "/run/podman/podman.sock"
    return env


async def resolved(compose_file: Path, env: dict[str, str]) -> dict[str, Any]:
    """The compose document after interpolation. Runs the docker-compose on PATH."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker-compose",
            "-f",
            str(compose_file),
            "config",
            "--format",
            "json",
            env=os.environ | env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("docker-compose is not on PATH") from exc
    out, err = await proc.communicate()
    if proc.returncode:
        raise RuntimeError(err.decode(errors="replace").strip() or "docker-compose config failed")

    try:
        doc = json.loads(out or b"{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError("docker-compose config did not print json") from exc
    return doc if isinstance(doc, dict) else {}


def ensure_env(path: Path) -> None:
    """The slot env file, empty when new. Compose refuses a missing env_file."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.touch(mode=0o600, exist_ok=True)
    path.chmod(0o600)


async def prepare(config: Config, slot: Slot, checkout: Path, image: str, sha: str) -> tuple[Project, list[str]]:
    """The project a deploy runs, and the warnings a repo compose earned. Nothing is written but the env file."""
    env = run_env(config, slot, checkout, image, sha)
    ensure_env(config.env(slot.name))
    compose_file = find_compose(checkout)
    if compose_file is None:
        return Project(slot.name, [DEFAULT_COMPOSE], env), []

    doc = await resolved(compose_file, env)
    warnings = check(slot, doc, compose_file.read_text())
    return Project(slot.name, [str(compose_file)], env), warnings


def check(slot: Slot, doc: dict[str, Any], text: str) -> list[str]:
    """The contract for a repo compose. The alias is a hard rule; the env file and the data mount warn."""
    if not slot.job and not _has_alias(doc, slot.name):
        raise RuntimeError(
            f"compose: no service on network yard with alias {slot.name}; "
            f"the health probe dials http://{slot.name}:{APP_PORT}/"
        )

    warnings: list[str] = []
    if "${APP_ENV}" not in text:
        warnings.append("warning: compose: no service attaches env_file ${APP_ENV}; the slot env is not passed")
    if "${DATA_DIR}" not in text:
        warnings.append("warning: compose: no service mounts ${DATA_DIR}; the slot has no /data")
    return warnings


def _has_alias(doc: dict[str, Any], name: str) -> bool:
    services = doc.get("services")
    if not isinstance(services, dict):
        return False
    for spec in services.values():
        networks = spec.get("networks") if isinstance(spec, dict) else None
        yard = networks.get("yard") if isinstance(networks, dict) else None
        aliases = yard.get("aliases") if isinstance(yard, dict) else None
        if isinstance(aliases, list) and name in aliases:
            return True
    return False


def saved_project(config: Config, slot: Slot) -> Project | None:
    """Rebuild the project from the live image's checkout."""
    sha = image_sha(slot.live_image)
    if not sha:
        return None
    checkout = config.checkout(slot.repo, slot.directory, sha)
    if not checkout.is_dir():
        return None

    compose_file = find_compose(checkout)
    files = [str(compose_file) if compose_file else DEFAULT_COMPOSE]
    env = run_env(config, slot, checkout, slot.live_image, sha)
    return Project(slot.name, files, env)


async def apply(
    config: Config,
    slot: str,
    command: str,
    project: Project,
    *,
    previous: str = "",
    detach: bool = False,
) -> tuple[int, str]:
    """Run the apply script as one Podman container. A non-zero exit is the deploy log, not an exception."""
    sock = config.podman_sock or "/run/podman/podman.sock"
    # The name is fixed so `podman logs yard-apply-<slot>` is the whole deploy
    # story; --replace replaces the previous container and its log.
    name = f"yard-apply-{slot}-job" if command == "job" else f"yard-apply-{slot}"
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
        f"COMPOSE_PROJECT_NAME={slot}",
        "-e",
        f"COMPOSE_FILE={':'.join(project.files)}",
        "-e",
        f"PREVIOUS={previous}",
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


def built_stamp(log: str) -> str:
    """The `built <stamp>` line apply.sh prints after a successful build."""
    for line in log.splitlines():
        if line.startswith("built "):
            return line[6:].strip()
    return ""
