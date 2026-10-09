"""One yard-apply command builds and starts a project."""

import os
from pathlib import Path

from app.errors import YardError
from app.text import parse_env

# This file is in the apply image. Env values fill it. Yard does not write a copy per deploy.
DEFAULT_COMPOSE = "/usr/local/share/yard/default.compose.yml"
_APPLY_PACKED = Path("/usr/local/share/yard/apply/apply.compose.yml")

_COMPOSE_NAMES = ("compose.yml", "compose.yaml", "ops/compose.yml", "ops/compose.yaml")
_DOCKERFILES = ("Containerfile", "Dockerfile", "ops/Containerfile", "ops/Dockerfile")


def write_env(path: Path, values: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{key}={value}\n" for key, value in values.items()))


def materialize(checkout: Path, env_path: Path, app_env: Path, values: dict[str, str]) -> Path:
    """Return the compose file. A Containerfile repo uses the default file in the apply image."""
    compose = next((checkout / name for name in _COMPOSE_NAMES if (checkout / name).is_file()), None)
    if compose is None:
        dockerfile = next((name for name in _DOCKERFILES if (checkout / name).is_file()), "")
        if not dockerfile:
            raise RuntimeError(f"no compose file or Containerfile in {checkout}")
        compose = Path(DEFAULT_COMPOSE)
        values["DOCKERFILE"] = dockerfile
    values["COMPOSE_ENV_FILE"] = str(env_path)
    values["APP_ENV"] = str(app_env)
    hidden = {"CHECKOUT", "COMPOSE_ENV_FILE", "PREVIOUS", "DATA_DIR", "YARD_ROOT"}
    write_env(app_env, {key: value for key, value in values.items() if key not in hidden})
    write_env(env_path, values)
    return compose


# These stay after they exit. Podman names them. job and purge remove this container.
_KEEP = {"up", "build", "start", "revert"}
_ROLE = {"up": "deploy", "build": "deploy", "start": "deploy", "revert": "revert"}


def apply_compose() -> Path:
    if _APPLY_PACKED.is_file():
        return _APPLY_PACKED
    return Path(__file__).resolve().parents[1] / "ops" / "apply" / "apply.compose.yml"


def _docker_host(sock: str) -> str:
    current = os.environ.get("DOCKER_HOST") or os.environ.get("CONTAINER_HOST")
    if current:
        return current
    path = sock or "/run/podman/podman.sock"
    return f"unix://{path}" if path.startswith("/") else path


def run_args(
    *,
    root: str,
    sock: str,
    project: str,
    compose: str,
    env_file: str,
    command: str,
    slot: str = "",
    sha: str = "",
    detach: bool = False,
) -> tuple[list[str], dict[str, str]]:
    """Start the apply service from apply.compose.yml. The env fills that file."""
    data_dir = ""
    image = ""
    recorded = Path(env_file)
    if recorded.is_file():
        try:
            values = parse_env(recorded.read_text())
        except YardError:
            values = {}
        data_dir = values.get("DATA_DIR", "")
        image = values.get("IMAGE", "")
    args = ["-f", str(apply_compose()), "run"]
    if detach:
        args.append("-d")
    args.extend(["-T", "--no-deps"])
    args.append("--rm=false" if command in _KEEP else "--rm")
    args.extend(["apply", command])
    env = {
        "DOCKER_HOST": _docker_host(sock),
        "YARD_PODMAN_SOCK": sock or "/run/podman/podman.sock",
        "YARD_ROOT": root,
        "YARD_PROJECT": project,
        "YARD_COMPOSE": compose,
        "YARD_APP_ENV": env_file,
        "YARD_SLOT": slot,
        "YARD_SHA": sha,
        "YARD_ROLE": _ROLE.get(command, ""),
        "YARD_DEFAULT_COMPOSE": "1" if compose == DEFAULT_COMPOSE else "",
        "YARD_DATA_DIR": data_dir,
        "YARD_IMAGE": image,
    }
    return args, env
