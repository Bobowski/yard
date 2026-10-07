"""Build an image, start the slot, and tell Caddy where it listens."""

import asyncio
import json
import shutil
import socket
import stat
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from app.podman import Podman
from app.store import Slot
from app.text import image_sha, slot_env

PROXY_PORT = 8443
APP_PORT = 8000
CADDY_NAME = "caddy"
CADDY_IMAGE = "docker.io/library/caddy:2"
JOB_RETRY = 30.0


class Paths:
    def __init__(self, root: Path) -> None:
        self.root = root

    def repo(self, name: str) -> Path:
        return self.root / "repos" / f"{name}.git"

    def data(self, slot: str) -> Path:
        return self.root / "data" / slot

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
    latest = max((run.finished for run in runs if run.finished), default=None)
    if latest is None:
        return JobPlan(start=True)
    return JobPlan(sleep=max(0.0, (latest + timedelta(seconds=every) - now).total_seconds()))


async def build_image(podman: Podman, paths: Paths, repo: str, directory: str, sha: str, image: str) -> str:
    if await podman.image_exists(image):
        return f"reuse {image}"
    build_dir = paths.build(repo, directory, sha)
    shutil.rmtree(build_dir, ignore_errors=True)
    try:
        await run_cmd("git", "clone", str(paths.repo(repo)), str(build_dir))
        await run_cmd("git", "-C", str(build_dir), "checkout", "-f", sha)
        file, context = containerfile(build_dir, directory)
        await podman.build(image, str(context), str(file))
    except Exception as exc:
        raise RuntimeError(f"build {image}\n{exc}") from exc
    return f"build {image}"


async def deploy_slot(podman: Podman, paths: Paths, slot: Slot, sha: str) -> Result:
    sha = sha.strip()
    image = image_tag(slot.repo, slot.directory, sha)
    lines: list[str] = []
    try:
        lines.append(await build_image(podman, paths, slot.repo, slot.directory, sha, image))
    except Exception as exc:
        lines.append(str(exc))
        return Result(image, "\n".join(lines) + "\n", str(exc))
    if slot.every:
        await _remove_stay(podman, slot.name)
        lines.append(f"job every {slot.every} image {image}")
        return Result(image, "\n".join(lines) + "\n")
    paths.data(slot.name).mkdir(parents=True, exist_ok=True)
    try:
        await podman.ensure_network("yard")
    except Exception:
        pass
    lines.append(f"stop {slot.name}")
    await _remove_stay(podman, slot.name)
    try:
        await _run_slot(podman, paths, slot, image)
    except Exception as exc:
        lines.append(str(exc))
        await _rollback(podman, paths, slot, image, lines)
        return Result(image, "\n".join(lines) + "\n", str(exc))
    health = f"http://{slot.name}:{APP_PORT}/"
    lines.append(f"wait {health}")
    try:
        await wait_url(health, 30)
    except Exception as exc:
        try:
            logs = await podman.run("logs", "--tail", "200", slot.name, timeout=20)
        except Exception:
            logs = ""
        lines.append(str(exc))
        if logs:
            lines.append(logs)
        await _remove_stay(podman, slot.name)
        await _rollback(podman, paths, slot, image, lines)
        return Result(image, "\n".join(lines) + "\n", str(exc))
    lines.append(f"ready {image}")
    return Result(image, "\n".join(lines) + "\n")


async def run_job(podman: Podman, paths: Paths, slot: Slot, image: str) -> str:
    paths.data(slot.name).mkdir(parents=True, exist_ok=True)
    try:
        await podman.ensure_network("yard")
    except Exception:
        pass
    await _remove_stay(podman, slot.name)
    args = [
        "run",
        "-d",
        "--name",
        slot.name,
        "--restart=no",
        "--network",
        "yard",
        "--log-driver",
        "journald",
        "--userns=keep-id",
        "--label",
        f"yard.slot={slot.name}",
        "--label",
        f"yard.image={image}",
        "-v",
        f"{paths.data(slot.name)}:/data:U",
    ]
    for item in slot_env(slot.name, slot.env):
        args.extend(("-e", item))
    sha = image_sha(image)
    if image.startswith("localhost/") and sha:
        args.extend(("-e", f"SHA={sha}"))
    args.append(image)
    await podman.run(*args, timeout=None)
    return slot.name


async def _run_slot(podman: Podman, paths: Paths, slot: Slot, image: str) -> None:
    args = [
        "run",
        "-d",
        "--name",
        slot.name,
        "--restart=always",
        "--network",
        "yard",
        "--label",
        f"yard.slot={slot.name}",
        "--log-driver",
        "journald",
        "--userns=keep-id",
        "-v",
        f"{paths.data(slot.name)}:/data:U",
    ]
    for item in slot_env(slot.name, slot.env):
        args.extend(("-e", item))
    args.append(image)
    await podman.run(*args, timeout=None)


async def _remove_stay(podman: Podman, slot: str) -> None:
    try:
        await podman.remove(slot)
    except Exception:
        pass


async def _rollback(podman: Podman, paths: Paths, slot: Slot, image: str, lines: list[str]) -> None:
    if not slot.live_image or slot.live_image == image:
        return
    lines.append(f"rollback {slot.live_image}")
    await _remove_stay(podman, slot.name)
    try:
        await _run_slot(podman, paths, slot, slot.live_image)
    except Exception as exc:
        lines.append(str(exc))


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


def containerfile(root: Path, sub: str) -> tuple[Path, Path]:
    directory = root if not sub else root / sub
    if sub and not directory.is_dir():
        raise RuntimeError(f"directory {sub!r} is not in the repo at this revision")
    for name in ("Containerfile", "Dockerfile", "ops/Containerfile", "ops/Dockerfile"):
        path = directory / name
        if path.is_file():
            return path, directory
    where = sub or "the repo root"
    raise RuntimeError(f"no Containerfile in {where}")


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


def caddy_document(sock: Path, email: str, routes: list[Route]) -> bytes:
    http_routes = [
        {
            "handle": [{"handler": "reverse_proxy", "upstreams": [{"dial": route.dial}]}],
            "match": [{"host": [route.host]}],
        }
        for route in routes
    ]
    issuer: dict[str, object] = {"challenges": {"http": {"disabled": True}}, "module": "acme"}
    if email:
        issuer["email"] = email
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


def caddy_run_args(root: Path, sock: Path) -> list[str]:
    data = root / "data" / "caddy"
    run = sock.parent
    return [
        "run",
        "-d",
        "--name",
        CADDY_NAME,
        "--restart=always",
        "--network",
        "yard",
        "--log-driver",
        "journald",
        "--entrypoint",
        "caddy",
        "-p",
        f"{PROXY_PORT}:{PROXY_PORT}",
        "-v",
        f"{data}:/config/caddy",
        "-v",
        f"{data}/lib:/data",
        "-v",
        f"{run}:{run}",
        CADDY_IMAGE,
        "run",
        "--resume",
    ]


async def ensure_caddy(podman: Podman, root: Path, sock: Path) -> None:
    data = root / "data" / "caddy"
    data.mkdir(parents=True, exist_ok=True)
    (data / "lib").mkdir(parents=True, exist_ok=True)
    sock.parent.mkdir(parents=True, exist_ok=True)
    auto = data / "autosave.json"
    if not auto.is_file() or auto.stat().st_size == 0:
        auto.write_bytes(caddy_document(sock, "", []))
        auto.chmod(0o600)
    try:
        await podman.run("container", "exists", CADDY_NAME, timeout=20)
        exists = True
    except Exception:
        exists = False
    if exists:
        try:
            state = await podman.run("inspect", "-f", "{{.State.Running}}", CADDY_NAME, timeout=20)
            running = state == "true"
        except Exception:
            running = False
        if not running:
            await podman.run("start", CADDY_NAME, timeout=20)
    else:
        try:
            await podman.ensure_network("yard")
        except Exception:
            pass
        await podman.run(*caddy_run_args(root, sock), timeout=None)
    await _wait_sock(sock, 15)


async def load_caddy(sock: Path, email: str, routes: list[Route]) -> None:
    await asyncio.to_thread(_post_unix, sock, caddy_document(sock, email, routes))


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


async def run_cmd(*args: str, timeout: float | None = None) -> str:
    proc = await asyncio.create_subprocess_exec(
        *args,
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
