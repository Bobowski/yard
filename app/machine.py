"""Build an image, start the slot, and tell Caddy where it listens."""

import asyncio
import json
import os
import shutil
import socket
import stat
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from app.apply import materialize, run_args, write_env
from app.errors import YardError
from app.podman import Podman
from app.store import Slot
from app.text import image_sha, parse_env, process_defaults

PROXY_PORT = 8443
APP_PORT = 8000
JOB_RETRY = 30.0


class Paths:
    def __init__(self, root: Path) -> None:
        self.root = root

    def repo(self, name: str) -> Path:
        return self.root / "repos" / f"{name}.git"

    def data(self, slot: str) -> Path:
        return self.root / "data" / slot

    def run_dir(self, slot: str) -> Path:
        return self.root / "run" / slot

    def build(self, repo: str, directory: str, sha: str) -> Path:
        name = sha if not directory else f"{sha}-{slug(directory)}"
        return self.root / "build" / repo / name


def slug(directory: str) -> str:
    return directory.lower().replace("/", "-").replace("_", "-").replace(".", "-")


def image_tag(repo: str, directory: str, sha: str) -> str:
    if not directory:
        return f"localhost/{repo}:{sha}"
    return f"localhost/{repo}-{slug(directory)}:{sha}"


@dataclass(slots=True)
class Result:
    image: str
    log: str
    error: str = ""
    pending: bool = False


@dataclass(slots=True)
class RunState:
    id: str
    running: bool
    started: datetime | None = None
    finished: datetime | None = None


@dataclass(slots=True)
class JobPlan:
    start: bool = False
    wait_id: str = ""
    sleep: float = 0
    warn: str = ""


def job_due(runs: list[RunState], every: float, now: datetime) -> bool:
    if every <= 0:
        return False
    if any(run.running for run in runs):
        return False
    latest = max((run.finished for run in runs if run.finished), default=None)
    if latest is None:
        return True
    return now >= latest + timedelta(seconds=every)


def plan_job(every: float, image: str, runs: list[RunState], now: datetime) -> JobPlan:
    if every <= 0:
        return JobPlan()
    if not image:
        return JobPlan(sleep=JOB_RETRY, warn="no image yet")
    running = [run for run in runs if run.running]
    if running:
        plan = JobPlan(wait_id=running[0].id)
        if len(running) > 1:
            plan.warn = "more than one run is still up"
        elif running[0].started and now >= running[0].started + timedelta(seconds=every):
            plan.warn = "run is still up past the interval"
        return plan
    if job_due(runs, every, now):
        return JobPlan(start=True)
    latest = max(run.finished for run in runs if run.finished)
    return JobPlan(sleep=max(0.0, (latest + timedelta(seconds=every) - now).total_seconds()))


async def prepare_checkout(paths: Paths, repo: str, directory: str, sha: str) -> Path:
    """Extract one revision. A finished extract stays for the next deploy of that revision."""
    build_dir = paths.build(repo, directory, sha)
    ready = build_dir.parent / f".{build_dir.name}.ready"
    if ready.is_file() and build_dir.is_dir():
        return build_dir
    shutil.rmtree(build_dir, ignore_errors=True)
    ready.unlink(missing_ok=True)
    build_dir.mkdir(parents=True)
    source = paths.repo(repo)
    sub = directory.strip("/")
    cmd = ["git", "-C", str(source), "archive", "--format=tar", sha]
    strip = 0
    if sub:
        cmd.append(sub)
        strip = len([part for part in sub.split("/") if part])
    tar_cmd = ["tar", "-x", "-C", str(build_dir)]
    if strip:
        tar_cmd.append(f"--strip-components={strip}")
    read_fd, write_fd = os.pipe()
    try:
        git = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=write_fd,
            stderr=asyncio.subprocess.PIPE,
        )
    finally:
        os.close(write_fd)
    try:
        tar = await asyncio.create_subprocess_exec(
            *tar_cmd,
            stdin=read_fd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
    except Exception:
        git.kill()
        await git.wait()
        raise
    finally:
        os.close(read_fd)
    assert git.stderr is not None and tar.stderr is not None
    git_raw, tar_raw, git_code, tar_code = await asyncio.gather(
        git.stderr.read(),
        tar.stderr.read(),
        git.wait(),
        tar.wait(),
    )
    git_err = git_raw.decode(errors="replace")
    tar_err = tar_raw.decode(errors="replace")
    if git_code:
        detail = git_err.strip()
        low = detail.lower()
        if sub and ("pathspec" in low or "did not match" in low):
            raise RuntimeError(f"directory {directory!r} is not in the repo at this revision")
        raise RuntimeError(detail or "git archive failed")
    if tar_code:
        raise RuntimeError(tar_err.strip() or "tar failed")
    ready.write_text(sha + "\n")
    return build_dir


# The yard container reads these. A later deploy must not drop them.
_HOST_KEYS = (
    "YARD_PODMAN_SOCK",
    "YARD_PUBLIC_PORT",
    "YARD_CADDY_SOCK",
    "YARD_BOOT",
    "YARD_DOMAIN",
    "CONTAINER_HOST",
    "PROXY_PORT",
)


def _kept_host(env_path: Path) -> dict[str, str]:
    found: dict[str, str] = {}
    if env_path.is_file():
        try:
            current = parse_env(env_path.read_text())
        except YardError:
            current = {}
        for key in _HOST_KEYS:
            if current.get(key):
                found[key] = current[key]
    for key in _HOST_KEYS:
        if key not in found and os.environ.get(key):
            found[key] = os.environ[key]
    return found


def _apply_env(paths: Paths, slot: Slot, checkout: Path, image: str, sha: str) -> tuple[Path, Path]:
    data = paths.data(slot.name)
    data.mkdir(parents=True, exist_ok=True)
    run = paths.run_dir(slot.name)
    run.mkdir(parents=True, exist_ok=True)
    env_path = run / "compose.env"
    app_env = run / "app.env"
    kept = _kept_host(env_path) if slot.name == "yard" else {}
    values = process_defaults(slot.name)
    values.update(
        {
            "IMAGE": image,
            "PREVIOUS": slot.live_image,
            "CHECKOUT": str(checkout),
            "PORT": str(APP_PORT),
            "RESTART": "no" if slot.every else "always",
        }
    )
    values.update(slot.env)
    values["SHA"] = sha
    # The container path is SLOT_DATA_DIR. This host directory is that path.
    values["DATA_DIR"] = str(data)
    values["YARD_ROOT"] = str(paths.root)
    compose = materialize(checkout, env_path, app_env, values)
    with env_path.open("a") as handle:
        for key, value in kept.items():
            handle.write(f"{key}={value}\n")
        handle.write(f"COMPOSE_FILE={compose}\n")
    return compose, env_path


def write_image(env_path: Path, image: str) -> None:
    """Point the compose env at one image. The next apply command uses that image."""
    values = parse_env(env_path.read_text()) if env_path.is_file() else {}
    values["IMAGE"] = image
    values["SHA"] = image_sha(image)
    write_env(env_path, values)


async def _revert_after_start(
    podman: Podman,
    paths: Paths,
    slot: Slot,
    image: str,
    compose: Path,
    env_path: Path,
    sock: str,
) -> list[str]:
    if not slot.live_image or slot.live_image == image:
        return []
    try:
        write_image(env_path, slot.live_image)
    except Exception as exc:
        return [str(exc)]
    built = paths.run_dir(slot.name) / "apply.built"
    if not built.is_file():
        return [f"kept {slot.live_image}"]
    lines = [f"rollback {slot.live_image}"]
    try:
        args, env = run_args(
            root=str(paths.root),
            sock=sock,
            project=slot.name,
            compose=str(compose),
            env_file=str(env_path),
            command="revert",
            slot=slot.name,
            sha=image_sha(slot.live_image),
        )
        lines.append(await podman.run(*args, env=env, program="docker-compose", timeout=None))
    except Exception as roll:
        lines.append(str(roll))
    return lines


async def deploy_slot(podman: Podman, paths: Paths, slot: Slot, sha: str, sock: str) -> Result:
    sha = sha.strip()
    image = image_tag(slot.repo, slot.directory, sha)
    lines: list[str] = []
    try:
        checkout = await prepare_checkout(paths, slot.repo, slot.directory, sha)
        compose, env_path = _apply_env(paths, slot, checkout, image, sha)
    except Exception as exc:
        lines.append(str(exc))
        return Result(image, "\n".join(lines) + "\n", str(exc))

    # A push builds the image. A web slot also starts. A job waits for the clock.
    # Upgrading yard replaces this container, so that apply container is detached.
    command = "build" if slot.every else "up"
    detach = slot.name == "yard" and command == "up"
    try:
        args, env = run_args(
            root=str(paths.root),
            sock=sock,
            project=slot.name,
            compose=str(compose),
            env_file=str(env_path),
            command=command,
            slot=slot.name,
            sha=sha,
            detach=detach,
        )
        lines.append(await podman.run(*args, env=env, program="docker-compose", timeout=None))
    except YardError as exc:
        lines.append(str(exc))
        if command == "up" and not detach:
            lines.extend(await _revert_after_start(podman, paths, slot, image, compose, env_path, sock))
        return Result(image, "\n".join(lines) + "\n", str(exc))
    if detach:
        lines.append("upgrading")
        return Result(image, "\n".join(lines) + "\n", pending=True)

    if slot.every:
        lines.append(f"job every {slot.every} image {image}")
        return Result(image, "\n".join(lines) + "\n")

    health = f"http://{slot.name}:{APP_PORT}/"
    lines.append(f"wait {health}")
    try:
        await wait_url(health, 30)
    except Exception as exc:
        lines.append(str(exc))
        lines.extend(await _revert_after_start(podman, paths, slot, image, compose, env_path, sock))
        return Result(image, "\n".join(lines) + "\n", str(exc))
    lines.append(f"ready {image}")
    return Result(image, "\n".join(lines) + "\n")


async def run_job(podman: Podman, paths: Paths, slot: Slot, image: str, sock: str) -> None:
    env_path = paths.run_dir(slot.name) / "compose.env"
    if not env_path.is_file():
        raise RuntimeError(f"slot {slot.name} has no compose env")
    values = parse_env(env_path.read_text())
    compose = values.get("COMPOSE_FILE") or ""
    if not compose:
        raise RuntimeError(f"slot {slot.name} has no compose file")
    args, env = run_args(
        root=str(paths.root),
        sock=sock,
        project=slot.name,
        compose=str(compose),
        env_file=str(env_path),
        command="job",
        slot=slot.name,
        sha=image_sha(image),
    )
    await podman.run(*args, env=env, program="docker-compose", timeout=None)


async def wait_url(url: str, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    last = "no response"
    while time.monotonic() < deadline:
        try:
            await asyncio.to_thread(_probe, url)
            return
        except Exception as exc:
            last = str(exc)
        await asyncio.sleep(0.3)
    raise RuntimeError(f"{url} not ready: {last}")


def _probe(url: str) -> None:
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            response.read(64)
    except urllib.error.HTTPError:
        return


@dataclass(slots=True)
class Route:
    host: str
    dial: str


def routes_from(slots: list[Slot]) -> list[Route]:
    return [Route(slot.domain, f"{slot.name}:{APP_PORT}") for slot in slots if slot.domain and not slot.every]


def yard_route(domain: str) -> Route | None:
    if not domain:
        return None
    return Route(domain, f"yard:{APP_PORT}")


def caddy_document(sock: Path, routes: list[Route]) -> bytes:
    http_routes = [
        {
            "handle": [
                {
                    "handler": "reverse_proxy",
                    "lb_try_duration": "10s",
                    "lb_try_interval": "250ms",
                    "upstreams": [{"dial": route.dial}],
                }
            ],
            "match": [{"host": [route.host]}],
        }
        for route in routes
    ]
    issuer: dict[str, object] = {"challenges": {"http": {"disabled": True}}, "module": "acme"}
    document = {
        "admin": {"listen": f"unix/{sock}|0600"},
        "apps": {
            "http": {
                "https_port": PROXY_PORT,
                "servers": {
                    "yard": {
                        "automatic_https": {"disable_redirects": True},
                        "listen": [f":{PROXY_PORT}"],
                        "routes": http_routes,
                    }
                },
            },
            "tls": {"automation": {"policies": [{"issuers": [issuer]}]}},
        },
    }
    return json.dumps(document, indent="\t", sort_keys=True).encode() + b"\n"


async def ensure_caddy(root: Path, sock: Path) -> None:
    """Prepare Caddy's files. The compose project starts the container."""
    data = root / "data" / "caddy"
    data.mkdir(parents=True, exist_ok=True)
    (data / "lib").mkdir(parents=True, exist_ok=True)
    sock.parent.mkdir(parents=True, exist_ok=True)
    auto = data / "autosave.json"
    if not auto.is_file() or auto.stat().st_size == 0:
        auto.write_bytes(caddy_document(sock, []))
        auto.chmod(0o600)
    await _wait_sock(sock, 15)


def _post_unix(sock: Path, body: bytes) -> None:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(10)
        client.connect(str(sock))
        header = (
            "POST /load HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n"
        ).encode()
        client.sendall(header + body)
        data = bytearray()
        while chunk := client.recv(65536):
            data += chunk
    status = int(bytes(data).split(b" ", 2)[1])
    if status >= 300:
        raise RuntimeError(f"caddy load: {status}")


async def _wait_sock(path: Path, limit: float) -> None:
    deadline = time.monotonic() + limit
    while True:
        if path.exists() and stat.S_ISSOCK(path.stat().st_mode):
            return
        if time.monotonic() > deadline:
            raise RuntimeError(f"caddy socket {path} is not ready")
        await asyncio.sleep(0.1)


async def run_cmd(*args: str, timeout: float | None = None, env: dict[str, str] | None = None) -> str:
    proc = await asyncio.create_subprocess_exec(
        *args,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        if timeout is None:
            out, _ = await proc.communicate()
        else:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError as exc:
        proc.kill()
        raise RuntimeError(f"{args[0]} timed out") from exc
    text = (out or b"").decode(errors="replace").strip()
    if proc.returncode:
        raise RuntimeError(text or f"{' '.join(args)} failed")
    return text
